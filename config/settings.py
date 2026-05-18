import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = 'django-insecure-biometric-system-dev-key-change-in-production'
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'crispy_forms',
    'crispy_bootstrap5',
    'rest_framework',
    'corsheaders',
    'apps.accounts',
    'apps.biometric',
    'apps.operator',
    'apps.liveness.apps.LivenessConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_USER_MODEL = 'accounts.CustomUser'
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 6}},
]

LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Crispy Forms
CRISPY_ALLOWED_TEMPLATE_PACKS = 'bootstrap5'
CRISPY_TEMPLATE_PACK = 'bootstrap5'

# Django REST
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.JSONParser',
    ],
    'EXCEPTION_HANDLER': 'apps.liveness.exceptions.custom_exception_handler',
}

# CORS
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = os.environ.get('CORS_ALLOWED_ORIGINS', '').split() or []

# Liveness Detection
# Операционные настройки (пороги, включить/выключить) хранятся в БД
# в модели SystemSettings и управляются оператором через /operator/settings/.
# Здесь только то, что нельзя менять без перезапуска сервера:
# пути к файлам моделей, устройство, лимит загрузки.

LIVENESS_CONFIG = {
    # Загружать ли сервис при старте Django.
    # False полностью отключает модуль на уровне кода.
    # Оперативное включение/выключение делается через SystemSettings.liveness_enabled.
    'ENABLED': True,

    # Папка с .pth-файлами MiniFASNet
    'MODEL_DIR': os.environ.get(
        'ANTI_SPOOF_MODEL_DIR',
        str(BASE_DIR / 'resources' / 'anti_spoof_models'),
    ),

    # Детектор лиц (Caffe RetinaFace)
    'DETECTOR_CAFFEMODEL': os.environ.get(
        'DETECTOR_CAFFEMODEL',
        str(BASE_DIR / 'resources' / 'detection_model' / 'Widerface-RetinaFace.caffemodel'),
    ),
    'DETECTOR_PROTOTXT': os.environ.get(
        'DETECTOR_PROTOTXT',
        str(BASE_DIR / 'resources' / 'detection_model' / 'deploy.prototxt'),
    ),

    # GPU device id; при недоступной CUDA автоматически падает на CPU
    'DEVICE_ID': int(os.environ.get('LIVENESS_DEVICE_ID', '0')),

    # Максимальный размер загружаемого изображения
    'MAX_IMAGE_MB': int(os.environ.get('LIVENESS_MAX_IMAGE_MB', '10')),
}

DATA_UPLOAD_MAX_MEMORY_SIZE = LIVENESS_CONFIG['MAX_IMAGE_MB'] * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {module} {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django':       {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'apps':         {'handlers': ['console'], 'level': 'INFO',    'propagate': False},
    },
}