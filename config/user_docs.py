"""Раздел «Документация» в интерфейсе (E9-07, ADR-056).

Пользовательская документация (user-docs/, docs/15-USER-DOCS.md) собирается
MkDocs при сборке образа в папку `settings.USER_DOCS_ROOT` (Dockerfile, стадия
docs). Рядом проверщик tools/check_user_docs.py кладёт две карты для
интерфейса: screens.json — какие страницы про какой экран, whatsnew.json —
верхний раздел журнала изменений. Здесь:

- `page` — отдаёт собранный сайт по адресу /docs/ только тем, кто входит в
  админку. Не WhiteNoise: тот раздаёт статику всем подряд, а документация
  внутренняя;
- `{% screen_help %}` — кнопка «?» у названия экрана и в шапке панели записи:
  одна страница про экран — ссылка на неё, несколько — меню, ни одной — кнопки
  нет. Всё открывается в новой вкладке: рабочий экран остаётся как был;
- `whats_new()` — «Что нового» для главной.

Какой экран открыт, узнаём по имени адреса админки (`SCREENS`); идентификаторы
экранов — docs/15-USER-DOCS.md §3.4, там же их ищет проверщик.
"""

import functools
import json
from pathlib import Path
from typing import Any

from django import template
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import SuspiciousFileOperation
from django.http import Http404, HttpRequest, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.utils._os import safe_join
from django.utils.cache import patch_cache_control
from django.utils.safestring import mark_safe
from django.views.static import serve

register = template.Library()

# Имя адреса админки → экран из 15-USER-DOCS.md §3.4. Панель записи — тот же
# адрес, что и полная страница, поэтому «?» в панели находит тот же экран.
SCREENS = {
    "admin:index": "home",
    "admin:sites_productsitelatest_changelist": "site_list",
    "admin:sites_site_card": "site_card",
    "admin:sites_productsite_decision": "site_decision",
    "admin:sites_productsite_change": "site_decision",
    "admin:sites_sitelist_changelist": "site_lists",
    "admin:sites_sitelist_change": "site_lists",
    "admin:sites_sitelist_ahrefs": "sitelist_ahrefs",
    "admin:sites_product_change": "product_card",
    "admin:sites_seller_changelist": "seller_list",
    "admin:sites_seller_change": "seller_list",
    "admin:sites_seller_add": "seller_list",
    "admin:sites_exchangerate_changelist": "exchange_rates",
    "admin:sites_upload_changelist": "upload_list",
    "admin:sites_upload_add": "upload_new",
    "admin:sites_upload_columns": "upload_new",
    "admin:sites_upload_summary": "upload_new",
    "admin:sites_upload_review": "upload_review",
    "admin:sites_productrefdomain_changelist": "refdomain_list",
    "admin:placements_placement_changelist": "placement_list",
    "admin:placements_placement_change": "placement_card",
    "admin:placements_placement_add": "placement_card",
    "admin:placements_invoice_changelist": "invoice_list",
    "admin:placements_invoice_change": "invoice_card",
    "admin:placements_invoice_add": "invoice_card",
    "admin:content_domainsetting_changelist": "settings",
    "admin:content_domainsetting_change": "settings",
    "admin:observability_taskrun_changelist": "task_runs",
    "admin:observability_taskrun_change": "task_runs",
    "admin:observability_apiusage_changelist": "api_usage",
}


@staff_member_required
def page(request: HttpRequest, path: str = "") -> HttpResponseBase:
    """Файл собранного сайта; без входа — страница входа и обратно сюда.

    Сайт собран с адресами-папками (`how-to/x/`): за папкой — её index.html,
    адрес папки без косой черты в конце переводим на адрес с ней, иначе
    относительные ссылки страницы считались бы от родителя.
    """
    root = Path(settings.USER_DOCS_ROOT)
    if path == "" or path.endswith("/"):
        path += "index.html"
    else:
        try:
            folder = Path(safe_join(root, path))
        except SuspiciousFileOperation as error:
            raise Http404("Нет такой страницы") from error
        if folder.is_dir():
            query = request.META.get("QUERY_STRING", "")
            return HttpResponseRedirect(request.path + "/" + (f"?{query}" if query else ""))
    response = serve(request, path, document_root=str(root))
    # Каждый раз сверяться с сервером (ответ «не изменилось» — пустой): после
    # выкатки браузер иначе мог бы ещё день показывать прежнюю страницу.
    patch_cache_control(response, private=True, no_cache=True)
    return response


@register.inclusion_tag("admin/screen_help.html", takes_context=True)
def screen_help(context: template.Context) -> dict[str, Any]:
    """Кнопка «?»: страницы документации про открытый экран."""
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    screen = SCREENS.get(match.view_name) if match is not None else None
    return {"help_pages": screen_pages(screen) if screen else []}


def screen_pages(screen: str) -> list[dict[str, str]]:
    """Страницы про экран: адрес и название, сначала — только про него."""
    screens = _load("screens.json")
    if not isinstance(screens, dict):
        return []
    return [
        {"url": str(found["url"]), "title": str(found["title"])}
        for found in screens.get(screen) or []
        if isinstance(found, dict) and "url" in found and "title" in found
    ]


def whats_new() -> dict[str, Any] | None:
    """«Что нового» для главной: версия и её записи; журнала нет — None.

    До первого выпуска это «Не выпущено» — сделанное, но ещё не выкаченное
    на рабочий сервер (`released` — False).
    """
    news = _load("whatsnew.json")
    if not isinstance(news, dict) or not news.get("html"):
        return None
    return {
        "version": str(news.get("version") or ""),
        "released": bool(news.get("released")),
        # HTML собран из нашего же журнала при сборке образа — доверяем, как шаблону.
        "html": mark_safe(str(news["html"])),
    }


def _load(name: str) -> Any:
    path = Path(settings.USER_DOCS_ROOT) / name
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    return _read(str(path), stamp)


# Кеш в памяти процесса: файл читается один раз, пока не сменится время его
# правки (новый образ — новый файл). Ключ — путь и это время.
@functools.lru_cache(maxsize=8)
def _read(path: str, stamp: int) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
