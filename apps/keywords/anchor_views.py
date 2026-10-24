"""Анкоры продукта на экранах (E3-05, ADR-059): страница «Анкоры», окно «Новый анкор»,
правка долей на месте, блок «Анкоры продукта» над ссылками размещения и
подсказка «ключ во фразе» в панели ссылки (ADR-069).

Всё — по одному списку анкоров, региону (ADR-075): на странице — выбранный, в
размещении — его список, сохранённый или угаданный (`regions`). Цели долей —
общие для продукта; факт по странам — размещения по регионам.

Ответы на запись — JSON и всегда 200: отказ — вопрос человеку, а не ошибка
запроса (4xx браузер пишет в консоль ошибкой).
"""

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from apps.content.domain_settings import anchor_recommend
from apps.keywords import matching, regions
from apps.keywords.anchors import Anchor, TypeStat, overview, pct_text, recommend
from apps.keywords.models import (
    DEFAULT_REGION,
    NAKED_TYPES,
    AnchorList,
    AnchorType,
    CountryShare,
    Keyword,
    PageTypeShare,
)
from apps.sites import countries
from apps.sites.models import Product, Site

MAX_KEYWORD = 200
MAX_URL = 2000
PCT_MAX = Decimal(100)
TYPE_FIELDS = {
    "target": "target_pct",
    "exact": "exact_pct",
    "diluted": "diluted_pct",
    "naked": "naked_pct",
}


# ---------- Страница «Анкоры» ----------


def page_context(
    request: HttpRequest,
    product_id: int | None,
    anchor_list: AnchorList | None = None,
    lists: Sequence[AnchorList] = (),
) -> dict[str, Any]:
    """Блоки над списком: регион, итоги, доли типов страниц, безанкорка, страны."""
    product = Product.objects.filter(pk=product_id).first() if product_id else None
    if product is None:
        return {"anchors_page": None}
    data = overview(product.pk, anchor_list)
    inactive = Keyword.objects.filter(
        product=product, anchor_list=anchor_list, is_active=False
    ).count()
    inactive_query = request.GET.copy()
    inactive_query["activity"] = "inactive"
    inactive_query.pop("p", None)
    region_query = request.GET.copy()
    region_query.pop("p", None)
    region_links = []
    for item in lists:
        region_query["list"] = str(item.pk)
        region_links.append(
            {
                "list": item,
                "url": request.path + "?" + region_query.urlencode(),
                "flag": countries.flag_code(item.country) or "",
                "selected": item == anchor_list,
            }
        )
    naked = [a for a in data.anchors if a.is_naked]
    naked_links = sum(a.links for a in naked)
    keys = [a for a in data.anchors if not a.is_naked]
    targets = [t.target for t in data.types if t.target is not None]
    target_sum = sum(targets, Decimal(0))
    country_rows = list(
        CountryShare.objects.filter(product=product).order_by("-target_pct", "country")
    )
    country_sum = sum((c.target_pct for c in country_rows), Decimal(0))
    upload_url = reverse("admin:sites_upload_add") + f"?kind=anchors&product={product.pk}"
    if anchor_list is not None:
        upload_url += f"&anchor_country={anchor_list.country}"
    return {
        "anchors_page": {
            "product": product,
            "inactive_count": inactive,
            "inactive_url": request.path + "?" + inactive_query.urlencode(),
            "data": data,
            "keys": len(keys),
            "naked_count": len(naked),
            "types": _type_rows(data.types),
            "by_key": sum(t.kinds.by_key for t in data.types),
            "kind_unknown": sum(t.kinds.unknown for t in data.types),
            "target_sum": pct_text(target_sum) if targets else "",
            "target_sum_ok": not targets or target_sum == PCT_MAX,
            "naked": [
                {
                    "anchor": a,
                    "share": _input(a.share),
                    "fact": pct_text(_percent(a.links, naked_links)),
                    "links": a.links,
                }
                for a in sorted(naked, key=lambda a: (-(a.share or 0), a.keyword))
            ],
            "naked_sum": pct_text(sum((a.share or Decimal(0) for a in naked), Decimal(0)))
            if naked
            else "",
            "countries": _country_rows(product, country_rows),
            "country_sum": pct_text(country_sum) if country_rows else "",
            "country_sum_ok": not country_rows or country_sum == PCT_MAX,
            "anchor_list": anchor_list,
            "region_links": region_links,
            "can_edit": request.user.has_perm("keywords.change_keyword"),
            "can_add": request.user.has_perm("keywords.add_keyword"),
            "delete_list_url": reverse("admin:keywords_anchorlist_delete", args=[anchor_list.pk])
            if anchor_list is not None and request.user.has_perm("keywords.delete_anchorlist")
            else "",
            "share_url": reverse("admin:keywords_anchor_share"),
            "upload_url": upload_url,
            **dialog_context(product, lists, anchor_list),
        }
    }


