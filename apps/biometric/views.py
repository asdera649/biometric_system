"""
apps/biometric/views.py

API-эндпоинты биометрической обработки.

Поток аутентификации с liveness:

    POST /biometric/api/authenticate/
        Распознавание лица:
            ✗ не распознан        → {success: false, error: ...}
            ✓ распознан, liveness ВЫКЛ → логин → {success: true}
            ✓ распознан, liveness ВКЛ  → пассивная проверка:
                    score ≥ ACCEPT      → логин → {success: true}
                    score ≤ REJECT      → {success: false, result: fail_liveness}
                    СРЕДНЯЯ ЗОНА        → {success: false,
                                           needs_challenge: true,
                                           challenge_type: ...,
                                           challenge_label: ...}

    POST /biometric/api/liveness/frame/
        Каждые ~300 мс фронтенд присылает кадр.
        Сервер проверяет: задание выполнено? и тот ли это человек?
        При успехе: логин → {success: true, redirect_url: ...}
        При ошибке идентификации (> 3 раз): {success: false, abort: true}
        Таймаут 45 с: {success: false, abort: true, error: 'Время истекло'}

    POST /biometric/api/liveness/cancel/
        Отмена challenge.
"""

import json
import logging
import base64
import io

from django.http        import JsonResponse
from django.views.decorators.http import require_POST
from django.utils       import timezone
from django.contrib.auth import login as auth_login

from apps.accounts.models  import CustomUser, BiometricTemplate
from apps.operator.models  import SystemSettings
from .face_processor       import get_processor
from .models               import AuthenticationLog, write_system_log
from .liveness             import pipeline as liveness_pipeline_mod
from .liveness             import session as liveness_session
from .liveness.base        import ActiveChallengeEngine

logger = logging.getLogger('apps.biometric')

# Утилиты

def _get_client_ip(request) -> str:
    fwd = request.META.get('HTTP_X_FORWARDED_FOR')
    return fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')

def _log_auth_attempt(
    user, username, result_code,
    recognition_confidence, quality_score,
    ip, ua, error='', liveness_score=None,
) -> None:
    AuthenticationLog.objects.create(
        user=user,
        attempted_username=username,
        result=result_code,
        recognition_confidence=recognition_confidence,
        quality_score=quality_score,
        liveness_score=liveness_score,
        failure_reason=error,
        ip_address=ip,
        user_agent=ua,
    )

def _do_login(request, user) -> None:
    """Авторизовать пользователя и сбросить счётчик неудачных попыток."""
    user.reset_failed_attempts()
    auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')


# Регистрация биометрии

@require_POST
def api_register_biometric(request):
    """
    POST /biometric/api/register/
    Body: {user_id, image_data}
    """
    try:
        data       = json.loads(request.body)
        user_id    = data.get('user_id')
        image_data = data.get('image_data', '')
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'success': False, 'error': 'Некорректный запрос'}, status=400)

    if not user_id or not image_data:
        return JsonResponse({'success': False, 'error': 'Отсутствуют обязательные поля'}, status=400)

    # Защита от IDOR: user_id должен совпадать с сессионным
    if str(request.session.get('pending_biometric_user_id')) != str(user_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)

    try:
        user = CustomUser.objects.get(pk=user_id)
    except CustomUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Пользователь не найден'}, status=404)

    processor = get_processor()
    if not processor.is_ready:
        return JsonResponse(
            {'success': False, 'error': 'Сервис распознавания временно недоступен'}, status=503
        )

    result = processor.process_registration_image(image_data)
    if not result['success']:
        write_system_log('WARNING', 'Регистрация биометрии',
                         f'Неудача для {user.username}: {result["error"]}', user=user)
        return JsonResponse({
            'success':              False,
            'error':                result['error'],
            'quality_score':        result.get('quality_score', 0),
            'detection_confidence': result.get('detection_confidence', 0),
        })

    conf = round(result['detection_confidence'] * 100, 2)
    qual = round(result['quality_score'] * 100, 2)

    template, _ = BiometricTemplate.objects.get_or_create(user=user)
    template.embedding             = result['embedding']
    template.detection_confidence  = conf
    template.image_quality_score   = qual

    # Сохраняем миниатюру лица
    try:
        from PIL import Image
        from django.core.files.base import ContentFile
        img_bytes = base64.b64decode(image_data.split(',', 1)[-1])
        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        img.thumbnail((200, 200))
        thumb_io = io.BytesIO()
        img.save(thumb_io, format='JPEG', quality=100)
        template.face_image.save(
            f'user_{user.pk}.jpg', ContentFile(thumb_io.getvalue()), save=False
        )
    except Exception:
        pass

    template.save()
    user.is_biometric_registered = True
    user.save(update_fields=['is_biometric_registered'])

    del request.session['pending_biometric_user_id']

    write_system_log(
        'INFO', 'Регистрация биометрии',
        f'Биометрия зарегистрирована для {user.username}. '
        f'Качество: {result["quality_score"]:.2%}, Детекция: {result["detection_confidence"]:.2%}',
        user=user,
    )

    return JsonResponse({
        'success':              True,
        'quality_score':        result['quality_score'],
        'detection_confidence': result['detection_confidence'],
        'message':              'Биометрия успешно зарегистрирована!',
    })


