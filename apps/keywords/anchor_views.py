"""Анкоры продукта на экранах (E3-05, ADR-059): страница «Анкоры», окно «Новый анкор»,
правка долей на месте и блок «Анкоры продукта» над ссылками размещения.

Ответы на запись — JSON и всегда 200: отказ — вопрос человеку, а не ошибка
запроса (4xx браузер пишет в консоль ошибкой).
"""

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from apps.content.domain_settings import anchor_recommend
from apps.keywords.anchors import Anchor, TypeStat, overview, pct_text, recommend
from apps.keywords.models import (
    NAKED_TYPES,
    AnchorType,
    CountryShare,
    Keyword,
    PageTypeShare,
)
from apps.sites import countries
from apps.sites.models import Product

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


def page_context(request: HttpRequest, product_id: int | None) -> dict[str, Any]:
    """Блоки над списком: итоги, доли типов страниц, безанкорка, страны."""
    product = Product.objects.filter(pk=product_id).first() if product_id else None
    if product is None:
        return {"anchors_page": None}
    data = overview(product.pk)
    naked = [a for a in data.anchors if a.is_naked]
    naked_links = sum(a.links for a in naked)
    keys = [a for a in data.anchors if not a.is_naked]
    targets = [t.target for t in data.types if t.target is not None]
    target_sum = sum(targets, Decimal(0))
    country_rows = list(
        CountryShare.objects.filter(product=product).order_by("-target_pct", "country")
    )
    country_sum = sum((c.target_pct for c in country_rows), Decimal(0))
    return {
        "anchors_page": {
            "product": product,
            "data": data,
            "keys": len(keys),
            "naked_count": len(naked),
            "types": _type_rows(data.types),
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
            "countries": [
                {
                    "row": c,
                    "name": countries.name(c.country) if c.country else "Остальные",
                    "flag": countries.flag_html(c.country or countries.GLOBE),
                    "share": _input(c.target_pct),
                }
                for c in country_rows
            ],
            "country_sum": pct_text(country_sum) if country_rows else "",
            "country_sum_ok": not country_rows or country_sum == PCT_MAX,
            "can_edit": request.user.has_perm("keywords.change_keyword"),
            "can_add": request.user.has_perm("keywords.add_keyword"),
            "share_url": reverse("admin:keywords_anchor_share"),
            "upload_url": reverse("admin:sites_upload_add") + f"?kind=anchors&product={product.pk}",
            **dialog_context(product),
        }
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


def dialog_context(product: Product) -> dict[str, Any]:
    """Что нужно окну «Новый анкор»: адрес записи и типы страниц продукта."""
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
    text = " ".join((request.POST.get("keyword") or "").split())
    url = (request.POST.get("target_url") or "").strip()
    page_type = " ".join((request.POST.get("page_type") or "").split()) or None
    if not text:
        return JsonResponse({"ok": False, "message": "Впишите анкор."})
    if len(text) > MAX_KEYWORD:
        return JsonResponse({"ok": False, "message": f"Анкор длиннее {MAX_KEYWORD} знаков."})
    found = (
        Keyword.objects.annotate(text=Lower("keyword"))
        .filter(product=product, text=text.lower())
        .first()
    )
    if found is not None:
        if not found.is_active:
            found.is_active = True
            found.save(update_fields=["is_active"])
        return JsonResponse(
            {**anchor_json(found), "ok": True, "existing": True,
             "message": f"Анкор «{found.keyword}» уже есть — выбран он."}
        )  # fmt: skip
    if not url.startswith(("http://", "https://")) or len(url) > MAX_URL or " " in url:
        return JsonResponse({"ok": False, "message": "Впишите адрес страницы целиком: https://…"})
    from apps.sites.uploads.anchors import naked_type

    kind = naked_type(text, product)
    try:
        with transaction.atomic():
            keyword = Keyword.objects.create(
                product=product,
                keyword=text,
                target_url=url,
                page_type=page_type,
                anchor_type=kind if kind != AnchorType.GENERIC else AnchorType.EXACT,
            )
    except IntegrityError:
        return JsonResponse({"ok": False, "message": f"Анкор «{text}» уже есть."})
    return JsonResponse(
        {**anchor_json(keyword), "ok": True, "existing": False,
         "message": f"Анкор «{keyword.keyword}» добавлен."}
    )  # fmt: skip


def anchor_json(keyword: Keyword) -> dict[str, Any]:
    return {
        "id": keyword.pk,
        "keyword": keyword.keyword,
        "url": keyword.target_url,
        "page_type": keyword.page_type or "",
        "product": keyword.product_id,
        "naked": keyword.anchor_type in NAKED_TYPES,
    }


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


def summary_context(product_id: int | None, exclude: Iterable[int] = ()) -> dict[str, Any]:
    """Итоги, «цель / факт» по типам и рекомендации — для блока в размещении."""
    product = Product.objects.filter(pk=product_id).first() if product_id else None
    if product is None:
        return {"anchor_block": None}
    data = overview(product.pk)
    rules = anchor_recommend(product.pk)
    picks = recommend(
        data.anchors, data.shares, limit=rules.limit, skip_top=rules.skip_top, exclude=exclude
    )
    return {
        "anchor_block": {
            "product": product,
            "data": data,
            "types": [row for row in _type_rows(data.types) if row["stat"].target is not None],
            "picks": [_pick_json(p.anchor, p.reason) for p in picks],
            "skip_top": rules.skip_top,
            "anchors_url": reverse("admin:keywords_keywordcoverage_changelist")
            + f"?product={product.pk}",
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
    """Блок «Анкоры продукта» заново — сменили продукт в форме нового размещения."""
    exclude = [int(pk) for pk in request.GET.getlist("exclude") if pk.isdigit()]
    return TemplateResponse(
        request, "admin/keywords/anchor_summary.html", summary_context(product_id, exclude)
    )


def _digits(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0