def _country_rows(product: Product, shares: Sequence[CountryShare]) -> list[dict[str, Any]]:
    """Страны: цель из «Распределения по странам» и факт — размещения по регионам.

    Регион размещения — его список анкоров, не выбирали — угаданный, как в
    карточке размещения (`regions`). Страна с размещениями, но без цели, — своей
    строкой без цели; «Остальные» — все, кого нет среди строк с целью.
    """
    placed = regions.placement_countries(product.pk)
    total = sum(placed.values())
    named = {share.country for share in shares if share.country}
    rows: list[dict[str, Any]] = []
    for share in shares:
        if share.country:
            count = placed.get(share.country, 0)
        else:
            count = sum(n for code, n in placed.items() if code not in named)
        rows.append(_country_row(share, share.country, count, total))
    if any(share.country is None for share in shares):
        return rows
    # «Остальных» среди целей нет — регион без цели своей строкой: факт сойдётся в 100%.
    for code in sorted(set(placed) - named, key=lambda code: -placed[code]):
        rows.append(_country_row(None, code, placed[code], total))
    return rows


def _country_row(
    share: CountryShare | None, country: str | None, count: int, total: int
) -> dict[str, Any]:
    return {
        "row": share,
        "name": countries.name(country) if country else "Остальные",
        "flag": countries.flag_html(country or countries.GLOBE),
        "share": _input(share.target_pct) if share is not None else "",
        "count": count,
        "fact": pct_text(_percent(count, total)),
    }


def _type_rows(types: Sequence[TypeStat]) -> list[dict[str, Any]]:
    """Строки таблицы долей: числа и полоска «цель / факт» в одном масштабе."""
    values = [v for t in types for v in (t.target, t.total_pct) if v is not None]
    scale = max(values, default=Decimal(0))
    scale = max(Decimal(10), (scale / 10).to_integral_value(rounding="ROUND_CEILING") * 10)
    rows = []
    for stat in types:
        share = stat.share
        gap = stat.gap
        rows.append(
            {
                "stat": stat,
                "target": _input(stat.target),
                "exact": _input(share.exact if share else None),
                "diluted": _input(share.diluted if share else None),
                "naked": _input(share.naked if share else None),
                # Факт по виду ссылок внутри типа — под целью в той же ячейке.
                "exact_fact": pct_text(stat.kinds.pct("exact")),
                "diluted_fact": pct_text(stat.kinds.pct("diluted")),
                "naked_fact": pct_text(stat.kinds.pct("naked")),
                "placed_pct": pct_text(stat.placed_pct),
                "waiting_pct": pct_text(stat.waiting_pct),
                "total_pct": pct_text(stat.total_pct),
                "fact_width": _width(stat.total_pct, scale),
                "target_left": _width(stat.target, scale),
                "gap": gap,
                "gap_text": _gap_text(gap),
                "state": _state(gap),
            }
        )
    return rows


def _width(value: Decimal | None, scale: Decimal) -> str:
    if value is None or scale <= 0:
        return "0"
    return f"{min(Decimal(100), value * 100 / scale):.1f}"


def _gap_text(gap: Decimal | None) -> str:
    if gap is None:
        return ""
    if abs(gap) < Decimal("0.5"):
        return "по цели"
    sign = "недобор" if gap > 0 else "перебор"
    return f"{sign} {pct_text(abs(gap))}"


def _state(gap: Decimal | None) -> str:
    """Цвет полоски: недобор больше 3 п.п. — красный, перебор — жёлтый, иначе зелёный."""
    if gap is None:
        return "none"
    if gap > 3:
        return "under"
    if gap < -3:
        return "over"
    return "ok"


def _input(value: Decimal | None) -> str:
    """Значение для поля ввода: 15.00 → «15», 56.86 → «56.86»."""
    if value is None:
        return ""
    return f"{value.normalize():f}"