# Аутентификация

@require_POST
def api_authenticate(request):
    """
    POST /biometric/api/authenticate/
    Body: {username, image_data}

    Шаг 1 двухэтапного liveness pipeline:
        Распознавание → пассивная проверка → логин ИЛИ challenge ИЛИ отказ.
    """
    try:
        data       = json.loads(request.body)
        username   = data.get('username', '').strip()
        image_data = data.get('image_data', '')
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'success': False, 'error': 'Некорректный запрос'}, status=400)

    if not username or not image_data:
        return JsonResponse({'success': False, 'error': 'Отсутствуют обязательные поля'}, status=400)

    ip = _get_client_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', '')[:500]
    cfg = SystemSettings.get_settings()

    # Поиск пользователя
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        _log_auth_attempt(None, username, 'fail_recognition', 0, 0, ip, ua, 'Пользователь не найден')
        return JsonResponse({'success': False, 'error': 'Пользователь не найден'})

    if user.is_locked():
        remaining = max(0, int((user.locked_until - timezone.now()).total_seconds() // 60))
        _log_auth_attempt(user, username, 'fail_locked', 0, 0, ip, ua, 'Аккаунт заблокирован')
        return JsonResponse({
            'success': False,
            'error': f'Аккаунт заблокирован. Повторите через {remaining} мин.',
        })

    if not user.is_biometric_registered:
        return JsonResponse({'success': False, 'error': 'Биометрия не зарегистрирована'})

    try:
        template = user.biometric_template
    except BiometricTemplate.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Шаблон не найден'})

    processor = get_processor()
    if not processor.is_ready:
        return JsonResponse(
            {'success': False, 'error': 'Сервис распознавания временно недоступен'}, status=503
        )

    # Распознавание лица
    result = processor.process_authentication_image(
        image_data, template.embedding, threshold=cfg.recognition_threshold
    )

    conf = round(result['recognition_confidence'] * 100, 2)
    qual = round(result['quality_score'] * 100, 2)

    if not result['success']:
        # Распознавание провалилось — сразу логируем и наказываем
        _log_auth_attempt(
            user, username, result['result_code'], conf, qual,
            ip, ua, result.get('error', ''),
        )
        _increment_failed_attempts(user, cfg)
        return JsonResponse({
            'success':                False,
            'error':                  result['error'],
            'recognition_confidence': result['recognition_confidence'],
            'quality_score':          result['quality_score'],
            'attempts_left':          max(0, cfg.max_auth_attempts - user.failed_attempts),
        })

    # Liveness
    if not cfg.liveness_enabled:
        # Liveness выключен — логиним сразу
        _log_auth_attempt(user, username, 'success', conf, qual, ip, ua)
        _do_login(request, user)
        write_system_log(
            'INFO', 'Аутентификация',
            f'Успешный вход: {user.username}, схожесть: {result["recognition_confidence"]:.2%}',
            user=user,
        )
        return JsonResponse({
            'success':                True,
            'recognition_confidence': result['recognition_confidence'],
            'quality_score':          result['quality_score'],
            'redirect_url':           '/accounts/success/',
        })

    # Liveness включён — запускаем пассивный детектор
    img      = processor.decode_image(image_data)
    pipeline = liveness_pipeline_mod.get_pipeline()
    passive  = pipeline.run_passive(img)

    if passive['status'] == 'reject':
        # Явная атака или артефакт — логируем и блокируем попытку
        _log_auth_attempt(
            user, username, 'fail_liveness', conf, qual, ip, ua,
            f'Passive liveness reject: score={passive["score"]:.3f}',
        )
        _increment_failed_attempts(user, cfg)
        return JsonResponse({
            'success':                False,
            'recognition_confidence': result['recognition_confidence'],
            'quality_score':          result['quality_score'],
            'error':                  'Обнаружена попытка обмана системы. Доступ отклонён.',
            'attempts_left':          max(0, cfg.max_auth_attempts - user.failed_attempts),
        })

    if passive['status'] == 'accept':
        # Пассивный детектор уверен — логиним
        _log_auth_attempt(
            user, username, 'success', conf, qual, ip, ua,
            liveness_score=passive['score'],
        )
        _do_login(request, user)
        write_system_log(
            'INFO', 'Аутентификация',
            f'Вход (passive liveness ok): {user.username}, '
            f'схожесть: {result["recognition_confidence"]:.2%}, '
            f'liveness: {passive["score"]:.2%}',
            user=user,
        )
        return JsonResponse({
            'success':                True,
            'recognition_confidence': result['recognition_confidence'],
            'quality_score':          result['quality_score'],
            'redirect_url':           '/accounts/success/',
        })

    # Пассивный детектор не уверен — активный challenge
    current_embedding = result.get('current_embedding')
    if current_embedding is None:
        # Нет вектора текущего кадра (редко) — используем шаблон как эталон
        current_embedding = processor.embedding_from_json(template.embedding)

    challenge_type = pipeline.select_challenge()

    liveness_session.create(
        request,
        username=username,
        challenge_type=challenge_type,
        reference_embedding=current_embedding,
        recognition_confidence=result['recognition_confidence'],
        quality_score=result['quality_score'],
    )

    return JsonResponse({
        'success':                False,
        'needs_challenge':        True,
        'challenge_type':         challenge_type,
        'challenge_label':        ActiveChallengeEngine.CHALLENGE_LABELS[challenge_type],
        'challenge_icon':         ActiveChallengeEngine.CHALLENGE_ICONS[challenge_type],
        'passive_score':          passive['score'],
        'recognition_confidence': result['recognition_confidence'],
        'quality_score':          result['quality_score'],
    })


# Активный challenge

@require_POST
def api_liveness_frame(request):
    """
    POST /biometric/api/liveness/frame/
    Body: {image_data}  (base64, один кадр)

    Фронтенд вызывает каждые ~300 мс во время активного challenge.
    Сервер:
        1. Проверяет наличие и актуальность сеанса.
        2. Анализирует кадр: challenge + same-person.
        3. Если challenge выполнен и личность подтверждена → логинит.
        4. Если провалов личности > MAX_IDENTITY_FAILURES → abort.
    """
    try:
        data = json.loads(request.body)
        image_data = data.get('image_data', '')
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'success': False, 'error': 'Некорректный запрос'}, status=400)

    if not image_data:
        return JsonResponse({'success': False, 'error': 'image_data обязателен'}, status=400)

    # Сеанс
    sess = liveness_session.get(request)
    if sess is None:
        return JsonResponse({
            'success': False, 'abort': True,
            'error': 'Сеанс проверки истёк или не существует. Попробуйте снова.',
        })

    username       = sess['username']
    challenge_type = sess['challenge_type']
    ref_emb        = liveness_session.get_reference_embedding(sess)
    secs_left      = liveness_session.seconds_remaining(sess)

    # Получаем объекты
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        liveness_session.clear(request)
        return JsonResponse({'success': False, 'abort': True, 'error': 'Пользователь не найден'})

    processor = get_processor()
    if not processor.is_ready:
        return JsonResponse(
            {'success': False, 'error': 'Сервис временно недоступен'}, status=503
        )

    cfg = SystemSettings.get_settings()
    ip  = _get_client_ip(request)
    ua  = request.META.get('HTTP_USER_AGENT', '')[:500]

    # Анализ кадра
    img = processor.decode_image(image_data)
    pipeline = liveness_pipeline_mod.get_pipeline()

    frame_result = pipeline.analyze_challenge_frame(
        image=img,
        challenge_type=challenge_type,
        reference_embedding=ref_emb,
        face_processor=processor,
    )

    # Реальный провал личности: лицо обнаружено, но схожесть ниже порога
    real_identity_failure = (
        frame_result.identity_score >= 0            # лицо обнаружено
        and not frame_result.identity_ok            # схожесть ниже порога
    )

    sess = liveness_session.increment_frame(request, real_identity_failure)
    frames_count    = sess['frames_analyzed']
    identity_fails  = sess['identity_failures']

    # Проверка: слишком много провалов личности → подмена
    if identity_fails > liveness_pipeline_mod.HybridLivenessPipeline.MAX_IDENTITY_FAILURES:
        liveness_session.clear(request)
        _log_auth_attempt(
            user, username, 'fail_liveness',
            round(sess['recognition_confidence'] * 100, 2),
            round(sess['quality_score'] * 100, 2),
            ip, ua,
            f'Same-person verification failed ({identity_fails} раз)',
        )
        _increment_failed_attempts(user, cfg)
        return JsonResponse({
            'success': False, 'abort': True,
            'error': 'Верификация личности провалена. Перед камерой другой человек.',
        })

    # Challenge выполнен
    if frame_result.challenge_completed and frame_result.identity_ok:
        liveness_session.mark_completed(request)
        liveness_session.clear(request)

        _log_auth_attempt(
            user, username, 'success',
            round(sess['recognition_confidence'] * 100, 2),
            round(sess['quality_score'] * 100, 2),
            ip, ua,
            liveness_score=frame_result.identity_score,
        )
        _do_login(request, user)
        write_system_log(
            'INFO', 'Аутентификация',
            f'Вход (liveness challenge ok): {user.username}, '
            f'challenge={challenge_type}, кадров={frames_count}, '
            f'identity={frame_result.identity_score:.2%}',
            user=user,
        )
        return JsonResponse({
            'success':      True,
            'redirect_url': '/accounts/success/',
        })

    # Challenge ещё не выполнен
    return JsonResponse({
        'success':    False,
        'completed':  False,
        'progress':   round(frame_result.progress, 3),
        'identity_ok': frame_result.identity_ok,
        'identity_score': round(max(frame_result.identity_score, 0), 3),
        'seconds_left': secs_left,
        'frames': frames_count,
    })


# Отмена challenge

@require_POST
def api_liveness_cancel(request):
    """
    POST /biometric/api/liveness/cancel/
    Пользователь отменил challenge.
    """
    liveness_session.clear(request)
    return JsonResponse({'success': True})


# Утилиты

def _increment_failed_attempts(user: CustomUser, cfg) -> None:
    user.failed_attempts += 1
    if user.failed_attempts >= cfg.max_auth_attempts:
        user.locked_until = timezone.now() + timezone.timedelta(minutes=cfg.lockout_minutes)
        write_system_log(
            'WARNING', 'Аутентификация',
            f'Аккаунт заблокирован: {user.username} ({user.failed_attempts} попыток)',
            user=user,
        )
    user.save(update_fields=['failed_attempts', 'locked_until'])
