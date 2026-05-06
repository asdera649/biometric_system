"""
apps/operator/views.py
"""

import logging
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.db.models import Count, Avg, Min, Max, Q
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST

from apps.accounts.models import CustomUser
from apps.biometric.models import AuthenticationLog, SystemLog, write_system_log
from .models import SystemSettings, ModelMetricsSnapshot
from apps.accounts.forms import UserEditForm

logger = logging.getLogger('apps.operator')


def operator_required(view_func):
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_operator and not request.user.is_superuser:
            messages.error(request, 'Доступ запрещён. Требуются права оператора.')
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return wrapped


@operator_required
def dashboard(request):
    now = timezone.now()
    last_24h = now - timedelta(hours=24)

    logs_24h   = AuthenticationLog.objects.filter(timestamp__gte=last_24h)
    total_24h  = logs_24h.count()
    success_24h = logs_24h.filter(result='success').count()
    fail_24h   = total_24h - success_24h

    total_users  = CustomUser.objects.filter(is_active=True).count()
    bio_users    = CustomUser.objects.filter(is_biometric_registered=True).count()
    locked_users = CustomUser.objects.filter(locked_until__gt=now).count()

    hourly_data = []
    for i in range(23, -1, -1):
        hour_start = now - timedelta(hours=i + 1)
        hour_end   = now - timedelta(hours=i)
        cnt = AuthenticationLog.objects.filter(
            timestamp__gte=hour_start, timestamp__lt=hour_end
        ).count()
        hourly_data.append({'hour': hour_end.strftime('%H:00'), 'count': cnt})

    recent_logs        = AuthenticationLog.objects.select_related('user')[:10]
    recent_system_logs = SystemLog.objects.all()[:5]

    avg_metrics = logs_24h.aggregate(
        avg_conf=Avg('recognition_confidence'),
        avg_quality=Avg('quality_score'),
    )

    ctx = {
        'total_24h': total_24h, 'success_24h': success_24h, 'fail_24h': fail_24h,
        'total_users': total_users, 'bio_users': bio_users, 'locked_users': locked_users,
        'success_rate': round(success_24h / total_24h * 100, 1) if total_24h else 0,
        'hourly_data':        hourly_data,
        'recent_logs':        recent_logs,
        'recent_system_logs': recent_system_logs,
        'avg_recognition':    (avg_metrics['avg_conf']    or 0) * 100,
        'avg_quality':        (avg_metrics['avg_quality'] or 0) * 100,
    }
    return render(request, 'operator/dashboard.html', ctx)


@operator_required
def auth_logs(request):
    qs = AuthenticationLog.objects.select_related('user').all()

    result_filter = request.GET.get('result', '')
    user_filter   = request.GET.get('username', '').strip()
    date_from     = request.GET.get('date_from', '')
    date_to       = request.GET.get('date_to', '')

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
    page      = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page': page, 'result_filter': result_filter,
        'user_filter': user_filter, 'date_from': date_from, 'date_to': date_to,
        'result_choices': AuthenticationLog.RESULT_CHOICES,
        'total_count': qs.count(),
    }
    return render(request, 'operator/auth_logs.html', ctx)


@operator_required
def system_logs(request):
    qs = SystemLog.objects.select_related('user').all()

    level_filter     = request.GET.get('level', '')
    component_filter = request.GET.get('component', '').strip()
    if level_filter:
        qs = qs.filter(level=level_filter)
    if component_filter:
        qs = qs.filter(component__icontains=component_filter)

    paginator  = Paginator(qs, 30)
    page       = paginator.get_page(request.GET.get('page', 1))
    components = SystemLog.objects.values_list('component', flat=True).distinct()

    ctx = {
        'page': page, 'level_filter': level_filter,
        'component_filter': component_filter,
        'level_choices': SystemLog.LEVEL_CHOICES,
        'components': sorted(set(components)),
    }
    return render(request, 'operator/system_logs.html', ctx)


@operator_required
def users_list(request):
    qs     = CustomUser.objects.all()
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
    page      = paginator.get_page(request.GET.get('page', 1))

    ctx = {'page': page, 'search': search, 'status_filter': status_filter}
    return render(request, 'operator/users_list.html', ctx)


