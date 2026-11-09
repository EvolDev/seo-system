"""Экран «Сверка с Collaborator», кнопка «Обновить из Collaborator», блок в панели.

Экран (Работа → Сверка с Collaborator) — сделки рабочего продукта по группам:
готовы к закрытию, можно заполнить, расходится, нет в системе, оставлено как в
системе; внизу — последние действия сверки с «Отменить». Действия — запросом без
перезагрузки (seo/collaborator.js), после них экран перечитывается.

Кнопка «Обновить» ставит задачу `collaborator_refresh` и отдаёт ключ; ход и итог
страница спрашивает по ключу (`refresh_status_view`). Пока задача идёт, второе
нажатие получает тот же ключ.
"""

import datetime as dt
import json
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from django.contrib import admin
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.integrations.collaborator import has_key
from apps.integrations.collaborator.client import NoApiKey
from apps.placements.tasks import REFRESH_RUNNING, REFRESH_TTL, collaborator_refresh, refresh_key
from apps.sites.display import domain_tools_html
from apps.sites.models import Product, SiteOffer, Upload, UploadKind, WorkStatus
from apps.sites.offers import money
from apps.sites.site_card import when_text
from apps.workspace.products import working_product_id
from config.run_id import bind_run_id, new_run_id

from . import actions
from .deals import Deal, Money
from .matching import HOW_TEXT, Matched, Miss
from .reconcile import (
    FIELD_TEXT,
    ORDER,
    VERDICT_TEXT,
    Differ,
    Field,
    Row,
    Verdict,
    closing_fills,
)
from .service import Report, report

JOURNAL_SIZE = 10
VIEW = "placements.view_placement"
CHANGE = "placements.change_placement"

MISS_TEXT = {
    Miss.NO_SITE: "площадки нет в базе: на Collaborator домен записан иначе?",
    Miss.NO_PLACEMENT: "размещения на площадке нет",
    Miss.TAKEN: "размещения площадки уже у других сделок",
    Miss.OTHER_SELLER: "размещения площадки — через других продавцов",
    Miss.MANY: "на площадке несколько размещений — впишите номер заявки в нужное",
    Miss.SHARED: "одно размещение на несколько сделок — впишите номер заявки в нужное",
}