def _percent(part: int, whole: int) -> Decimal | None:
    if whole == 0:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"))


# ---------- Окно «Новый анкор» ----------


def dialog_context(
    product: Product,
    lists: Sequence[AnchorList] | None = None,
    current: AnchorList | None = None,
) -> dict[str, Any]:
    """Что нужно окну «Новый анкор»: адрес записи, списки продукта и типы страниц.

    Список в окне выбран заранее — открытый на странице или выбранный в
    размещении; скрипт ставит его заново при каждом открытии.
    """
    if lists is None:
        lists = AnchorList.of_product(product.pk)
    if current is None and lists:
        current = lists[0]
    names = set(PageTypeShare.objects.filter(product=product).values_list("page_type", flat=True))
    names.update(
        name
        for name in Keyword.objects.filter(product=product, page_type__isnull=False)
        .values_list("page_type", flat=True)
        .distinct()
        if name
    )
    page_types = sorted(names)
    return {
        "add_url": reverse("admin:keywords_anchor_add"),
        "page_types": page_types,
        "product_domain": product.domain,
        "lists": regions.options(lists),
        "current_list": str(current.pk) if current is not None else "",
        # Списков нет — первый анкор заведёт основной: США (ADR-075).
        "first_list": {
            "name": countries.name(DEFAULT_REGION),
            "flag": countries.flag_code(DEFAULT_REGION) or "",
        },
    }


def quick_add_view(request: HttpRequest) -> JsonResponse:
    """Новый анкор продукта из окна: анкор, куда ведёт и, если знают, тип страницы.

    Такой анкор уже есть (без учёта регистра) — отдаём его: окно его и выберет.
    """
    if not request.user.has_perm("keywords.add_keyword"):
        return JsonResponse({"ok": False, "message": "Нет прав добавлять анкоры."})
    product = Product.objects.filter(pk=_digits(request.POST.get("product"))).first()
    if product is None:
        return JsonResponse({"ok": False, "message": "Выберите продукт размещения."})
    # Список — выбранный в окне, иначе основной; у продукта без списков первый
    # анкор заводит основной список — США, как анкоры до регионов (ADR-075).
    anchor_list = AnchorList.objects.filter(
        product=product, pk=_digits(request.POST.get("list"))
    ).first() or AnchorList.main_of(product.pk)
    text = " ".join((request.POST.get("keyword") or "").split())
    url = (request.POST.get("target_url") or "").strip()
    page_type = " ".join((request.POST.get("page_type") or "").split()) or None
    if not text:
        return JsonResponse({"ok": False, "message": "Впишите анкор."})
    if len(text) > MAX_KEYWORD:
        return JsonResponse({"ok": False, "message": f"Анкор длиннее {MAX_KEYWORD} знаков."})
    found = (
        Keyword.objects.annotate(text=Lower("keyword"))
        .filter(anchor_list=anchor_list, text=text.lower())
        .first()
        if anchor_list is not None
        else None
    )
    if found is not None and anchor_list is not None:
        if not found.is_active:
            found.is_active = True
            found.save(update_fields=["is_active"])
        where = anchor_list.region_name
        return JsonResponse(
            {**anchor_json(found), "ok": True, "existing": True,
             "message": f"Анкор «{found.keyword}» уже есть в списке «{where}» — выбран он."}
        )  # fmt: skip
    if not url.startswith(("http://", "https://")) or len(url) > MAX_URL or " " in url:
        return JsonResponse({"ok": False, "message": "Впишите адрес страницы целиком: https://…"})
    from apps.sites.uploads.anchors import naked_type

    kind = naked_type(text, product)
    try:
        with transaction.atomic():
            anchor_list = anchor_list or AnchorList.ensure_main(product.pk)
            keyword = Keyword.objects.create(
                product=product,
                anchor_list=anchor_list,
                keyword=text,
                target_url=url,
                page_type=page_type,
                anchor_type=kind if kind != AnchorType.GENERIC else AnchorType.EXACT,
            )
    except IntegrityError:
        return JsonResponse({"ok": False, "message": f"Анкор «{text}» уже есть."})
    where = anchor_list.region_name
    return JsonResponse(
        {**anchor_json(keyword), "ok": True, "existing": False,
         "message": f"Анкор «{keyword.keyword}» добавлен в список «{where}»."}
    )  # fmt: skip


