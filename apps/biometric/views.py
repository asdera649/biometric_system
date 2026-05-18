"""
API-эндпоинты для биометрической обработки.
"""
import base64 as base64_lib
import io
import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import CustomUser, BiometricTemplate
from apps.operator.models import SystemSettings
from .face_processor import get_processor
from .models import AuthenticationLog, write_system_log

logger = logging.getLogger('apps.biometric')

def get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    return x_forwarded.split(',')[0] if x_forwarded else request.META.get('REMOTE_ADDR')

@require_POST
def api_register_biometric(request):
    """
    POST /biometric/api/register/
    Body JSON: { user_id, image_data (base64) }
    """
    try:
        data = json.loads(request.body)
        user_id = data.get('user_id')
        image_data = data.get('image_data', '')
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'success': False, 'error': 'Некорректный запрос'}, status=400)

    if not user_id or not image_data:
        return JsonResponse({'success': False, 'error': 'Отсутствуют обязательные поля'}, status=400)

    # Защита от IDOR: user_id должен совпадать с сессионным
    session_user_id = request.session.get('pending_biometric_user_id')
    if str(session_user_id) != str(user_id):
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
            'success': False,
            'error': result['error'],
            'quality_score': result.get('quality_score', 0),
            'detection_confidence': result.get('detection_confidence', 0),
        })

    conf = round(result['detection_confidence'] * 100, 2)
    qual = round(result['quality_score'] * 100, 2)

    template, _ = BiometricTemplate.objects.get_or_create(user=user)
    template.embedding = result['embedding']
    template.detection_confidence = conf
    template.image_quality_score = qual

    # Сохраняем миниатюру лица
    try:
        from PIL import Image
        from django.core.files.base import ContentFile
        img_bytes = base64_lib.b64decode(image_data.split(',', 1)[-1])
        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        img.thumbnail((200, 200))
        thumb_io = io.BytesIO()
        img.save(thumb_io, format='JPEG', quality=100)
        template.face_image.save(f'user_{user.pk}.jpg', ContentFile(thumb_io.getvalue()), save=False)
    except Exception:
        pass

    template.save()
    user.is_biometric_registered = True
    user.save(update_fields=['is_biometric_registered'])

    del request.session['pending_biometric_user_id']

    write_system_log(
        'INFO', 'Регистрация биометрии',
        f'Биометрия зарегистрирована для {user.username}. '
        f'Качество: {result["quality_score"]:.2%}, Обнаружение: {result["detection_confidence"]:.2%}',
        user=user,
    )

    return JsonResponse({
        'success': True,
        'quality_score': result['quality_score'],
        'detection_confidence': result['detection_confidence'],
        'message': 'Биометрия успешно зарегистрирована!',
    })

@require_POST
def api_authenticate(request):
    """
    POST /biometric/api/authenticate/
    Body JSON: { username, image_data (base64) }

    Пайплайн:
      1. Поиск пользователя + проверка блокировки
      2. Распознавание лица (face_processor) - порог из SystemSettings
      3. Пассивная проверка живости - порог из SystemSettings (если liveness_enabled)
         3а. Провал - liveness_failed=True, require_active_liveness=True
      4. Успех - login
    """
    try:
        data = json.loads(request.body)
        username = data.get('username', '').strip()
        image_data = data.get('image_data', '')
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'success': False, 'error': 'Некорректный запрос'}, status=400)

    if not username or not image_data:
        return JsonResponse({'success': False, 'error': 'Отсутствуют обязательные поля'}, status=400)

    ip = get_client_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', '')[:500]

    cfg = SystemSettings.get_settings()

    def log_attempt(user, result_code, conf, quality, liveness_score=None, error=''):
        AuthenticationLog.objects.create(
            user=user,
            attempted_username=username,
            result=result_code,
            recognition_confidence=conf,
            quality_score=quality,
            liveness_score=liveness_score,
            failure_reason=error,
            ip_address=ip,
            user_agent=ua,
        )

    # 1. Поиск пользователя
    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        log_attempt(None, AuthenticationLog.RESULT_FAIL_RECOGNITION, 0, 0,
                    error='Пользователь не найден')
        return JsonResponse({'success': False, 'error': 'Пользователь не найден'})

    if user.is_locked():
        log_attempt(user, AuthenticationLog.RESULT_FAIL_LOCKED, 0, 0,
                    error='Аккаунт заблокирован')
        remaining = (user.locked_until - timezone.now()).seconds // 60
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

    # 2. Распознавание лица
    processor = get_processor()
    if not processor.is_ready:
        return JsonResponse(
            {'success': False, 'error': 'Сервис распознавания временно недоступен'}, status=503
        )

    face_result = processor.process_authentication_image(
        image_data,
        template.embedding,
        threshold=cfg.recognition_threshold,
    )

    conf = round(face_result['recognition_confidence'] * 100, 2)
    qual = round(face_result['quality_score'] * 100, 2)

    if not face_result['success']:
        log_attempt(user, face_result['result_code'], conf, qual,
                    error=face_result.get('error', ''))
        _increment_failed_attempts(user, cfg)
        return JsonResponse({
            'success': False,
            'error': face_result['error'],
            'recognition_confidence': face_result['recognition_confidence'],
            'quality_score': face_result['quality_score'],
            'attempts_left': max(0, cfg.max_auth_attempts - user.failed_attempts),
        })

    # 3. Пассивная проверка живости
    liveness_score = None

    if cfg.liveness_enabled:
        liveness_result = _run_liveness_check(image_data, user, conf, qual,
                                              log_attempt, cfg)
        if isinstance(liveness_result, dict) and liveness_result.get('failed'):
            return JsonResponse(liveness_result['response'])

        liveness_score = liveness_result

    # 4. Успешная аутентификация
    log_attempt(user, AuthenticationLog.RESULT_SUCCESS, conf, qual,
                liveness_score=liveness_score)

    user.reset_failed_attempts()
    from django.contrib.auth import login as auth_login
    auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')

    write_system_log(
        'INFO', 'Аутентификация',
        f'Успешный вход: {user.username}, '
        f'схожесть: {face_result["recognition_confidence"]:.2%}'
        + (f', liveness: {liveness_score:.2f}' if liveness_score is not None else ''),
        user=user,
    )

    return JsonResponse({
        'success': True,
        'recognition_confidence': face_result['recognition_confidence'],
        'quality_score': face_result['quality_score'],
        'redirect_url': '/accounts/success/',
    })

