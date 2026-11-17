"""Экран «Плагины» (Работа → Плагины, ADR-084) и скачивание архива расширения.

Видят все, кто видит площадки (`sites.view_site`): расширение нужно всем, кто
проверяет серость, а записывает замер всё равно карточка под входом человека.
"""

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.urls import reverse

from apps.sites import plugins

PERMISSION = "sites.view_site"


def _check(request: HttpRequest) -> None:
    if not request.user.has_perm(PERMISSION):
        raise PermissionDenied


def page(request: HttpRequest) -> TemplateResponse:
    _check(request)
    items = [
        {
            "plugin": plugin,
            "download_url": reverse("admin:plugin_download", args=[plugin.code]),
        }
        for plugin in plugins.PLUGINS.values()
    ]
    return TemplateResponse(
        request,
        "admin/sites/plugins/page.html",
        {**admin.site.each_context(request), "title": "Плагины", "items": items},
    )


def download(request: HttpRequest, code: str) -> HttpResponse:
    _check(request)
    plugin = plugins.PLUGINS.get(code)
    if plugin is None:
        raise Http404
    pattern = plugins.site_pattern(request.scheme or "https", request.get_host())
    response = HttpResponse(plugins.archive(plugin, pattern), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{plugin.filename}"'
    # Кэш запрещает admin_view (never_cache): новая версия не застрянет в браузере.
    return response
