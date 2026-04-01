"""
API-эндпоинты для биометрической обработки (AJAX/JSON).
"""
import json
import logging
import base64
import io
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings

from apps.accounts.models import CustomUser, BiometricTemplate
from apps.operator.models import SystemSettings
from .models import AuthenticationLog, write_system_log
from .face_processor import get_processor

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
