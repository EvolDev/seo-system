"""Прод: всё из base плюс то, что нельзя забыть выставить (E10-01, ADR-081).

Устройство сервера — `compose.prod.yaml`: Caddy снимает HTTPS и передаёт
запрос gunicorn, статику отдаёт само приложение (WhiteNoise) из образа.
"""

from config.env import env_bool, env_int
from config.settings.base import *  # noqa: F403
from config.settings.base import ALLOWED_HOSTS, DATABASES, MIDDLEWARE

# Отладка в проде раскрывает настройки и код на странице ошибки —
# выключена независимо от DJANGO_DEBUG.
DEBUG = False

# Куки сессии и CSRF — только по HTTPS.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Своё имя куки сессии: куки на localhost общие у всех портов, и тестовая копия
# прода (8003) иначе выкидывала бы из локальной версии (8000). Имя куки CSRF —
# штатное: его читают наши скрипты.
SESSION_COOKIE_NAME = "seo_session"

# HTTPS снимает Caddy, до приложения запрос идёт по внутренней сети Docker.
# Что запрос пришёл по HTTPS, Django узнаёт из заголовка Caddy; снаружи
# приложение недоступно, заголовок подставить некому.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
BEHIND_PROXY = True
# Браузер запоминает «только HTTPS» на этот срок. 0, пока HTTPS на сервере не
# проверен: запомненное не отменить, а на тестовой копии в локальной версии
# (localhost) оно сломало бы локальную версию в этом браузере.
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", default=0)

# Вход — пароль и код из Google Authenticator у всех (ADR-081). Выключить можно
# только явно — например, если сломался вход у всех администраторов сразу.
LOGIN_OTP_REQUIRED = env_bool("LOGIN_OTP_REQUIRED", default=True)

# Проверка контейнера ходит в приложение напрямую, по 127.0.0.1. Снаружи
# такой адрес не придёт: Caddy пропускает только запросы к домену системы.
ALLOWED_HOSTS = [*ALLOWED_HOSTS, "127.0.0.1", "localhost"]

# Статика — из образа: `collectstatic` при сборке, сжатые копии рядом; отдаёт
# WhiteNoise. Имена — те же, что в локальной версии, без отпечатка содержимого
# (ManifestStaticFilesStorage): переходы без перезагрузки узнают скрипты админки
# по пути (`seo/soft-nav.js`, NAV_SIDEBAR, RERUN), и с отпечатком в имени после
# перехода не работали меню и действия (тестовая копия, 09.11.2026). Свежесть
# своих файлов после выкладки даёт `?v=` (config/assets.py).
STATIC_ROOT = BASE_DIR / "staticfiles"  # noqa: F405
MIDDLEWARE = [MIDDLEWARE[0], "whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE[1:]]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Соединение с базой живёт между запросами одного процесса gunicorn, а не
# открывается на каждый; перед повторным использованием проверяется.
DATABASES["default"]["CONN_MAX_AGE"] = 60
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
