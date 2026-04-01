"""
Скрипт создания тестовых пользователей.
Запустить: python manage.py shell < create_superuser.py
"""
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import CustomUser
from apps.operator.models import SystemSettings
from apps.biometric.models import write_system_log

# Суперпользователь / оператор
if not CustomUser.objects.filter(username='admin').exists():
    u = CustomUser.objects.create_superuser(
        username='admin', password='admin123',
        first_name='Иван', last_name='Администратор',
        is_operator=True
    )
    print(f'✓ Создан администратор: admin / admin123')
else:
    print('admin уже существует')

# Тестовый обычный пользователь
if not CustomUser.objects.filter(username='testuser').exists():
    u = CustomUser.objects.create_user(
        username='testuser', password='test123',
        first_name='Тест', last_name='Пользователь',
        email='test@example.com'
    )
    print(f'✓ Создан тестовый пользователь: testuser / test123')
else:
    print('testuser уже существует')

# Инициализировать настройки системы
cfg = SystemSettings.get_settings()
print(f'✓ Настройки системы инициализированы')
print(f'  Порог распознавания: {cfg.recognition_threshold}')
print(f'  Порог качества: {cfg.quality_threshold}')

write_system_log('INFO', 'Инициализация', 'Система инициализирована. Тестовые пользователи созданы.')
print('✓ Системный журнал инициализирован')