def page_view(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm(VIEW):
        raise PermissionDenied
    product = _product(request)
    context: dict[str, Any] = {
        **admin.site.each_context(request),
        "title": "Сверка с Collaborator",
        "product": product,
        "has_key": has_key(),
        "can_change": request.user.has_perm(CHANGE),
        "refresh_url": reverse("admin:collaborator_refresh"),
        "status_url": reverse("admin:collaborator_refresh_status"),
    }
    if product is not None:
        found = report(product)
        context.update(screen(found, request))
    return TemplateResponse(request, "admin/placements/collaborator/page.html", context)


def refresh_view(request: HttpRequest) -> JsonResponse:
    """Ставит «Обновить из Collaborator»: {"key"} или {"error"} — без ключа API."""
    if not request.user.has_perm(VIEW):
        return JsonResponse({"error": "Нет прав на просмотр размещений."}, status=403)
    if not has_key():
        # 200, а не 4xx: это ответ человеку, браузер не пишет его в консоль ошибкой.
        return JsonResponse({"error": str(NoApiKey())})
    key = uuid4().hex
    if not cache.add(REFRESH_RUNNING, key, REFRESH_TTL):
        running = cache.get(REFRESH_RUNNING)
        if running:
            return JsonResponse({"key": running, "again": True})
        cache.set(REFRESH_RUNNING, key, REFRESH_TTL)
    cache.set(refresh_key(key), {"state": "queued"}, REFRESH_TTL)
    # Нажатие — начало цепочки (ADR-025).
    with bind_run_id(new_run_id()):
        collaborator_refresh.delay(key, working_product_id(request))
    return JsonResponse({"key": key})


def refresh_status_view(request: HttpRequest) -> JsonResponse:
    if not request.user.has_perm(VIEW):
        return JsonResponse({"error": "Нет прав на просмотр размещений."}, status=403)
    key = request.GET.get("key", "")
    state = cache.get(refresh_key(key)) if key else None
    if state is None:
        return JsonResponse({"state": "unknown"})
    if state.get("state") == "done":
        state = {**state, "url": reverse("admin:collaborator")}
    return JsonResponse(state)


def fill_view(request: HttpRequest) -> JsonResponse:
    product = _changing(request)
    if isinstance(product, JsonResponse):
        return product
    ids = [int(value) for value in request.POST.getlist("deal") if value.isdigit()]
    outcome = actions.fill(request, product, ids)
    return _answer(outcome)


def take_view(request: HttpRequest) -> JsonResponse:
    product = _changing(request)
    if isinstance(product, JsonResponse):
        return product
    deal_id, what = _deal_field(request)
    if deal_id is None or what is None:
        return JsonResponse({"error": "Не понял, что брать."}, status=400)
    return _answer(actions.take(request, product, deal_id, what))


def keep_view(request: HttpRequest) -> JsonResponse:
    product = _changing(request)
    if isinstance(product, JsonResponse):
        return product
    deal_id, what = _deal_field(request)
    if deal_id is None or what is None:
        return JsonResponse({"error": "Не понял, что оставить."}, status=400)
    if request.POST.get("back") == "1":
        return JsonResponse({"message": actions.unkeep(deal_id, what), "level": "success"})
    return JsonResponse({"message": actions.keep(request, product, deal_id, what)})


# --- То, что видит человек ---


def screen(found: Report, request: HttpRequest) -> dict[str, Any]:
    groups = found.groups
    journal = Upload.objects.filter(kind=UploadKind.COLLABORATOR, product=found.product)
    journal = journal.select_related("author").order_by("-pk")[:JOURNAL_SIZE]
    return {
        "report": found,
        "fetched": when_text(found.fetched_at) if found.fetched_at else "",
        "summary": found.summary,
        "fill_counts": _counts(groups.fill_counts()),
        "differ_counts": _counts(groups.differ_counts()),
        "ready": [_line(found, row.matched, row, closing=True) for row in groups.ready],
        "fill": [_line(found, row.matched, row) for row in groups.fill],
        "differ": [_line(found, row.matched, row, kept=False) for row in groups.differ],
        "missing": [_missing(found, item) for item in groups.missing],
        "kept": [_line(found, row.matched, row, kept=True) for row in groups.kept],
        "foreign": [
            {"project": project, "name": name, "count": count}
            for project, (name, count) in sorted(found.foreign.items())
        ],
        "journal": [_journal_line(upload) for upload in journal],
        "take_url": reverse("admin:collaborator_take"),
        "keep_url": reverse("admin:collaborator_keep"),
        "fill_url": reverse("admin:collaborator_fill"),
        "product_url": reverse("admin:sites_product_change", args=[found.product.pk]),
    }


def _line(
    found: Report, matched: Matched, row: Row, *, kept: bool | None = None, closing: bool = False
) -> dict[str, Any]:
    deal = matched.deal
    facts = row.placement
    domain = found.domains.get(matched.site_id or 0, deal.domain)
    differs = row.differs
    if kept is not None:
        differs = tuple(d for d in row.differs if d.kept is kept)
    return {
        "deal": deal,
        "domain": domain,
        "domain_html": _domain_html(domain, _change_url(facts.id)),
        "placement_url": _change_url(facts.id),
        "status": deal.status,
        "system": WorkStatus(facts.status).label,
        "how": HOW_TEXT[matched.how] if matched.how else "",
        "article_url": deal.article_url,
        # «Готовы к закрытию» — что поставит «Заполнить из Collaborator» в панели.
        "fills": [
            _fill_text(fill.field, fill.value)
            for fill in (closing_fills(row) if closing else row.fills)
        ],
        "differs": [_differ_line(deal, d, row) for d in sorted(differs, key=_order)],
        "total": _money(deal.total),
    }


def _differ_line(deal: Deal, differ: Differ, row: Row) -> dict[str, Any]:
    line: dict[str, Any] = {
        "field": differ.field,
        "label": FIELD_TEXT[differ.field],
        "system": _value_html(differ.field, differ.system),
        "collaborator": _value_html(differ.field, differ.collaborator),
        "takeable": differ.takeable,
        "target_status": differ.target_status,
        "target_label": WorkStatus(differ.target_status).label if differ.target_status else "",
        "kept": _kept_text(deal, differ.field),
    }
    if differ.field == Field.ANCHORS:
        line["links"] = [
            {
                "ok": check.verdict == Verdict.OK,
                "verdict": VERDICT_TEXT[check.verdict],
                "system": check.system,
                "collaborator": check.collaborator,
            }
            for check in row.links
        ]
    return line


def _missing(found: Report, item: Matched) -> dict[str, Any]:
    deal = item.deal
    domain = found.domains.get(item.site_id or 0, deal.domain)
    # Чьё размещение: у какой сделки оно уже есть — ссылкой на него.
    holders = {m.placement.id: m.deal.id for m in found.matched if m.placement is not None}
    candidates = [
        format_html(
            '<a href="{}" data-panel>{}</a>',
            _change_url(pk),
            f"у сделки {holders[pk]}" if pk in holders else f"размещение {number}",
        )
        for number, pk in enumerate(item.candidates, start=1)
    ]
    add_url = ""
    if item.site_id is not None:
        query = {"site": item.site_id, "product": found.product.pk, "collaborator_deal": deal.id}
        add_url = f"{reverse('admin:placements_placement_add')}?{urlencode(query)}"
    return {
        "deal": deal,
        "domain": domain,
        "domain_html": _domain_html(domain, ""),
        "reason": MISS_TEXT.get(item.miss, "") if item.miss else "",
        "candidates": candidates,
        "add_url": add_url,
        "total": _money(deal.total),
        "article_url": deal.article_url,
    }


def _journal_line(upload: Upload) -> dict[str, Any]:
    result = upload.result or {}
    who = ""
    if upload.author is not None:
        who = upload.author.first_name or upload.author.username
    return {
        "upload": upload,
        "when": when_text(upload.created_at),
        "who": who,
        "title": upload.file_name,
        "placements": result.get("sites", 0),
        "undo_url": reverse("admin:sites_upload_delete", args=[upload.pk]),
    }


def _counts(counts: dict[Field, int]) -> list[tuple[str, int]]:
    return [(FIELD_TEXT[name], count) for name, count in counts.items()]


def _order(differ: Differ) -> int:
    return ORDER.index(differ.field)


def _fill_text(what: Field, value: Any) -> SafeString:
    if what == Field.ORDER:
        return format_html("номер заявки <b>{}</b>", value)
    if what == Field.PAID and isinstance(value, Money):
        return format_html("«Заплачено» <b>{}</b>", money(value.cents, value.currency))
    if what == Field.ARTICLE:
        return format_html('адрес статьи <a href="{}" target="_blank" rel="noopener">↗</a>', value)
    return format_html("{}", "анонс на главной")


def _value_html(what: Field, value: Any) -> SafeString:
    if what == Field.PAID and isinstance(value, list) and len(value) == 2:
        return format_html("{}", money(value[0], value[1]))
    if what == Field.ANNOUNCE:
        return format_html("{}", "есть" if value else "нет")
    if what == Field.ARTICLE and value:
        return format_html('<a href="{0}" target="_blank" rel="noopener">{0}</a>', value)
    if what == Field.STATUS and isinstance(value, str) and value in WorkStatus.values:
        return format_html("{}", WorkStatus(value).label)
    if what == Field.ANCHORS:
        return format_html("{}", "")
    return format_html("{}", value or "—")


def _kept_text(deal: Deal, what: Field) -> str:
    stored = deal.kept.get(what)
    if not isinstance(stored, dict):
        return ""
    at = stored.get("at")
    try:
        moment = dt.datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        moment = None
    return when_text(moment) if moment else ""


def _money(value: Money | None) -> str:
    return money(value.cents, value.currency) if value is not None else ""


def _change_url(placement_id: int) -> str:
    return reverse("admin:placements_placement_change", args=[placement_id])


def _domain_html(domain: str, url: str) -> SafeString:
    """Домен — ссылкой на размещение (панелью), рядом — «открыть сайт» и «скопировать»."""
    if url:
        name = format_html('<a href="{}" data-panel>{}</a>', url, domain)
    else:
        name = format_html("<b>{}</b>", domain)
    return format_html("{}{}", name, domain_tools_html(domain))


def _product(request: HttpRequest) -> Product | None:
    product_id = working_product_id(request)
    return Product.objects.filter(pk=product_id).first() if product_id is not None else None


def _changing(request: HttpRequest) -> Product | JsonResponse:
    if not request.user.has_perm(CHANGE):
        return JsonResponse({"error": "Нет прав на изменение размещений."}, status=403)
    product = _product(request)
    if product is None:
        return JsonResponse({"error": "Выберите рабочий продукт в шапке."})
    return product


def _deal_field(request: HttpRequest) -> tuple[int | None, Field | None]:
    raw = request.POST.get("deal", "")
    what = request.POST.get("field", "")
    deal_id = int(raw) if raw.isdigit() else None
    try:
        return deal_id, Field(what)
    except ValueError:
        return deal_id, None


def _answer(outcome: actions.Outcome) -> JsonResponse:
    level = "success" if outcome.changed else "warning" if outcome.busy else "info"
    data: dict[str, Any] = {"message": outcome.message(), "level": level}
    if outcome.upload is not None:
        data["undo_url"] = reverse("admin:sites_upload_delete", args=[outcome.upload.pk])
    return JsonResponse(data)


# --- Новое размещение из сделки и блок в панели ---


def deal_initial(product: Product, site_id: int, deal: Deal) -> dict[str, Any]:
    """Поля нового размещения из сделки: «Завести размещение» в «Нет в системе».

    Статус — «Заявка отправлена» с днём сделки на Collaborator; закрывает
    человек. «Заплачено» и анонс — только у сделки с готовой статьёй.
    """
    from apps.sites.models import Seller

    initial: dict[str, Any] = {
        "status": WorkStatus.ORDERED,
        "collaborator_order_id": str(deal.id),
        "seller": Seller.collaborator().pk,
    }
    day = _day(deal.created_at)
    if day is not None:
        initial["ordered_at"] = timezone.make_aware(dt.datetime.combine(day, dt.time()))
    if deal.article_url:
        initial["article_url"] = deal.article_url
    if deal.article_ready and deal.total is not None:
        initial["price_paid_cents"] = deal.total.cents
        initial["currency"] = deal.total.currency
    if deal.article_ready and deal.announce is not None and deal.announce.cents > 0:
        initial["announce_on_homepage"] = True
    # «Через кого» — предложение Collaborator (те же варианты, что у формы).
    offer = (
        SiteOffer.objects.filter(site_id=site_id, seller__is_collaborator=True)
        .order_by("placement_type", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    if offer is not None:
        initial["offer"] = offer
    return initial


def _day(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def panel_block(row: Row, found: Report, *, can_fill: bool = True) -> SafeString:
    """Блок «Collaborator» в панели размещения: сделка, Total, анкоры, «Заполнить»."""
    from django.template.loader import render_to_string

    deal = row.deal
    links = [
        {
            "ok": check.verdict == Verdict.OK,
            "verdict": VERDICT_TEXT[check.verdict],
            "system": check.system,
            "collaborator": check.collaborator,
        }
        for check in row.links
    ]
    differs = [_differ_line(deal, d, row) for d in sorted(row.open_differs, key=_order)]
    html = render_to_string(
        "admin/placements/collaborator/_panel_block.html",
        {
            "deal": deal,
            "row": row,
            "how": HOW_TEXT[row.matched.how] if row.matched.how else "",
            "total": _money(deal.total),
            "parts": [(name, _money(value)) for name, value in deal.parts],
            "announce": _money(deal.announce),
            "links": links,
            "differs": [d for d in differs if d["field"] != Field.ANCHORS],
            "fill": bool(can_fill),
            "fill_json": json.dumps(fill_data(row), ensure_ascii=False) if can_fill else "",
            "fetched": when_text(found.fetched_at) if found.fetched_at else "",
            "screen_url": reverse("admin:collaborator"),
        },
    )
    return SafeString(html)


def fill_data(row: Row) -> dict[str, Any]:
    """Значения для «Заполнить из Collaborator»: поле формы → значение."""
    deal, facts = row.deal, row.placement
    data: dict[str, Any] = {"collaborator_order_id": str(deal.id)}
    if deal.article_url:
        data["article_url"] = deal.article_url
    if deal.article_ready and deal.total is not None and not facts.from_invoice:
        units, cents = divmod(deal.total.cents, 100)
        data["price_paid_cents"] = f"{units},{cents:02d}"
        data["currency"] = deal.total.currency
    if deal.article_ready and deal.announce is not None and deal.announce.cents > 0:
        data["announce_on_homepage"] = "true"
    return data


def home_card() -> tuple[str, str]:
    """Карточка главной: сколько сделок в снимке и когда их забрали — один запрос."""
    from django.db.models import Count, Max

    from apps.placements.models import CollaboratorDeal

    found = CollaboratorDeal.objects.aggregate(count=Count("pk"), at=Max("fetched_at"))
    if not found["count"]:
        return ("", "сделок ещё нет — «Обновить из Collaborator»")
    return (f"{found['count']}", f"сделок · забрали {when_text(found['at'])}")
