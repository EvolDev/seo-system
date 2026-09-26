"""Общие настройки Django для всех окружений.

Всё, что различается между машинами, — из переменных окружения
(`docs/13-CONFIG.md` §1). Модуль окружения (`local.py`, `prod.py`)
импортирует отсюда всё и дописывает своё; выбирает его `DJANGO_ENV`
через `config.env.settings_module()`.
"""

from pathlib import Path

from config.env import env_bool, env_list, env_str
from config.logs import logging_config
from config.sentry import init_sentry

# Корень проекта: config/settings/base.py → три уровня вверх.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = env_str("DJANGO_SECRET_KEY")
DEBUG = env_bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Свои приложения — по доменам, не по слоям (ADR-024).
    "apps.sites",
    "apps.placements",
    "apps.keywords",
    "apps.content",
    "apps.observability",
    "apps.integrations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env_str("POSTGRES_HOST"),
        "PORT": env_str("POSTGRES_PORT", default="5432"),
        "NAME": env_str("POSTGRES_DB"),
        "USER": env_str("POSTGRES_USER"),
        "PASSWORD": env_str("POSTGRES_PASSWORD"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = env_str("TIME_ZONE", default="Europe/Moscow")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# В schema.sql первичные ключи — bigserial.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Технические логи — в stdout, с run_id и маскировкой секретов (ADR-011).
# В проде JSON для grep и jq; local.py переключает на читаемый формат.
LOGGING = logging_config("json")

# Ошибки — в Sentry (ADR-013); пустой SENTRY_DSN — выключен.
init_sentry(env_str("SENTRY_DSN", default=""), environment=env_str("DJANGO_ENV"))
