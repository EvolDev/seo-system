from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

from config import user_docs

urlpatterns: list[URLPattern | URLResolver] = [
    path("admin/", admin.site.urls),
    # Раздел «Документация» — собранный сайт MkDocs, только для вошедших (E9-07).
    path("docs/", user_docs.page, name="user_docs"),
    path("docs/<path:path>", user_docs.page, name="user_docs_page"),
]

# Панель запросов — только в локальной разработке (config/settings/local.py).
if "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns.append(path("__debug__/", include("debug_toolbar.urls")))
