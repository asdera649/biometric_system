import logging
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse, HttpResponse
from django.db.models import Count, Avg, Min, Max, Q
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST

from apps.accounts.models import CustomUser, BiometricTemplate
from apps.biometric.models import AuthenticationLog, SystemLog, write_system_log
from .models import SystemSettings, ModelMetricsSnapshot
from apps.biometric.models import DoorAccessLog
from apps.accounts.forms import UserEditForm

logger = logging.getLogger('apps.operator')


def operator_required(view_func):
    """Декоратор: доступ только для операторов"""
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_operator and not request.user.is_superuser:
            messages.error(request, 'Доступ запрещён. Требуются права оператора.')
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return wrapped


@operator_required
def dashboard(request):
    """Главная панель оператора"""
    now = timezone.now()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    # Статистика за 24 часа
    logs_24h = AuthenticationLog.objects.filter(timestamp__gte=last_24h)
    total_24h = logs_24h.count()
    success_24h = logs_24h.filter(result='success').count()
    fail_24h = total_24h - success_24h

    # Общая статистика
    total_users = CustomUser.objects.filter(is_active=True).count()
    bio_users = CustomUser.objects.filter(is_biometric_registered=True).count()
    locked_users = CustomUser.objects.filter(locked_until__gt=now).count()

    # График активности по часам (последние 24ч)
    hourly_data = []
    for i in range(23, -1, -1):
        hour_start = now - timedelta(hours=i+1)
        hour_end = now - timedelta(hours=i)
        cnt = AuthenticationLog.objects.filter(timestamp__gte=hour_start, timestamp__lt=hour_end).count()
        hourly_data.append({'hour': hour_end.strftime('%H:00'), 'count': cnt})

    # Последние события
    recent_logs = AuthenticationLog.objects.select_related('user')[:10]
    recent_system_logs = SystemLog.objects.all()[:5]

    # Метрики нейросети
    avg_metrics = logs_24h.aggregate(
        avg_conf=Avg('recognition_confidence'),
        avg_quality=Avg('quality_score')
    )

    ctx = {
        'total_24h': total_24h, 'success_24h': success_24h, 'fail_24h': fail_24h,
        'total_users': total_users, 'bio_users': bio_users, 'locked_users': locked_users,
        'success_rate': round(success_24h / total_24h * 100, 1) if total_24h else 0,
        'hourly_data': hourly_data,
        'recent_logs': recent_logs,
        'recent_system_logs': recent_system_logs,
        'avg_recognition': avg_metrics['avg_conf'] or 0,
        'avg_quality': avg_metrics['avg_quality'] or 0,
    }
    return render(request, 'operator/dashboard.html', ctx)


@operator_required
def auth_logs(request):
    """Журнал аутентификации с фильтрацией"""
    qs = AuthenticationLog.objects.select_related('user').all()

    # Фильтры
    result_filter = request.GET.get('result', '')
    user_filter = request.GET.get('username', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if result_filter:
        qs = qs.filter(result=result_filter)
    if user_filter:
        qs = qs.filter(
            Q(attempted_username__icontains=user_filter) |
            Q(user__username__icontains=user_filter) |
            Q(user__last_name__icontains=user_filter)
        )
    if date_from:
        qs = qs.filter(timestamp__date__gte=date_from)
    if date_to:
        qs = qs.filter(timestamp__date__lte=date_to)

    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page': page, 'result_filter': result_filter,
        'user_filter': user_filter, 'date_from': date_from, 'date_to': date_to,
        'result_choices': AuthenticationLog.RESULT_CHOICES,
        'total_count': qs.count(),
    }
    return render(request, 'operator/auth_logs.html', ctx)


@operator_required
def system_logs(request):
    """Системный журнал"""
    qs = SystemLog.objects.select_related('user').all()

    level_filter = request.GET.get('level', '')
    component_filter = request.GET.get('component', '').strip()
    if level_filter:
        qs = qs.filter(level=level_filter)
    if component_filter:
        qs = qs.filter(component__icontains=component_filter)

    paginator = Paginator(qs, 30)
    page = paginator.get_page(request.GET.get('page', 1))

    components = SystemLog.objects.values_list('component', flat=True).distinct()

    ctx = {
        'page': page, 'level_filter': level_filter, 'component_filter': component_filter,
        'level_choices': SystemLog.LEVEL_CHOICES, 'components': sorted(set(components)),
    }
    return render(request, 'operator/system_logs.html', ctx)


@operator_required
def users_list(request):
    """Управление пользователями"""
    qs = CustomUser.objects.all()
    search = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '')

    if search:
        qs = qs.filter(
            Q(username__icontains=search) | Q(last_name__icontains=search) |
            Q(first_name__icontains=search) | Q(email__icontains=search)
        )
    if status_filter == 'bio':
        qs = qs.filter(is_biometric_registered=True)
    elif status_filter == 'nobio':
        qs = qs.filter(is_biometric_registered=False)
    elif status_filter == 'locked':
        qs = qs.filter(locked_until__gt=timezone.now())
    elif status_filter == 'inactive':
        qs = qs.filter(is_active=False)

    paginator = Paginator(qs.order_by('last_name', 'first_name'), 20)
    page = paginator.get_page(request.GET.get('page', 1))

    ctx = {'page': page, 'search': search, 'status_filter': status_filter}
    return render(request, 'operator/users_list.html', ctx)


