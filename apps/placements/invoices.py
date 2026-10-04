"""Счета продавцов: доли размещений и «Заплачено» из счёта (ADR-055).

Счёт закрывает одно размещение или пачку. Доля — сколько из суммы счёта
приходится на размещение, в валюте счёта. Долю не вписали — остаток суммы
делится поровну до цента между такими строками.

«Заплачено» размещения (`price_paid_cents`) — сумма его долей в неотменённых
счетах. Его пишет только `sync_paid`: в форме размещения, в импорте таблицы и в
загрузках оно тогда не правится. Отменили счёт или убрали размещение из счёта,
а других счетов у размещения нет — «Заплачено» пусто.
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.placements.models import Invoice, InvoiceItem, InvoiceStatus, Placement
from apps.sites.offers import money


class ShareError(ValueError):
    """Доли строк не сходятся с суммой счёта."""


def shares(total_cents: int, given: Sequence[int | None], currency: str) -> list[int]:
    """Доли строк счёта: вписанные — как есть, пустые делят остаток поровну.

    Лишний цент от деления — первым пустым строкам: 100,00 на три —
    33,34 + 33,33 + 33,33. Вписанные доли больше суммы или все вписаны и не
    дают сумму — `ShareError` с тем, сколько лишнего или не хватает.
    """
    rest = total_cents - sum(value for value in given if value is not None)
    empty = [index for index, value in enumerate(given) if value is None]
    if rest < 0:
        raise ShareError(f"Доли больше суммы счёта на {money(-rest, currency)}.")
    if not empty:
        if given and rest:
            raise ShareError(
                f"Доли меньше суммы счёта на {money(rest, currency)}:"
                " впишите недостающее или оставьте долю пустой — поделим поровну."
            )
        return [value for value in given if value is not None]
    if rest == 0:
        # Новой строке в счёте, где доли уже дают всю сумму, молча достался бы 0.
        raise ShareError(
            "Вписанные доли уже дают всю сумму счёта, пустым строкам ничего не остаётся:"
            " впишите им 0 или «Поделить поровну»."
        )
    base, extra = divmod(rest, len(empty))
    result = list(given)
    for order, index in enumerate(empty):
        result[index] = base + (1 if order < extra else 0)
    return [value or 0 for value in result]


@dataclass(frozen=True)
class InvoiceRef:
    """Неотменённый счёт, где есть размещение: для формы размещения и проверок счёта."""

    invoice_id: int
    placement_id: int
    currency: str
    status: str
    label: str


def invoice_refs(placement_ids: Iterable[int], *, exclude: int | None = None) -> list[InvoiceRef]:
    """Неотменённые счета этих размещений, кроме счёта `exclude`."""
    items = (
        InvoiceItem.objects.filter(placement_id__in=list(placement_ids))
        .exclude(invoice__status=InvoiceStatus.CANCELLED)
        .select_related("invoice__seller")
        .order_by("invoice__issued_on", "invoice_id")
    )
    if exclude is not None:
        items = items.exclude(invoice_id=exclude)
    return [
        InvoiceRef(
            item.invoice_id,
            item.placement_id,
            item.invoice.currency,
            item.invoice.status,
            label(item.invoice),
        )
        for item in items
    ]


def label(invoice: Invoice) -> str:
    """«счёт № 12 от 30.09.2026, StarMedia» — для подсказок и ссылок."""
    number = f" № {invoice.number}" if invoice.number else ""
    return f"счёт{number} от {invoice.issued_on:%d.%m.%Y}, {invoice.seller}"


def item_errors(
    invoice: Invoice, placements: Sequence[Placement], *, currency: str, seller_id: int | None
) -> list[str]:
    """Что мешает записать эти размещения в счёт.

    Размещение куплено у другого продавца; размещение уже в другом счёте в
    другой валюте — «Заплачено» у размещения в одной валюте.
    """
    errors = []
    for placement in placements:
        if seller_id is not None and placement.seller_id not in (None, seller_id):
            errors.append(
                f"{placement.site} куплено у продавца «{placement.seller}» —"
                " в счёт другого продавца его не записать."
            )
    for other in invoice_refs([p.pk for p in placements], exclude=invoice.pk):
        if other.currency != currency:
            site = next(p.site for p in placements if p.pk == other.placement_id)
            errors.append(
                f"{site} уже в другом счёте ({other.label}) в {other.currency} —"
                " у размещения одна валюта «Заплачено»."
            )
    return errors


def sync_paid(placement_ids: Collection[int]) -> None:
    """Пересчитать «Заплачено» размещений по их неотменённым счетам.

    Есть такие счета — сумма долей в валюте счёта. Нет — пусто: размещения
    сюда передают, только когда их счёт записан, отменён или их убрали из
    счёта. Продавца у размещения без продавца ставит счёт.
    """
    if not placement_ids:
        return
    totals: dict[int, int] = defaultdict(int)
    currency: dict[int, str] = {}
    seller: dict[int, int] = {}
    items = (
        InvoiceItem.objects.filter(placement_id__in=placement_ids)
        .exclude(invoice__status=InvoiceStatus.CANCELLED)
        .values_list("placement_id", "amount_cents", "invoice__currency", "invoice__seller_id")
    )
    for placement_id, amount, invoice_currency, seller_id in items:
        totals[placement_id] += amount
        currency[placement_id] = invoice_currency
        seller[placement_id] = seller_id
    now = timezone.now()
    with transaction.atomic():
        for placement_id in placement_ids:
            rows = Placement.objects.filter(pk=placement_id)
            if placement_id in totals:
                rows.update(
                    price_paid_cents=totals[placement_id],
                    currency=currency[placement_id],
                    updated_at=now,
                )
                rows.filter(seller__isnull=True).update(seller_id=seller[placement_id])
            else:
                rows.exclude(price_paid_cents=None).update(price_paid_cents=None, updated_at=now)


def invoiced(placement_ids: Iterable[int]) -> set[int]:
    """Размещения, у которых «Заплачено» из счёта: импорт и загрузки его не трогают."""
    return set(
        InvoiceItem.objects.filter(placement_id__in=list(placement_ids))
        .exclude(invoice__status=InvoiceStatus.CANCELLED)
        .values_list("placement_id", flat=True)
    )


@dataclass
class SellerMoney:
    """Счета продавца: к оплате и заплачено — по валютам, суммы валют не складываются."""

    due: dict[str, int]
    due_count: int
    paid: dict[str, int]
    paid_count: int


def seller_money(rows: Iterable[Invoice]) -> SellerMoney:
    """Сводка по счетам продавца; отменённые не считаются."""
    result = SellerMoney({}, 0, {}, 0)
    for invoice in rows:
        code, cents = invoice.currency, invoice.amount_cents
        if invoice.status == InvoiceStatus.ISSUED:
            result.due[code] = result.due.get(code, 0) + cents
            result.due_count += 1
        elif invoice.status == InvoiceStatus.PAID:
            result.paid[code] = result.paid.get(code, 0) + cents
            result.paid_count += 1
    return result


def sums_text(sums: dict[str, int]) -> str:
    """«€181 + $50»: по валютам, евро первыми."""
    order = sorted(sums, key=lambda code: (code != "EUR", code))
    return " + ".join(money(sums[code], code) for code in order)
