from django.apps import AppConfig
import logging

logger = logging.getLogger('apps.liveness')


class LivenessConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.liveness'
    verbose_name = 'Liveness Detection'

    def ready(self):
        """
        Предзагрузка моделей при старте Django, чтобы первый запрос
        не платил холодный старт.
        Пропускается при management-командах, не требующих модели.
        """
        import sys
        skip_commands = ('migrate', 'collectstatic', 'makemigrations', 'shell',
                         'createsuperuser', 'dbshell', 'inspectdb')
        if any(cmd in sys.argv for cmd in skip_commands):
            return

        from django.conf import settings
        cfg = getattr(settings, 'LIVENESS_CONFIG', {})
        if not cfg.get('ENABLED', True):
            logger.info('Liveness service disabled via LIVENESS_CONFIG["ENABLED"].')
            return

        try:
            from apps.liveness.service import LivenessService
            service = LivenessService.get_instance()
            logger.info(
                'Liveness service ready — %d model(s) loaded on %s',
                len(service.loaded_models),
                service.device,
            )
        except Exception as exc:
            # Не роняем Django при старте; view вернёт 503 если сервис недоступен
            logger.error('Failed to initialize LivenessService: %s', exc)