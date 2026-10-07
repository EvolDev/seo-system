"""Цены, стрелки и значки для экранов площадок (E1-07, ADR-043).

Общие для «Площадок» и карточки площадки. Только показ: сравнение и
пересчёт в евро делают представления (`v_site_latest`, `v_site_offers`),
здесь — готовые числа в HTML. Стрелки как на бирже: ▼ зелёная —
дешевле, ▲ красная — дороже. Сравнивается только цена услуги.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.urls import reverse
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


# Значки у домена (E2-06, по просьбе пользователя): открыть сайт в новой вкладке
# и скопировать домен. SVG, а не эмодзи: эмодзи Windows рисует по-своему.
_OPEN_ICON = mark_safe(
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'
    "</svg>"
)
_COPY_ICON = mark_safe(
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="9" y="9" width="11" height="11" rx="2"/>'
    '<path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>'
)


# Значки размещения (E1-20): «взять в размещение» — лист со знаком плюс,
# «карточка размещения» — лист с текстом, «убрать из размещений» — корзина.
_PLACE_ICON = mark_safe(
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
    '<path d="M14 3v5h5M12 11v6M9 14h6"/></svg>'
)
_PLACEMENT_ICON = mark_safe(
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
    '<path d="M14 3v5h5M9 13h6M9 17h4"/></svg>'
)
_REMOVE_ICON = mark_safe(
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 7h16M10 11v6M14 11v6"/>'
    '<path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>'
    "</svg>"
)


# Оценка площадки (E1-21): звезда с числом перед доменом. Полная звезда — есть
# оценки, контурная — нет. Нажатие открывает окошко выбора (seo/rating.js).
_STAR_FULL = mark_safe(
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="currentColor">'
    '<path d="M12 2.5l2.9 5.9 6.6.9-4.8 4.6 1.2 6.5-5.9-3.1-5.9 3.1 1.2-6.5L2.5 9.3l6.6-.9z"/>'
    "</svg>"
)
_STAR_EMPTY = mark_safe(
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linejoin="round">'
    '<path d="M12 2.5l2.9 5.9 6.6.9-4.8 4.6 1.2 6.5-5.9-3.1-5.9 3.1 1.2-6.5L2.5 9.3l6.6-.9z"/>'
    "</svg>"
)


def rating_title(
    average: Decimal | None, count: int, mine: int | None, what: str = "площадку"
) -> str:
    """Подсказка звезды: среднее, сколько оценок и своя, если она есть.

    «оценок: 7», а не «7 оценок»: склонение по числу потребовало бы своего
    помощника, которого в проекте нет, а ради подсказки он лишний.
    """
    if not count or average is None:
        return f"Оценить {what}"
    own = f" · ваша {mine}" if mine else ""
    return f"{average} · оценок: {count}{own}"


def rating_html(
    url: str,
    average: Decimal | None,
    count: int,
    mine: int | None = None,
    what: str = "площадку",
) -> SafeString:
    """Звезда с числом перед доменом; число — одна цифра после точки.

    Без оценок число не рисуем вовсе: «0.0» читалось бы как плохая площадка,
    а это «никто не оценивал». Своя оценка — отдельным классом, по ней
    окошко подсвечивает выбранное.
    """
    rated = bool(count) and average is not None
    return format_html(
        '<button type="button" class="seo-rating{}" data-rate="{}" data-mine="{}"'
        ' title="{}" aria-label="{}">{}{}</button>',
        "" if rated else " seo-rating-empty",
        url,
        mine or "",
        rating_title(average, count, mine, what),
        rating_title(average, count, mine, what),
        _STAR_FULL if rated else _STAR_EMPTY,
        format_html('<span class="seo-rating-value">{}</span>', average) if rated else "",
    )


def seller_cell(seller_id: int | None, name: str, marks: "dict[int, Any]") -> SafeString:
    """Продавец в таблице предложений: звезда оценки и имя ссылкой (E1-24).

    Имя открывает карточку продавца **в новой вкладке**: карточка площадки сама
    живёт в панели поверх списка, и увести её на другую запись значит потерять
    место, куда человек смотрел (просьба пользователя 07.10.2026).
    """
    if seller_id is None:
        return format_html("<b>{}</b>", name)
    average, count, mine = marks.get(seller_id, (None, 0, None))
    return format_html(
        '{}<a href="{}" target="_blank" rel="noopener" title="Карточка продавца"><b>{}</b></a>',
        rating_html(
            reverse("admin:sites_seller_rate", args=[seller_id]),
            average,
            count,
            mine,
            "продавца",
        ),
        reverse("admin:sites_seller_change", args=[seller_id]),
        name,
    )


def site_url(domain: str) -> str:
    """Адрес сайта площадки: домен хранится без схемы и `www.`, сайт сам перенаправит."""
    return f"https://{domain}/"


def open_site_html(domain: str) -> SafeString:
    """Значок «открыть сайт в новой вкладке»."""
    return format_html(
        '<a class="seo-icon-btn" href="{}" target="_blank" rel="noopener noreferrer"'
        ' title="Открыть сайт в новой вкладке" aria-label="Открыть {} в новой вкладке">{}</a>',
        site_url(domain),
        domain,
        _OPEN_ICON,
    )


def copy_domain_html(domain: str) -> SafeString:
    """Кнопка «скопировать домен» — копирует seo/domain-tools.js."""
    return format_html(
        '<button type="button" class="seo-icon-btn" data-copy="{}" data-domain="{}"'
        ' title="Скопировать домен" aria-label="Скопировать {}">{}</button>',
        domain,
        domain,
        domain,
        _COPY_ICON,
    )


def take_placement_html(url: str) -> SafeString:
    """Значок «взять в размещение»: откроется пустая форма под рабочий продукт."""
    return format_html(
        '<a class="seo-icon-btn" href="{}" data-panel title="Взять в размещение"'
        ' aria-label="Взять в размещение">{}</a>',
        url,
        _PLACE_ICON,
    )


def placement_card_html(url: str) -> SafeString:
    """Значок «карточка размещения» — когда размещение рабочего продукта уже есть."""
    return format_html(
        '<a class="seo-icon-btn" href="{}" data-panel title="Карточка размещения"'
        ' aria-label="Карточка размещения">{}</a>',
        url,
        _PLACEMENT_ICON,
    )


def remove_placement_html(url: str) -> SafeString:
    """Значок «убрать из размещений»: страница подтверждения удаления (ADR-060)."""
    return format_html(
        '<a class="seo-icon-btn" href="{}" title="Убрать из размещений"'
        ' aria-label="Убрать из размещений">{}</a>',
        url,
        _REMOVE_ICON,
    )


def domain_tools_html(domain: str, extra: SafeString | str = "") -> SafeString:
    """Значки рядом с доменом в списке; `extra` — значок самого списка (E1-20)."""
    return format_html(
        '<span class="seo-domain-tools">{}{}{}</span>',
        open_site_html(domain),
        copy_domain_html(domain),
        extra,
    )
