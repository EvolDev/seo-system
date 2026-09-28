"""Общие настройки Django для всех окружений.

Всё, что различается между машинами, — из переменных окружения
(`docs/13-CONFIG.md` §1). Модуль окружения (`local.py`, `prod.py`)
импортирует отсюда всё и дописывает своё; выбирает его `DJANGO_ENV`
через `config.env.settings_module()`.
"""

from pathlib import Path

from celery.schedules import crontab

from config.env import env_bool, env_list, env_str
from config.logs import logging_config
from config.sentry import init_sentry

# Корень проекта: config/settings/base.py → три уровня вверх.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = env_str("DJANGO_SECRET_KEY")
DEBUG = env_bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")

INSTALLED_APPS = [
    # Тема админки Admin Interface (ADR-038): до django.contrib.admin, чтобы
    # её шаблоны перекрыли штатные; colorfield — поля цветов её модели темы.
    "admin_interface",
    "colorfield",
    # Штатная админка со своим сайтом: меню по работе человека (config/admin_site.py).
    "config.admin_site.SeoAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Поля Postgres (ArrayField) и их формы в админке.
    "django.contrib.postgres",
    # Фильтр диапазона в списках админки: поля «С» и «До» (DR, трафик).
    "rangefilter",
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
        # Шаблоны уровня проекта: переопределения админки (шапка, расцветки).
        "DIRS": [BASE_DIR / "config" / "templates"],
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
# Интерфейс только на русском. С одним языком тема админки не показывает
# свой переключатель языков (ADR-038).
LANGUAGES = [("ru", "Русский")]
TIME_ZONE = env_str("TIME_ZONE", default="Europe/Moscow")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Статика уровня проекта: расцветки админки (ADR-038).
STATICFILES_DIRS = [BASE_DIR / "config" / "static"]

# В schema.sql первичные ключи — bigserial.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Очереди: Celery + Redis (E2-01, ADR-039) ---
# Celery читает отсюда всё, что начинается с CELERY_ (config/celery.py).
CELERY_BROKER_URL = env_str("REDIS_URL")
# Задача выполняется сразу, в том же процессе, без воркера и Redis. В тестах
# включено всегда (tests/conftest.py); локально — отлаживать без воркера.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", default=False)
# Ошибку задачи в этом режиме не поднимать там, где задачу поставили: иначе
# Celery выбрасывает наружу и сигнал повтора, и повторы не работают. Итог
# задачи — в журнале запусков, её ошибку поднимет `.get()` у результата.
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
# Результаты задач Celery не хранит: итог задачи — строки в базе (task_runs, checks).
CELERY_TASK_IGNORE_RESULT = True
# Сообщение удаляется из Redis только после завершения задачи: воркер
# остановили посреди задачи — она осталась в очереди и выполнится снова.
CELERY_TASK_ACKS_LATE = True
# Процесс воркера убит посреди задачи (нехватка памяти, жёсткий лимит) —
# задачу в очередь не возвращаем: иначе та, что роняет процесс, шла бы по кругу.
CELERY_TASK_REJECT_ON_WORKER_LOST = False
# Воркер берёт задачи по одной: остальные ждут в Redis, а не в его памяти.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Не подтверждённое за это время сообщение Redis отдаёт снова — так
# возвращаются задачи жёстко убитого воркера. Больше самой долгой задачи и
# самой длинной паузы перед повтором (config/queue.py), иначе задача
# выполнится дважды.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 60 * 60}
# Пропала связь с Redis — задачи с поздним подтверждением отменяются: Redis
# всё равно отдаст их снова (в Celery 6 так будет по умолчанию).
CELERY_WORKER_CANCEL_LONG_RUNNING_TASKS_ON_CONNECTION_LOSS = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Зависшая задача (запрос без таймаута) иначе навсегда займёт процесс воркера.
# Мягкий лимит — исключение внутри задачи: повтор и алерт, как у любой ошибки;
# жёсткий убивает процесс, если задача не остановилась сама. Задача может
# задать свои лимиты (soft_time_limit, time_limit).
CELERY_TASK_SOFT_TIME_LIMIT = 10 * 60
CELERY_TASK_TIME_LIMIT = 12 * 60
# Логи — через LOGGING ниже (config/celery.py), print из задач — в stdout как есть.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_WORKER_REDIRECT_STDOUTS = False
# Расписание beat — docs/13-CONFIG.md §3; время — в TIME_ZONE.
CELERY_BEAT_SCHEDULE = {
    # Пульс очереди: строка в «Запусках задач» раз в час. Нет строк — не
    # работает воркер или beat.
    "heartbeat": {
        "task": "heartbeat",
        "schedule": crontab(minute=0),
        # Пропущенный пульс (воркер лежал) не копится: выбрасывается до следующего.
        "options": {"expires": 50 * 60},
    },
}

# Технические логи — в stdout, с run_id и маскировкой секретов (ADR-011).
# В проде JSON для grep и jq; local.py переключает на читаемый формат.
LOGGING = logging_config("json")

# Ошибки — в Sentry (ADR-013); пустой SENTRY_DSN — выключен.
init_sentry(env_str("SENTRY_DSN", default=""), environment=env_str("DJANGO_ENV"))

# Admin Interface открывает окно «добавить связанный объект» во фрейме той же
# страницы (ADR-038): фреймы разрешаем только своему сайту. W019 — проверка
# `check --deploy`, которая требует DENY.
X_FRAME_OPTIONS = "SAMEORIGIN"
SILENCED_SYSTEM_CHECKS = ["security.W019"]
