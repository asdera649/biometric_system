from django.db import models
from django.conf import settings


class AuthenticationLog(models.Model):
    """Журнал попыток аутентификации"""
    RESULT_SUCCESS = 'success'
    RESULT_FAIL_QUALITY = 'fail_quality'
    RESULT_FAIL_NO_FACE = 'fail_no_face'
    RESULT_FAIL_RECOGNITION = 'fail_recognition'
    RESULT_FAIL_LIVENESS = 'fail_liveness'
    RESULT_FAIL_LOCKED = 'fail_locked'
    RESULT_CHOICES = [
        (RESULT_SUCCESS, 'Успех'),
        (RESULT_FAIL_QUALITY, 'Низкое качество изображения'),
        (RESULT_FAIL_NO_FACE, 'Лицо не обнаружено'),
        (RESULT_FAIL_RECOGNITION, 'Лицо не распознано'),
        (RESULT_FAIL_LIVENESS, 'Проверка живости не пройдена'),
        (RESULT_FAIL_LOCKED, 'Аккаунт заблокирован'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_logs', verbose_name='Пользователь'
    )
    attempted_username = models.CharField('Введённый логин', max_length=150, blank=True)
    timestamp = models.DateTimeField('Время', auto_now_add=True, db_index=True)
    result = models.CharField('Результат', max_length=30, choices=RESULT_CHOICES)
    recognition_confidence = models.FloatField('Уверенность распознавания', default=0.0)
    quality_score = models.FloatField('Оценка качества', default=0.0)
    liveness_score = models.FloatField('Оценка живости (заглушка)', null=True, blank=True)
    failure_reason = models.TextField('Причина отказа', null=True, blank=True)
    ip_address = models.GenericIPAddressField('IP-адрес', null=True, blank=True)
    user_agent = models.CharField('User-Agent', max_length=500, blank=True)

    class Meta:
        verbose_name = 'Журнал аутентификации'
        verbose_name_plural = 'Журнал аутентификации'
        ordering = ['-timestamp']

    def is_success(self):
        return self.result == self.RESULT_SUCCESS

    def __str__(self):
        return f'{self.timestamp:%Y-%m-%d %H:%M} | {self.get_result_display()}'


class SystemLog(models.Model):
    """Системный журнал"""
    LEVEL_INFO = 'INFO'
    LEVEL_WARNING = 'WARNING'
    LEVEL_ERROR = 'ERROR'
    LEVEL_CHOICES = [
        (LEVEL_INFO, 'Информация'),
        (LEVEL_WARNING, 'Предупреждение'),
        (LEVEL_ERROR, 'Ошибка'),
    ]

    timestamp = models.DateTimeField('Время', auto_now_add=True, db_index=True)
    level = models.CharField('Уровень', max_length=10, choices=LEVEL_CHOICES, default=LEVEL_INFO)
    component = models.CharField('Компонент', max_length=100)
    message = models.TextField('Сообщение')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name='Пользователь'
    )
    extra_data = models.JSONField('Доп. данные', null=True, blank=True)

    class Meta:
        verbose_name = 'Системный лог'
        verbose_name_plural = 'Системные логи'
        ordering = ['-timestamp']

    def __str__(self):
        return f'[{self.level}] {self.timestamp:%Y-%m-%d %H:%M} | {self.component}: {self.message[:60]}'


def write_system_log(level, component, message, user=None, extra_data=None):
    """Утилита записи системного лога"""
    SystemLog.objects.create(
        level=level, component=component,
        message=message, user=user, extra_data=extra_data
    )

class DoorAccessLog(models.Model):
    """Журнал доступа через домофон"""
    RESULT_GRANTED = 'granted'
    RESULT_DENIED = 'denied'
    RESULT_NO_FACE = 'no_face'
    RESULT_CHOICES = [
        (RESULT_GRANTED, 'Доступ разрешён'),
        (RESULT_DENIED, 'Доступ запрещён'),
        (RESULT_NO_FACE, 'Лицо не обнаружено'),
    ]

    timestamp = models.DateTimeField('Время', auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='door_logs', verbose_name='Пользователь'
    )
    result = models.CharField('Результат', max_length=20, choices=RESULT_CHOICES)
    recognition_confidence = models.FloatField('Уверенность', default=0.0)
    quality_score = models.FloatField('Качество кадра', default=0.0)
    door_opened = models.BooleanField('Дверь открыта', default=False)
    source_ip = models.GenericIPAddressField('IP сервиса', null=True, blank=True)
    snapshot = models.ImageField(
        'Снимок кадра', upload_to='door_snapshots/', blank=True
    )

    class Meta:
        verbose_name = 'Доступ к домофону'
        verbose_name_plural = 'Доступ к домофону'
        ordering = ['-timestamp']

    def __str__(self):
        who = self.user.username if self.user else '—'
        return f'{self.timestamp:%Y-%m-%d %H:%M} | {who} | {self.get_result_display()}'