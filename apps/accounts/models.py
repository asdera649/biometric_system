from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class CustomUser(AbstractUser):
    """Расширенная модель пользователя"""
    patronymic = models.CharField('Отчество', max_length=150, blank=True)
    phone = models.CharField('Телефон', max_length=20, blank=True)
    is_operator = models.BooleanField('Оператор', default=False)
    is_biometric_registered = models.BooleanField('Биометрия зарегистрирована', default=False)
    failed_attempts = models.PositiveIntegerField('Неудачных попыток', default=0)
    locked_until = models.DateTimeField('Заблокирован до', null=True, blank=True)
    created_at = models.DateTimeField('Дата регистрации', auto_now_add=True)

    class Meta:
        verbose_name = 'Пользователь'
        verbose_name_plural = 'Пользователи'

    def get_full_name_ru(self):
        parts = [self.last_name, self.first_name, self.patronymic]
        return ' '.join(p for p in parts if p) or self.username

    def is_locked(self):
        if self.locked_until and self.locked_until > timezone.now():
            return True
        return False

    def reset_failed_attempts(self):
        self.failed_attempts = 0
        self.locked_until = None
        self.save(update_fields=['failed_attempts', 'locked_until'])

    def __str__(self):
        return self.get_full_name_ru()


class BiometricTemplate(models.Model):
    """Биометрический шаблон пользователя"""
    user = models.OneToOneField(
        CustomUser, on_delete=models.CASCADE,
        related_name='biometric_template', verbose_name='Пользователь'
    )
    embedding = models.TextField('Вектор признаков (JSON)')  # JSON array of 512 floats
    detection_confidence = models.FloatField('Уверенность детекции', default=0.0)
    image_quality_score = models.FloatField('Оценка качества изображения', default=0.0)
    face_image = models.ImageField('Фото лица', upload_to='biometric_faces/', blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    class Meta:
        verbose_name = 'Биометрический шаблон'
        verbose_name_plural = 'Биометрические шаблоны'

    def __str__(self):
        return f'Шаблон [{self.user}]'
