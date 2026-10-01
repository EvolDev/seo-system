"""Цены, стрелки и значки для экранов площадок (E1-07, ADR-043).

Общие для «Площадок» и карточки площадки. Только показ: сравнение и
пересчёт в евро делают представления (`v_site_latest`, `v_site_offers`),
здесь — готовые числа в HTML. Стрелки как на бирже: ▼ зелёная —
дешевле, ▲ красная — дороже. Сравнивается только цена услуги.
"""

from dataclasses import dataclass

from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from apps.sites.offers import money

WRITING_HINT = "Площадка может написать статью сама"


@dataclass(frozen=True)
class Amount:
    """Сумма в исходной валюте и, для сравнения, в евро (пусто — курса нет)."""

    cents: int | None
    currency: str
    eur_cents: int | None

    @property
    def is_euro(self) -> bool:
        return self.currency == "EUR"


def price_html(amount: Amount) -> SafeString:
    """«€240» или «$210 ≈ €185»: исходная цена видна всегда."""
    if amount.cents is None:
        return mark_safe("—")
    if amount.is_euro or amount.eur_cents is None:
        return format_html("<b>{}</b>", money(amount.cents, amount.currency))
    return format_html(
        '<b>{}</b> <span class="seo-approx">≈&nbsp;{}</span>',
        money(amount.cents, amount.currency),
        money(round_euros(amount.eur_cents), "EUR"),
    )


def round_euros(cents: int) -> int:
    """Евро после пересчёта — до целых: копейки курса тут только шум."""
    return round(cents / 100) * 100


@dataclass(frozen=True)
class Delta:
    """Разница нового предложения с рабочей ценой: сумма, валюта, проценты."""

    cents: int
    currency: str
    percent: int
    approx: bool

    @property
    def up(self) -> bool:
        return self.cents > 0


def delta(new: Amount, ref: Amount) -> Delta | None:
    """В валюте предложения, если валюта та же, иначе — в евро по курсу."""
    if new.cents is None or ref.cents is None:
        return None
    if new.currency == ref.currency:
        diff, base, currency, approx = new.cents - ref.cents, ref.cents, new.currency, False
    elif new.eur_cents is not None and ref.eur_cents is not None:
        diff, base, currency, approx = new.eur_cents - ref.eur_cents, ref.eur_cents, "EUR", True
    else:
        return None
    if approx:
        diff = round_euros(diff)
    if diff == 0:
        return None
    percent = round(abs(diff) * 100 / base) if base else 0
    return Delta(diff, currency, percent, approx)


def delta_text(value: Delta) -> str:
    """«▼ €55 −23%» — для подсказок, где нет цвета."""
    arrow, sign = ("▲", "+") if value.up else ("▼", "−")
    approx = "≈" if value.approx else ""
    return f"{arrow} {approx}{money(abs(value.cents), value.currency)} {sign}{value.percent}%"


def delta_html(value: Delta | None, *, muted: bool = False) -> SafeString:
    """Стрелка с разницей: ▼ зелёная — дешевле, ▲ красная — дороже."""
    if value is None:
        return format_html('<span class="seo-flat">{}</span>', "= без изменений")
    kind = "seo-up" if value.up else "seo-down"
    if muted:
        kind += " seo-muted"
    arrow, sign = ("▲", "+") if value.up else ("▼", "−")
    approx = "≈" if value.approx else ""
    return format_html(
        '<span class="seo-delta {}">{} {}{} <small>{}{}%</small></span>',
        kind,
        arrow,
        approx,
        money(abs(value.cents), value.currency),
        sign,
        value.percent,
    )


def writing_html(cents: int | None, currency: str) -> SafeString:
    """«📝 €10», если площадка пишет сама; пусто — статью пишем мы."""
    if cents is None:
        return mark_safe("")
    amount = "бесплатно" if cents == 0 else money(cents, currency)
    return format_html('<span class="seo-writing" title="{}">📝 {}</span>', WRITING_HINT, amount)


def announce_text(cents: int | None, currency: str) -> str:
    if cents is None:
        return "нет"
    return "бесплатно" if cents == 0 else money(cents, currency)


def seller_mark(seller: str | None) -> SafeString:
    """Пометка «прод.»: доверенных замеров нет, цифры — со слов продавца."""
    title = f"Доверенных замеров нет — по словам продавца {seller or ''}".strip()
    return format_html('<span class="seo-seller-mark" title="{}">прод.</span>', title)