@operator_required
def user_detail(request, user_id):
    target      = get_object_or_404(CustomUser, pk=user_id)
    logs        = AuthenticationLog.objects.filter(user=target)[:20]
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
    ctx = {
        'target': target, 'logs': logs,
        'last_success': last_success, 'edit_form': edit_form,
    }
    return render(request, 'operator/user_detail.html', ctx)


@operator_required
def reports(request):
    period = request.GET.get('period', '7')
    try:
        days = max(1, min(int(period), 365))
    except ValueError:
        days = 7

    now         = timezone.now()
    period_from = now - timedelta(days=days)
    logs        = AuthenticationLog.objects.filter(timestamp__gte=period_from)

    total   = logs.count()
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
    above_threshold_conf = logs.filter(
        recognition_confidence__gte=cfg.recognition_threshold
    ).count()
    above_threshold_qual = logs.filter(
        quality_score__gte=cfg.quality_threshold
    ).count()

    fail_breakdown = {
        r[0]: r[1]
        for r in logs.values_list('result').annotate(cnt=Count('result'))
                     .values_list('result', 'cnt')
    }

    daily_data = []
    for i in range(days - 1, -1, -1):
        day      = (now - timedelta(days=i)).date()
        day_logs = logs.filter(timestamp__date=day)
        daily_data.append({
            'date':     day.strftime('%d.%m'),
            'total':    day_logs.count(),
            'success':  day_logs.filter(result='success').count(),
            'avg_conf': round(day_logs.aggregate(a=Avg('recognition_confidence'))['a'] or 0, 3),
            'avg_qual': round(day_logs.aggregate(a=Avg('quality_score'))['a'] or 0, 3),
        })

    snapshots = ModelMetricsSnapshot.objects.all()[:5]

    ctx = {
        'period': days, 'period_from': period_from,
        'total': total, 'success': success,
        'fail': total - success,
        'success_rate': round(success / total * 100, 1) if total else 0,
        'metrics': metrics, 'cfg': cfg,
        'above_threshold_conf': above_threshold_conf,
        'above_threshold_qual': above_threshold_qual,
        'fail_breakdown':  fail_breakdown,
        'result_labels':   dict(AuthenticationLog.RESULT_CHOICES),
        'daily_data':      daily_data,
        'snapshots':       snapshots,
        'period_options':  [(1,'Сегодня'),(7,'7 дней'),(14,'14 дней'),(30,'30 дней'),(90,'90 дней')],
    }
    return render(request, 'operator/reports.html', ctx)


@operator_required
@require_POST
def save_metrics_snapshot(request):
    period = int(request.POST.get('period', 7))
    now         = timezone.now()
    period_from = now - timedelta(days=period)
    logs        = AuthenticationLog.objects.filter(timestamp__gte=period_from)
    total   = logs.count()
    success = logs.filter(result='success').count()

    m   = logs.aggregate(
        avg_conf=Avg('recognition_confidence'), min_conf=Min('recognition_confidence'),
        max_conf=Max('recognition_confidence'), avg_q=Avg('quality_score'),
        min_q=Min('quality_score'), max_q=Max('quality_score'),
    )
    cfg = SystemSettings.get_settings()
    snap = ModelMetricsSnapshot.objects.create(
        period_from=period_from, period_to=now,
        total_attempts=total, successful_attempts=success,
        failed_attempts=total - success,
        avg_recognition_confidence=m['avg_conf'] or 0,
        min_recognition_confidence=m['min_conf'] or 0,
        max_recognition_confidence=m['max_conf'] or 0,
        recognition_above_threshold=logs.filter(
            recognition_confidence__gte=cfg.recognition_threshold).count(),
        avg_quality_score=m['avg_q'] or 0,
        min_quality_score=m['min_q'] or 0,
        max_quality_score=m['max_q'] or 0,
        quality_above_threshold=logs.filter(
            quality_score__gte=cfg.quality_threshold).count(),
        fail_no_face_count=logs.filter(result='fail_no_face').count(),
        fail_quality_count=logs.filter(result='fail_quality').count(),
        fail_recognition_count=logs.filter(result='fail_recognition').count(),
        fail_liveness_count=logs.filter(result='fail_liveness').count(),
        liveness_available=logs.filter(liveness_score__isnull=False).exists(),
    )
    write_system_log('INFO', 'Отчёты',
                     f'Сохранён снимок метрик за {period} дней', user=request.user)
    messages.success(request, f'Снимок метрик сохранён (#{snap.pk})')
    return redirect('reports')


