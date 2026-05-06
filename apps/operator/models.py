"""
apps/operator/models.py
"""
from django.db import models


class SystemSettings(models.Model):
    """Настройки системы (singleton, pk=1)"""

    # Распознавание
    recognition_threshold = models.FloatField(
        'Порог распознавания', default=0.65,
        help_text='0.0–1.0. Минимальная схожесть для успешной аутентификации.'
    )
    quality_threshold = models.FloatField(
        'Порог качества изображения', default=0.35,
        help_text='0.0–1.0. Минимальное качество лица для обработки.'
    )
    max_auth_attempts = models.PositiveIntegerField(
        'Макс. попыток входа', default=5,
        help_text='После превышения — блокировка аккаунта.'
    )
    lockout_minutes = models.PositiveIntegerField(
        'Время блокировки (мин)', default=15
    )

    # Liveness detection
    liveness_enabled = models.BooleanField(
        'Детекция живости включена', default=False,
        help_text='Включить гибридную проверку живости (пассивная + активный challenge).'
    )
    liveness_passive_accept = models.FloatField(
        'Порог пассивного принятия', default=0.68,
        help_text='0.5–0.95. Score выше — принять без challenge. Рекомендуется: 0.68.'
    )
    liveness_passive_reject = models.FloatField(
        'Порог пассивного отказа', default=0.22,
        help_text='0.05–0.49. Score ниже — жёсткий отказ. Рекомендуется: 0.22.'
    )
    liveness_identity_threshold = models.FloatField(
        'Порог верификации личности (same-person)', default=0.52,
        help_text='0.3–0.9. Мин. косинусная схожесть во время challenge. Рекомендуется: 0.50–0.55.'
    )
    liveness_challenge_timeout = models.PositiveIntegerField(
        'Таймаут challenge (сек)', default=45,
        help_text='15–120. Максимальное время на выполнение активного задания.'
    )

    # Устаревшее поле (оставлено для совместимости с миграциями)
    # liveness_threshold = models.FloatField(
    #     'Порог живости (legacy)', default=0.70,
    #     help_text='Зарезервировано. Используйте liveness_passive_accept / reject.'
    # )

    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Настройки системы'
        verbose_name_plural = 'Настройки системы'

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return 'Настройки системы'


class ModelMetricsSnapshot(models.Model):
    """Снимок метрик качества нейросети"""
    recorded_at  = models.DateTimeField('Записано', auto_now_add=True)
    period_from  = models.DateTimeField('Период с')
    period_to    = models.DateTimeField('Период по')

    total_attempts      = models.PositiveIntegerField('Всего попыток', default=0)
    successful_attempts = models.PositiveIntegerField('Успешных', default=0)
    failed_attempts     = models.PositiveIntegerField('Неуспешных', default=0)

    avg_recognition_confidence   = models.FloatField('Ср. уверенность распознавания', default=0.0)
    min_recognition_confidence   = models.FloatField('Мин. уверенность распознавания', default=0.0)
    max_recognition_confidence   = models.FloatField('Макс. уверенность распознавания', default=0.0)
    recognition_above_threshold  = models.PositiveIntegerField('Выше порога распознавания', default=0)

    avg_quality_score    = models.FloatField('Ср. оценка качества', default=0.0)
    min_quality_score    = models.FloatField('Мин. оценка качества', default=0.0)
    max_quality_score    = models.FloatField('Макс. оценка качества', default=0.0)
    quality_above_threshold = models.PositiveIntegerField('Выше порога качества', default=0)

    liveness_available  = models.BooleanField('Данные живости доступны', default=False)
    avg_liveness_score  = models.FloatField('Ср. оценка живости', null=True, blank=True)

    fail_no_face_count    = models.PositiveIntegerField('Лицо не обнаружено', default=0)
    fail_quality_count    = models.PositiveIntegerField('Низкое качество', default=0)
    fail_recognition_count = models.PositiveIntegerField('Не распознан', default=0)
    fail_liveness_count   = models.PositiveIntegerField('Liveness отказ', default=0)

    class Meta:
        verbose_name = 'Снимок метрик'
        verbose_name_plural = 'Снимки метрик'
        ordering = ['-recorded_at']

    @property
    def success_rate(self):
        if self.total_attempts == 0:
            return 0.0
        return round(self.successful_attempts / self.total_attempts * 100, 1)

    def __str__(self):
        return f'Метрики {self.recorded_at:%Y-%m-%d %H:%M}'