def anchor_json(keyword: Keyword) -> dict[str, Any]:
    return {
        "id": keyword.pk,
        "keyword": keyword.keyword,
        "url": keyword.target_url,
        "page_type": keyword.page_type or "",
        "product": keyword.product_id,
        "list": keyword.anchor_list_id,
        "naked": keyword.anchor_type in NAKED_TYPES,
    }


# ---------- Ключ во фразе — подсказка в панели ссылки ----------


def match_view(request: HttpRequest) -> JsonResponse:
    """Ключи во фразе ссылки и её вид — тем же поиском, что форма и загрузки.

    `keyword` — анкор, выбранный в ссылке: вид считается по нему. Не выбран —
    по найденному, если выбор однозначен. Подсказка ничего не записывает:
    форма проверяет всё заново при сохранении.
    """
    if not (
        request.user.has_perm("placements.change_placement")
        or request.user.has_perm("placements.add_placement")
    ):
        return JsonResponse({"ok": False, "message": "Нет прав на размещения."})
    product = Product.objects.filter(pk=_digits(request.GET.get("product"))).first()
    if product is None:
        return JsonResponse({"ok": False, "message": "Выберите продукт размещения."})
    text = " ".join((request.GET.get("text") or "").split())
    url = (request.GET.get("url") or "").strip() or None
    list_id = _digits(request.GET.get("list")) or None
    found = matching.find(text, matching.candidates(product.pk, list_id), url=url)
    selected = Keyword.objects.filter(
        product=product, pk=_digits(request.GET.get("keyword"))
    ).first()
    base: matching.Candidate | None = (
        matching.Candidate(selected.pk, selected.keyword, selected.target_url, selected.anchor_type)
        if selected is not None
        else found.keyword
    )
    kind = matching.kind(text or base.keyword, base.keyword, base.anchor_type) if base else None
    return JsonResponse(
        {
            "ok": True,
            "chosen": found.keyword.id if found.keyword else None,
            "ambiguous": found.ambiguous,
            "exact": found.exact,
            "candidates": [
                {"id": c.id, "keyword": c.keyword, "url": c.target_url}
                for c in found.candidates[:6]
            ],
            "kind": str(kind) if kind else None,
            "kind_label": AnchorType(kind).label if kind else None,
        }
    )


# ---------- Доли — правка на месте ----------


def share_view(request: HttpRequest) -> JsonResponse:
    """Одна ячейка доли: тип страниц, безанкорный анкор или страна. Пусто — «не задано»."""
    if not request.user.has_perm("keywords.change_keyword"):
        return JsonResponse({"ok": False, "message": "Нет прав менять доли."})
    kind = request.POST.get("kind")
    field = request.POST.get("field") or ""
    raw = (request.POST.get("value") or "").replace(",", ".").replace("%", "").strip()
    try:
        value = Decimal(raw).quantize(Decimal("0.01")) if raw else None
    except InvalidOperation:
        return JsonResponse(
            {"ok": False, "message": "Доля — число процентов, например 15 или 2.5."}
        )
    if value is not None and not Decimal(0) <= value <= PCT_MAX:
        return JsonResponse({"ok": False, "message": "Доля — от 0 до 100%."})
    now = timezone.now()
    if kind == "type" and field in TYPE_FIELDS:
        product = get_object_or_404(Product, pk=_digits(request.POST.get("product")))
        page_type = " ".join((request.POST.get("id") or "").split())
        if not page_type:
            return JsonResponse({"ok": False, "message": "Нет такого типа страниц."})
        share, _ = PageTypeShare.objects.get_or_create(
            product=product,
            page_type=page_type,
            defaults={"position": _next_position(product)},
        )
        setattr(share, TYPE_FIELDS[field], value)
        share.updated_at = now
        share.save()
        return _saved(value)
    if kind == "naked" and field == "share":
        keyword = get_object_or_404(Keyword, pk=_digits(request.POST.get("id")))
        keyword.share = value
        keyword.save(update_fields=["share"])
        return _saved(value)
    if kind == "country" and field == "target":
        if value is None:
            return JsonResponse({"ok": False, "message": "Впишите долю страны."})
        country = get_object_or_404(CountryShare, pk=_digits(request.POST.get("id")))
        country.target_pct = value
        country.updated_at = now
        country.save(update_fields=["target_pct", "updated_at"])
        return _saved(value)
    return JsonResponse(
        {"ok": False, "message": "Не понял, какую долю менять — обновите страницу."}
    )