@operator_required
def user_detail(request, user_id):
    """Детальная информация о пользователе"""
    target = get_object_or_404(CustomUser, pk=user_id)
    logs = AuthenticationLog.objects.filter(user=target)[:20]
    last_success = AuthenticationLog.objects.filter(user=target, result='success').first()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'unlock':
            target.reset_failed_attempts()
            write_system_log('INFO', 'Управление пользователями',
                             f'Снята блокировка: {target.username}', user=request.user)
            messages.success(request, 'Блокировка снята.')
        elif action == 'deactivate':
            target.is_active = False
            target.save(update_fields=['is_active'])
            write_system_log('WARNING', 'Управление пользователями',
                             f'Деактивирован: {target.username}', user=request.user)
            messages.warning(request, 'Аккаунт деактивирован.')
        elif action == 'activate':
            target.is_active = True
            target.save(update_fields=['is_active'])
            messages.success(request, 'Аккаунт активирован.')
        elif action == 'delete_biometric':
            try:
                target.biometric_template.delete()
                target.is_biometric_registered = False
                target.save(update_fields=['is_biometric_registered'])
                write_system_log('WARNING', 'Управление биометрией',
                                 f'Удалена биометрия: {target.username}', user=request.user)
                messages.warning(request, 'Биометрические данные удалены.')
            except Exception:
                messages.error(request, 'Ошибка удаления биометрии.')
        elif action == 'edit':
            form = UserEditForm(request.POST, instance=target)
            if form.is_valid():
                form.save()
                write_system_log('INFO', 'Управление пользователями',
                                 f'Изменены данные: {target.username}', user=request.user)
                messages.success(request, 'Данные обновлены.')
            else:
                messages.error(request, 'Ошибка валидации формы.')
        return redirect('user_detail', user_id=user_id)

    edit_form = UserEditForm(instance=target)
    ctx = {'target': target, 'logs': logs, 'last_success': last_success, 'edit_form': edit_form}
    return render(request, 'operator/user_detail.html', ctx)


@operator_required
def reports(request):
    """Отчёты и метрики качества нейросети"""
    period = request.GET.get('period', '7')
    try:
        days = int(period)
        days = max(1, min(days, 365))
    except ValueError:
        days = 7

    now = timezone.now()
    period_from = now - timedelta(days=days)
    logs = AuthenticationLog.objects.filter(timestamp__gte=period_from)

    total = logs.count()
    success = logs.filter(result='success').count()

    metrics = logs.aggregate(
        avg_conf=Avg('recognition_confidence'),
        min_conf=Min('recognition_confidence'),
        max_conf=Max('recognition_confidence'),
        avg_q=Avg('quality_score'),
        min_q=Min('quality_score'),
        max_q=Max('quality_score'),
    )

    cfg = SystemSettings.get_settings()
    above_threshold_conf = logs.filter(recognition_confidence__gte=cfg.recognition_threshold).count()
    above_threshold_qual = logs.filter(quality_score__gte=cfg.quality_threshold).count()

    fail_breakdown = {
        r[0]: r[1]
        for r in logs.values_list('result').annotate(cnt=Count('result'))
                     .values_list('result', 'cnt')
    }

    # Динамика по дням
    daily_data = []
    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).date()
        day_logs = logs.filter(timestamp__date=day)
        daily_data.append({
            'date': day.strftime('%d.%m'),
            'total': day_logs.count(),
            'success': day_logs.filter(result='success').count(),
            'avg_conf': round(day_logs.aggregate(a=Avg('recognition_confidence'))['a'] or 0, 3),
            'avg_qual': round(day_logs.aggregate(a=Avg('quality_score'))['a'] or 0, 3),
        })

    # Последние сохранённые снимки
    snapshots = ModelMetricsSnapshot.objects.all()[:5]

    ctx = {
        'period': days, 'period_from': period_from,
        'total': total, 'success': success,
        'fail': total - success,
        'success_rate': round(success / total * 100, 1) if total else 0,
        'metrics': metrics, 'cfg': cfg,
        'above_threshold_conf': above_threshold_conf,
        'above_threshold_qual': above_threshold_qual,
        'fail_breakdown': fail_breakdown,
        'result_labels': dict(AuthenticationLog.RESULT_CHOICES),
        'daily_data': daily_data,
        'snapshots': snapshots,
        'period_options': [(1,'Сегодня'),(7,'7 дней'),(14,'14 дней'),(30,'30 дней'),(90,'90 дней')],
    }
    return render(request, 'operator/reports.html', ctx)