@operator_required
def system_settings(request):
    cfg    = SystemSettings.get_settings()
    errors = {}

    if request.method == 'POST':
        try:
            rec_thr  = float(request.POST.get('recognition_threshold', 0.65))
            qual_thr = float(request.POST.get('quality_threshold', 0.35))
            max_att  = int(request.POST.get('max_auth_attempts', 5))
            lockout  = int(request.POST.get('lockout_minutes', 15))
            liveness_en = request.POST.get('liveness_enabled') == 'on'

            # Liveness-поля
            pa_accept  = float(request.POST.get('liveness_passive_accept', 0.68))
            pa_reject  = float(request.POST.get('liveness_passive_reject', 0.22))
            id_thresh  = float(request.POST.get('liveness_identity_threshold', 0.52))
            ch_timeout = int(request.POST.get('liveness_challenge_timeout', 45))

            # Валидация
            if not 0.1 <= rec_thr <= 1.0:
                errors['recognition_threshold'] = 'Значение 0.1–1.0'
            if not 0.0 <= qual_thr <= 1.0:
                errors['quality_threshold'] = 'Значение 0.0–1.0'
            if not 1 <= max_att <= 20:
                errors['max_auth_attempts'] = 'Значение 1–20'
            if not 1 <= lockout <= 1440:
                errors['lockout_minutes'] = 'Значение 1–1440'
            if not 0.5 <= pa_accept <= 0.95:
                errors['liveness_passive_accept'] = 'Значение 0.5–0.95'
            if not 0.05 <= pa_reject <= 0.49:
                errors['liveness_passive_reject'] = 'Значение 0.05–0.49'
            if pa_reject >= pa_accept:
                errors['liveness_passive_reject'] = 'Порог отказа должен быть меньше порога принятия'
            if not 0.3 <= id_thresh <= 0.9:
                errors['liveness_identity_threshold'] = 'Значение 0.3–0.9'
            if not 15 <= ch_timeout <= 120:
                errors['liveness_challenge_timeout'] = 'Значение 15–120'

            if not errors:
                cfg.recognition_threshold       = rec_thr
                cfg.quality_threshold           = qual_thr
                cfg.max_auth_attempts           = max_att
                cfg.lockout_minutes             = lockout
                cfg.liveness_enabled            = liveness_en
                cfg.liveness_passive_accept     = pa_accept
                cfg.liveness_passive_reject     = pa_reject
                cfg.liveness_identity_threshold = id_thresh
                cfg.liveness_challenge_timeout  = ch_timeout
                cfg.save()

                _sync_pipeline_thresholds(cfg)

                write_system_log('INFO', 'Настройки',
                                 'Настройки системы обновлены', user=request.user)
                messages.success(request, 'Настройки сохранены.')
                return redirect('system_settings')

        except (ValueError, TypeError) as e:
            messages.error(request, f'Ошибка валидации: {e}')

    return render(request, 'operator/settings.html', {'cfg': cfg, 'errors': errors})


# Утилита: синхронизация pipeline с БД

def _sync_pipeline_thresholds(cfg: SystemSettings) -> None:
    """
    Обновляет пороги в HybridLivenessPipeline при изменении настроек.
    Не требует перезапуска сервера — pipeline является синглтоном.
    """
    try:
        from apps.biometric.liveness.pipeline import get_pipeline
        from apps.biometric.liveness.session import CHALLENGE_TIMEOUT_SEC
        import apps.biometric.liveness.session as _sess_mod
        import apps.biometric.liveness.pipeline as _pipe_mod

        pipeline = get_pipeline()
        pipeline.PASSIVE_ACCEPT_THRESHOLD  = cfg.liveness_passive_accept
        pipeline.PASSIVE_REJECT_THRESHOLD  = cfg.liveness_passive_reject
        pipeline.IDENTITY_SIM_THRESHOLD    = cfg.liveness_identity_threshold

        # Таймаут сессии — патчим константу модуля
        _sess_mod.CHALLENGE_TIMEOUT_SEC = cfg.liveness_challenge_timeout

        logger.info(
            f'Pipeline thresholds synced: '
            f'accept={cfg.liveness_passive_accept} '
            f'reject={cfg.liveness_passive_reject} '
            f'identity={cfg.liveness_identity_threshold} '
            f'timeout={cfg.liveness_challenge_timeout}s'
        )
    except Exception as e:
        logger.error(f'_sync_pipeline_thresholds error: {e}')
