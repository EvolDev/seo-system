from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

from config import health, user_docs

# Локально — /admin/ и /docs/. На сервере вся система под секретным путём
# (SITE_PATH, ADR-081): админка — в его корне, справка — /<путь>/docs/.
ROOT = settings.URL_ROOT

urlpatterns: list[URLPattern | URLResolver] = [
    # Жив ли сайт — для проверки контейнера и выкладки (E10-01). Изнутри сервера —
    # /healthz, снаружи через Caddy — только под путём системы.
    path("healthz", health.healthz, name="healthz"),
    # Раздел «Документация» — собранный сайт MkDocs, только для вошедших (E9-07).
    # Раньше админки: в корне пути у неё последний адрес ловит всё остальное.
    path(f"{ROOT}docs/", user_docs.page, name="user_docs"),
    path(f"{ROOT}docs/<path:path>", user_docs.page, name="user_docs_page"),
    path(ROOT or "admin/", admin.site.urls),
]
if ROOT:
    urlpatterns.insert(1, path(f"{ROOT}healthz", health.healthz, name="healthz_outside"))

# Панель запросов — только в локальной разработке (config/settings/local.py).
if "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns.append(path("__debug__/", include("debug_toolbar.urls")))
