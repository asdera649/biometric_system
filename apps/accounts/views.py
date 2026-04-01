import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib import messages
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required

from .models import CustomUser, BiometricTemplate
from .forms import UserRegistrationForm, FaceLoginForm
from apps.biometric.models import write_system_log, SystemLog

logger = logging.getLogger('apps.accounts')


def home(request):
    info_blocks = [
        # ('shield-lock', 'Биометрия', 'Распознавание лица в реальном времени'),
        # ('cpu', 'ИИ-анализ', 'InceptionResNetV1 + VGGFace2'),
        # ('eye-slash', 'Защита', 'От атак с фото, видео, масками'),
        # ('journal-text', 'Аудит', 'Полный журнал событий'),
    ]
    return render(request, 'home.html', {'info_blocks': info_blocks})


def register_step1(request):
    """Шаг 1: Ввод личных данных"""
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            request.session['pending_biometric_user_id'] = user.pk
            write_system_log('INFO', 'Регистрация', f'Новый пользователь создан: {user.username}')
            messages.success(request, 'Данные сохранены. Теперь зарегистрируйте биометрию.')
            return redirect('register_step2')
    else:
        form = UserRegistrationForm()
    return render(request, 'accounts/register_step1.html', {'form': form})


def register_step2(request):
    """Шаг 2: Регистрация биометрии (захват лица)"""
    user_id = request.session.get('pending_biometric_user_id')
    if not user_id:
        return redirect('register_step1')
    user = get_object_or_404(CustomUser, pk=user_id)
    return render(request, 'accounts/register_step2.html', {'user': user})


def face_login(request):
    """Аутентификация по лицу"""
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = FaceLoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            try:
                user = CustomUser.objects.get(username=username)
                if not user.is_biometric_registered:
                    messages.error(request, 'Биометрия не зарегистрирована для этого пользователя.')
                    return render(request, 'accounts/face_login.html', {'form': form})
                if user.is_locked():
                    messages.error(request, f'Аккаунт заблокирован до {user.locked_until:%H:%M %d.%m.%Y}.')
                    return render(request, 'accounts/face_login.html', {'form': form})
                # Сохраняем логин в сессии для следующего шага (захвата лица)
                request.session['auth_username'] = username
                return redirect('face_capture')
            except CustomUser.DoesNotExist:
                messages.error(request, 'Пользователь не найден.')
    else:
        form = FaceLoginForm()
    return render(request, 'accounts/face_login.html', {'form': form})


def face_capture(request):
    """Страница захвата лица для аутентификации"""
    username = request.session.get('auth_username')
    if not username:
        return redirect('face_login')
    return render(request, 'accounts/face_capture.html', {'username': username})


def auth_success(request):
    if not request.user.is_authenticated:
        return redirect('home')
    return render(request, 'accounts/auth_success.html')


@login_required
def profile(request):
    return render(request, 'accounts/profile.html', {'user': request.user})


@login_required
def logout_view(request):
    write_system_log('INFO', 'Аутентификация', f'Выход пользователя: {request.user.username}', user=request.user)
    logout(request)
    messages.info(request, 'Вы вышли из системы.')
    return redirect('home')
