"""
API-эндпоинты для биометрической обработки (AJAX/JSON).
"""
import json
import logging
import base64, io
from PIL import Image
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings

from apps.accounts.models import CustomUser, BiometricTemplate
from apps.operator.models import SystemSettings
from .models import AuthenticationLog, write_system_log
from .face_processor import get_processor

from .models import AuthenticationLog, SystemLog, write_system_log, DoorAccessLog

logger = logging.getLogger('apps.biometric')


def get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    return x_forwarded.split(',')[0] if x_forwarded else request.META.get('REMOTE_ADDR')


@require_POST
def api_register_biometric(request):
    """
    POST: Регистрация биометрии пользователя.
    Body JSON: {user_id, image_data (base64)}
    """
    try:
        data = json.loads(request.body)
        user_id = data.get('user_id')
        image_data = data.get('image_data', '')
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'success': False, 'error': 'Некорректный запрос'}, status=400)

    if not user_id or not image_data:
        return JsonResponse({'success': False, 'error': 'Отсутствуют обязательные поля'}, status=400)

    # Проверяем, что user_id совпадает с сессионным (защита от IDOR)
    session_user_id = request.session.get('pending_biometric_user_id')
    if str(session_user_id) != str(user_id):
        return JsonResponse({'success': False, 'error': 'Нет доступа'}, status=403)

    try:
        user = CustomUser.objects.get(pk=user_id)
    except CustomUser.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Пользователь не найден'}, status=404)

    cfg = SystemSettings.get_settings()
    processor = get_processor()

    if not processor.is_ready:
        return JsonResponse({'success': False, 'error': 'Сервис распознавания временно недоступен'}, status=503)

    result = processor.process_registration_image(image_data)

    if not result['success']:
        write_system_log('WARNING', 'Регистрация биометрии',
                         f'Неудача для {user.username}: {result["error"]}', user=user)
        return JsonResponse({'success': False, 'error': result['error'],
                             'quality_score': result.get('quality_score', 0),
                             'detection_confidence': result.get('detection_confidence', 0)})

    # Сохраняем шаблон
    template, _ = BiometricTemplate.objects.get_or_create(user=user)
    template.embedding = result['embedding']
    template.detection_confidence = result['detection_confidence']
    template.image_quality_score = result['quality_score']

    # Сохраняем миниатюру лица
    if image_data:
        try:
            from PIL import Image
            img_bytes = base64.b64decode(image_data.split(',', 1)[-1])
            img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
            img.thumbnail((200, 200))
            thumb_io = io.BytesIO()
            img.save(thumb_io, format='JPEG', quality=85)
            from django.core.files.base import ContentFile
            template.face_image.save(f'user_{user.pk}.jpg', ContentFile(thumb_io.getvalue()), save=False)
        except Exception:
            pass

    template.save()
    user.is_biometric_registered = True
    user.save(update_fields=['is_biometric_registered'])

    # Чистим сессию
    del request.session['pending_biometric_user_id']

    write_system_log('INFO', 'Регистрация биометрии',
                     f'Биометрия зарегистрирована для {user.username}. '
                     f'Качество: {result["quality_score"]:.2%}, Детекция: {result["detection_confidence"]:.2%}',
                     user=user)

    return JsonResponse({
        'success': True,
        'quality_score': result['quality_score'],
        'detection_confidence': result['detection_confidence'],
        'message': 'Биометрия успешно зарегистрирована!'
    })