@operator_required
@require_POST
def save_metrics_snapshot(request):
    """Сохранить снимок метрик"""
    period = int(request.POST.get('period', 7))
    now = timezone.now()
    period_from = now - timedelta(days=period)
    logs = AuthenticationLog.objects.filter(timestamp__gte=period_from)
    total = logs.count()
    success = logs.filter(result='success').count()

    m = logs.aggregate(
        avg_conf=Avg('recognition_confidence'), min_conf=Min('recognition_confidence'),
        max_conf=Max('recognition_confidence'), avg_q=Avg('quality_score'),
        min_q=Min('quality_score'), max_q=Max('quality_score'),
    )
    cfg = SystemSettings.get_settings()
    snap = ModelMetricsSnapshot.objects.create(
        period_from=period_from, period_to=now,
        total_attempts=total, successful_attempts=success, failed_attempts=total - success,
        avg_recognition_confidence=m['avg_conf'] or 0,
        min_recognition_confidence=m['min_conf'] or 0,
        max_recognition_confidence=m['max_conf'] or 0,
        recognition_above_threshold=logs.filter(recognition_confidence__gte=cfg.recognition_threshold).count(),
        avg_quality_score=m['avg_q'] or 0,
        min_quality_score=m['min_q'] or 0,
        max_quality_score=m['max_q'] or 0,
        quality_above_threshold=logs.filter(quality_score__gte=cfg.quality_threshold).count(),
        fail_no_face_count=logs.filter(result='fail_no_face').count(),
        fail_quality_count=logs.filter(result='fail_quality').count(),
        fail_recognition_count=logs.filter(result='fail_recognition').count(),
    )
    write_system_log('INFO', 'Отчёты', f'Сохранён снимок метрик за {period} дней', user=request.user)
    messages.success(request, f'Снимок метрик сохранён (#{snap.pk})')
    return redirect('reports')


@operator_required
def system_settings(request):
    """Настройки системы"""
    cfg = SystemSettings.get_settings()
    errors = {}

    if request.method == 'POST':
        try:
            rec_thr = float(request.POST.get('recognition_threshold', 0.65))
            qual_thr = float(request.POST.get('quality_threshold', 0.35))
            live_thr = float(request.POST.get('liveness_threshold', 0.70))
            max_att = int(request.POST.get('max_auth_attempts', 5))
            lockout = int(request.POST.get('lockout_minutes', 15))
            liveness_en = request.POST.get('liveness_enabled') == 'on'
            use_webhook = request.POST.get('use_webhook') == 'on'
            webhook_url = request.POST.get('webhook_url', '').strip()

            if not 0.1 <= rec_thr <= 1.0:
                errors['recognition_threshold'] = 'Значение 0.1–1.0'
            if not 0.0 <= qual_thr <= 1.0:
                errors['quality_threshold'] = 'Значение 0.0–1.0'
            if not 0.0 <= live_thr <= 1.0:
                errors['liveness_threshold'] = 'Значение 0.0–1.0'
            if not 1 <= max_att <= 20:
                errors['max_auth_attempts'] = 'Значение 1–20'
            if not 1 <= lockout <= 1440:
                errors['lockout_minutes'] = 'Значение 1–1440'
            if use_webhook and not webhook_url:
                errors['webhook_url'] = 'Укажите URL вебхука.'
            if use_webhook and webhook_url and not (
                    webhook_url.startswith('http://') or webhook_url.startswith('https://')
            ):
                errors['webhook_url'] = 'URL должен начинаться с http:// или https://'

            if not errors:
                cfg.recognition_threshold = rec_thr
                cfg.quality_threshold = qual_thr
                cfg.liveness_threshold = live_thr
                cfg.max_auth_attempts = max_att
                cfg.lockout_minutes = lockout
                cfg.liveness_enabled = liveness_en
                cfg.use_webhook = use_webhook
                cfg.webhook_url = webhook_url
                cfg.save()
                write_system_log('INFO', 'Настройки', 'Настройки системы обновлены', user=request.user)
                messages.success(request, 'Настройки сохранены.')
                return redirect('system_settings')
        except (ValueError, TypeError) as e:
            messages.error(request, f'Ошибка валидации: {e}')

    return render(request, 'operator/settings.html', {'cfg': cfg, 'errors': errors})


@operator_required
def door_logs(request):
    """Журнал доступа к домофону"""
    qs = DoorAccessLog.objects.select_related('user').all()

    result_filter = request.GET.get('result', '')
    if result_filter:
        qs = qs.filter(result=result_filter)

    # Статистика за сутки
    from django.utils import timezone
    from datetime import timedelta
    last_24h = timezone.now() - timedelta(hours=24)
    stats_24h = DoorAccessLog.objects.filter(timestamp__gte=last_24h)

    paginator = Paginator(qs, 30)
    page = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page': page,
        'result_filter': result_filter,
        'result_choices': DoorAccessLog.RESULT_CHOICES,
        'total_24h': stats_24h.count(),
        'granted_24h': stats_24h.filter(result='granted').count(),
        'denied_24h': stats_24h.filter(result='denied').count(),
    }
    return render(request, 'operator/door_logs.html', ctx)
