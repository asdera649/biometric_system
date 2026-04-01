import os, django, random
from datetime import timedelta
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.utils import timezone
from apps.accounts.models import CustomUser
from apps.biometric.models import AuthenticationLog, SystemLog

admin = CustomUser.objects.get(username='admin')
testuser = CustomUser.objects.get(username='testuser')

results = [
    ('success', 0.82, 0.75), ('success', 0.91, 0.88), ('success', 0.78, 0.70),
    ('fail_recognition', 0.41, 0.65), ('fail_no_face', 0.0, 0.15),
    ('fail_quality', 0.0, 0.18), ('fail_recognition', 0.38, 0.72),
    ('success', 0.85, 0.80), ('success', 0.76, 0.68),
    ('fail_recognition', 0.44, 0.61),
]

now = timezone.now()
for i, (result, conf, qual) in enumerate(results * 3):
    ts = now - timedelta(hours=random.randint(0, 23), minutes=random.randint(0, 59))
    user = random.choice([admin, testuser])
    AuthenticationLog.objects.create(
        user=user, attempted_username=user.username,
        result=result, recognition_confidence=conf * 100,
        quality_score=qual * 100,
        failure_reason='' if result == 'success' else 'Демонстрационная запись',
        ip_address='127.0.0.1',
        timestamp=ts
    )

for msg in [
    ('INFO', 'Аутентификация', 'Инициализация системы распознавания лиц'),
    ('INFO', 'Регистрация биометрии', 'Тестовые шаблоны загружены'),
    ('WARNING', 'Аутентификация', 'Серия неудачных попыток — IP: 192.168.1.105'),
    ('INFO', 'Настройки', 'Параметры системы обновлены оператором'),
    ('ERROR', 'FaceProcessor', 'Ошибка обработки кадра: некорректный формат изображения'),
    ('INFO', 'Управление пользователями', 'Зарегистрирован новый пользователь'),
]:
    SystemLog.objects.create(level=msg[0], component=msg[1], message=msg[2])

print(f'✓ Создано {AuthenticationLog.objects.count()} записей журнала аутентификации')
print(f'✓ Создано {SystemLog.objects.count()} системных логов')