@require_POST
def api_authenticate(request):
    """
    POST: Аутентификация по лицу.
    Body JSON: {username, image_data (base64)}
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

    def log_attempt(user, result_code, conf, quality, error=''):
        AuthenticationLog.objects.create(
            user=user, attempted_username=username,
            result=result_code, recognition_confidence=conf,
            quality_score=quality, failure_reason=error,
            ip_address=ip, user_agent=ua
        )

    try:
        user = CustomUser.objects.get(username=username)
    except CustomUser.DoesNotExist:
        log_attempt(None, 'fail_recognition', 0, 0, 'Пользователь не найден')
        return JsonResponse({'success': False, 'error': 'Пользователь не найден'})

    if user.is_locked():
        log_attempt(user, 'fail_locked', 0, 0, 'Аккаунт заблокирован')
        remaining = (user.locked_until - timezone.now()).seconds // 60
        return JsonResponse({'success': False,
                             'error': f'Аккаунт заблокирован. Повторите через {remaining} мин.'})

    if not user.is_biometric_registered:
        return JsonResponse({'success': False, 'error': 'Биометрия не зарегистрирована'})

    try:
        template = user.biometric_template
    except BiometricTemplate.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Шаблон не найден'})

    processor = get_processor()
    if not processor.is_ready:
        return JsonResponse({'success': False, 'error': 'Сервис распознавания временно недоступен'}, status=503)

    result = processor.process_authentication_image(
        image_data, template.embedding, threshold=cfg.recognition_threshold
    )

    log_attempt(user, result['result_code'],
                result['recognition_confidence'], result['quality_score'],
                result.get('error', ''))

    if result['success']:
        from django.contrib.auth import login as auth_login
        user.reset_failed_attempts()
        auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        write_system_log('INFO', 'Аутентификация',
                         f'Успешный вход: {user.username}, схожесть: {result["recognition_confidence"]:.2%}',
                         user=user)
        return JsonResponse({
            'success': True,
            'recognition_confidence': result['recognition_confidence'],
            'quality_score': result['quality_score'],
            'redirect_url': '/accounts/success/'
        })
    else:
        user.failed_attempts += 1
        if user.failed_attempts >= cfg.max_auth_attempts:
            user.locked_until = timezone.now() + timezone.timedelta(minutes=cfg.lockout_minutes)
            write_system_log('WARNING', 'Аутентификация',
                             f'Аккаунт заблокирован: {user.username} ({user.failed_attempts} попыток)',
                             user=user)
        user.save(update_fields=['failed_attempts', 'locked_until'])

        return JsonResponse({
            'success': False,
            'error': result['error'],
            'recognition_confidence': result['recognition_confidence'],
            'quality_score': result['quality_score'],
            'attempts_left': max(0, cfg.max_auth_attempts - user.failed_attempts)
        })

@require_POST
def api_identify(request):
    """
    Идентификация лица (для домофона — без логина).

    Принимает кадр с камеры домофона, сравнивает со всеми биометрическими
    шаблонами в базе, возвращает лучшее совпадение (если выше порога).

    Body JSON:
        {
            "image_data": "<base64 JPEG>",
            "service_token": "<секретный токен домофон-сервиса>",   // опционально
            "open_door": true   // запросить открытие двери при успехе
        }

    Response:
        {
            "identified": true/false,
            "user_id": 5,
            "username": "ivanov",
            "full_name": "Иванов Иван",
            "recognition_confidence": 0.87,
            "quality_score": 0.72,
            "door_opened": true,
            "error": null
        }
    """
    try:
        data = json.loads(request.body)
        image_data = data.get('image_data', '')
        open_door = data.get('open_door', True)
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'identified': False, 'error': 'Некорректный запрос'}, status=400)

    if not image_data:
        return JsonResponse({'identified': False, 'error': 'Нет изображения'}, status=400)

    # Простая защита: сервис должен слать токен из settings
    service_token = data.get('service_token', '')
    expected_token = getattr(settings, 'DOORBELL_SERVICE_TOKEN', '')
    if expected_token and service_token != expected_token:
        return JsonResponse({'identified': False, 'error': 'Неверный токен'}, status=403)

    ip = get_client_ip(request)
    cfg = SystemSettings.get_settings()
    processor = get_processor()

    if not processor.is_ready:
        return JsonResponse({'identified': False, 'error': 'Face processor недоступен'}, status=503)

    # Декодируем изображение и считаем качество
    from apps.biometric.quality import calculate_quality
    try:
        img = processor.decode_image(image_data)
    except Exception as e:
        return JsonResponse({'identified': False, 'error': f'Ошибка декодирования: {e}'}, status=400)

    quality = calculate_quality(img)

    # Обнаружение лица
    face_tensor, det_conf, box = processor.detect_face(img)
    if face_tensor is None:
        _save_door_log(None, 'no_face', 0.0, quality, False, ip, image_data)
        return JsonResponse({
            'identified': False, 'error': 'Лицо не обнаружено',
            'quality_score': quality, 'recognition_confidence': 0.0
        })

    # Извлекаем вектор
    embedding = processor.get_embedding(face_tensor)
    if embedding is None:
        _save_door_log(None, 'no_face', 0.0, quality, False, ip, image_data)
        return JsonResponse({'identified': False, 'error': 'Ошибка извлечения признаков'})

    # Сравниваем со всеми зарегистрированными шаблонами
    from apps.accounts.models import BiometricTemplate, CustomUser
    templates = BiometricTemplate.objects.select_related('user').filter(
        user__is_active=True, user__is_biometric_registered=True
    )

    best_user = None
    best_similarity = 0.0

    for tmpl in templates:
        try:
            stored_emb = processor.embedding_from_json(tmpl.embedding)
            sim = processor.cosine_similarity(embedding, stored_emb)
            if sim > best_similarity:
                best_similarity = sim
                best_user = tmpl.user
        except Exception:
            continue

    threshold = cfg.recognition_threshold

    if best_similarity >= threshold and best_user is not None:
        # Пользователь идентифицирован
        door_opened = False
        if open_door:
            door_opened = _open_door()

        _save_door_log(best_user, 'granted', best_similarity, quality, door_opened, ip, image_data)
        write_system_log(
            'INFO', 'Домофон',
            f'Идентифицирован: {best_user.username}, схожесть: {best_similarity:.2%}',
            user=best_user
        )

        return JsonResponse({
            'identified': True,
            'user_id': best_user.pk,
            'username': best_user.username,
            'full_name': best_user.get_full_name_ru(),
            'recognition_confidence': round(best_similarity, 4),
            'quality_score': round(quality, 4),
            'door_opened': door_opened,
            'error': None
        })

    else:
        # Никто не распознан
        _save_door_log(None, 'denied', best_similarity, quality, False, ip, image_data)
        write_system_log(
            'WARNING', 'Домофон',
            f'Доступ запрещён. Лучшая схожесть: {best_similarity:.2%} (порог {threshold:.2%})'
        )
        return JsonResponse({
            'identified': False,
            'error': f'Пользователь не распознан (схожесть {best_similarity:.2%})',
            'recognition_confidence': round(best_similarity, 4),
            'quality_score': round(quality, 4),
            'door_opened': False
        })


def _open_door() -> bool:
    """
    Заглушка - логика открытия замка.

    """
    # import logging
    # logger = logging.getLogger('apps.biometric')
    try:


        logger.info('Команда открытия двери отправлена')
        return True
    except Exception as e:
        logger.error(f'Ошибка открытия двери: {e}')
        return False


def _save_door_log(user, result, confidence, quality, door_opened, ip, image_data=None):
    """Сохранить запись в DoorAccessLog"""
    try:
        log = DoorAccessLog.objects.create(
            user=user, result=result,
            recognition_confidence=confidence,
            quality_score=quality,
            door_opened=door_opened,
            source_ip=ip
        )
        # Сохраняем снимок
        if image_data:
            try:
                img_bytes = base64.b64decode(image_data.split(',', 1)[-1])
                img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
                img.thumbnail((320, 240))
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=75)
                log.snapshot.save(f'door_{log.pk}.jpg', ContentFile(buf.getvalue()), save=True)
            except Exception:
                pass
    except Exception:
        pass