def _saved(value: Decimal | None) -> JsonResponse:
    return JsonResponse({"ok": True, "value": _input(value), "text": pct_text(value)})


def _next_position(product: Product) -> int:
    last = (
        PageTypeShare.objects.filter(product=product)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
    )
    return (last or 0) + 1


# ---------- Блок «Анкоры продукта» над ссылками размещения ----------


def summary_context(
    product_id: int | None,
    exclude: Iterable[int] = (),
    *,
    choice: regions.Choice | None = None,
    lists: Sequence[AnchorList] | None = None,
) -> dict[str, Any]:
    """Итоги, «цель / факт» по типам и рекомендации — для блока в размещении.

    `choice` — список размещения и почему он (`regions`); не задан — основной.
    """
    product = Product.objects.filter(pk=product_id).first() if product_id else None
    if product is None:
        return {"anchor_block": None}
    if lists is None:
        lists = AnchorList.of_product(product.pk)
    if choice is None:
        choice = regions.Choice(lists[0] if lists else None, "")
    current = choice.anchor_list
    data = overview(product.pk, current)
    rules = anchor_recommend(product.pk)
    picks = recommend(
        data.anchors, data.shares, limit=rules.limit, skip_top=rules.skip_top, exclude=exclude
    )
    anchors_url = reverse("admin:keywords_keywordcoverage_changelist") + f"?product={product.pk}"
    if current is not None:
        anchors_url += f"&list={current.pk}"
    return {
        "anchor_block": {
            "product": product,
            "data": data,
            "types": [row for row in _type_rows(data.types) if row["stat"].target is not None],
            "picks": [_pick_json(p.anchor, p.reason) for p in picks],
            "skip_top": rules.skip_top,
            "lists": regions.options(lists),
            "current": current,
            "current_id": str(current.pk) if current is not None else "",
            "reason": choice.reason,
            "anchors_url": anchors_url,
            "summary_url": reverse("admin:keywords_anchor_summary", args=[product.pk]),
        }
    }


def _pick_json(anchor: Anchor, reason: str) -> dict[str, Any]:
    return {
        "id": anchor.id,
        "keyword": anchor.keyword,
        "url": anchor.target_url,
        "page_type": anchor.page_type or "",
        "placed": anchor.placed,
        "waiting": anchor.waiting,
        "position": anchor.position,
        "naked": anchor.is_naked,
        "reason": reason,
    }


def summary_view(request: HttpRequest, product_id: int) -> TemplateResponse:
    """Блок «Анкоры продукта» заново: сменили продукт в форме нового размещения или
    список анкоров в блоке (`list`). Без списка — угаданный по площадке (`site`)."""
    exclude = [int(pk) for pk in request.GET.getlist("exclude") if pk.isdigit()]
    lists = AnchorList.of_product(product_id)
    by_id = {item.pk: item for item in lists}
    chosen = by_id.get(_digits(request.GET.get("list")))
    site = Site.all_objects.filter(pk=_digits(request.GET.get("site"))).first()
    if chosen is not None:
        choice = regions.Choice(chosen, "выбран вами")
    elif site is not None:
        choice = regions.choose(site, lists)
    else:
        choice = regions.Choice(lists[0] if lists else None, "")
    return TemplateResponse(
        request,
        "admin/keywords/anchor_summary.html",
        summary_context(product_id, exclude, choice=choice, lists=lists),
    )


def _digits(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def activity_view(request: HttpRequest, keyword_id: int) -> HttpResponse:
    """Только активность ключа: исторические ссылки не меняются."""
    if not request.user.has_perm("keywords.change_keyword"):
        return HttpResponse(status=403)
    value = request.POST.get("active")
    if value not in ("0", "1"):
        return HttpResponse("Выберите состояние анкора.", status=400)
    keyword = get_object_or_404(Keyword, pk=keyword_id)
    Keyword.objects.filter(pk=keyword.pk).update(is_active=value == "1")
    fallback = (
        reverse("admin:keywords_keywordcoverage_changelist") + f"?product={keyword.product_id}"
    )
    target = request.GET.get("next") or fallback
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = fallback
    return HttpResponseRedirect(target)