#  Utils

def _increment_failed_attempts(user, cfg):
    """
    Увеличивает счётчик неудачных попыток.
    Порог max_auth_attempts и lockout_minutes - из SystemSettings.
    """
    user.failed_attempts += 1
    if user.failed_attempts >= cfg.max_auth_attempts:
        user.locked_until = timezone.now() + timezone.timedelta(minutes=cfg.lockout_minutes)
        write_system_log(
            'WARNING', 'Аутентификация',
            f'Аккаунт заблокирован: {user.username} ({user.failed_attempts} попыток)',
            user=user,
        )
    user.save(update_fields=['failed_attempts', 'locked_until'])


def _run_liveness_check(image_data: str, user, conf, qual, log_attempt, cfg):
    """
    Выполняет пассивную проверку живости.

    Возвращает:
      float  - liveness_score, если проверка пройдена
      None   - если сервис недоступен (graceful degradation, вход не блокируется)
      dict   - {'failed': True, 'response': dict} при провале проверки
    """
    try:
        from apps.liveness.service import LivenessService

        raw_b64 = image_data.split(',', 1)[-1] if ',' in image_data else image_data
        img_bytes = base64_lib.b64decode(raw_b64)

        liveness_svc = LivenessService.get_instance()

        liveness_result = liveness_svc.check(
            img_bytes,
            threshold=cfg.liveness_threshold,
        )
        liveness_score = liveness_result.score

        if not liveness_result.is_real:
            log_attempt(
                user,
                AuthenticationLog.RESULT_FAIL_LIVENESS,
                conf, qual,
                liveness_score=liveness_score,
                error=(
                    f'Liveness failed: score={liveness_score:.3f}, '
                    f'threshold={cfg.liveness_threshold:.2f}, '
                    f'label={liveness_result.label_name}'
                ),
            )
            write_system_log(
                'WARNING', 'Аутентификация',
                f'Liveness failed: {user.username}, '
                f'score={liveness_score:.3f} (threshold={cfg.liveness_threshold:.2f})',
                user=user,
            )
            return {
                'failed': True,
                'response': {
                    'success': False,
                    'liveness_failed': True,
                    'require_active_liveness': True,
                    'liveness_score': liveness_score,
                    'recognition_confidence': conf / 100,
                    'quality_score': qual / 100,
                    'error': 'Проверка живости не пройдена. Пожалуйста, попробуйте ещё раз.',
                },
            }

        return liveness_score

    except Exception as exc:
        # Graceful degradation: сервис недоступен - не блокируем вход, только лог
        logger.error('Liveness service unavailable: %s', exc)
        write_system_log(
            'WARNING', 'Liveness',
            f'Сервис недоступен для {user.username}: {exc}',
            user=user,
        )
        return None