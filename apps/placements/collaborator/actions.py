"""Действия экрана сверки (E11-01, ADR-080).

- «Заполнить отмеченные» — у отмеченных сделок только пустые поля: номер
  заявки, адрес статьи, «Заплачено», анонс на главной;
- «Взять из Collaborator» — одно расходящееся поле сделки, анкоры — все ссылки
  размещения разом;
- «Оставить как в системе» и «Вернуть в сверку» — пометка у сделки в снимке.

Сверка пересчитывается заново по снимку и базе: записывается то, что видно
сейчас, а не то, что было на странице. Каждое действие, которое что-то поменяло,
— запись «Сверка с Collaborator» в «Загрузках» с журналом (ADR-060): «Отменить
загрузку» возвращает как было. Ничего не поменялось — записи нет, поэтому
повтор ничего не делает. Статус размещения действия не трогают: его меняет
человек в панели.

Размещения, которые сейчас правит другой человек (ADR-077), пропускаются.
«Заплачено» из счёта (ADR-055) не трогается.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpRequest
from django.utils import timezone

from apps.keywords import matching as keyword_matching
from apps.keywords import regions
from apps.placements.models import (
    CollaboratorDeal,
    InvoiceItem,
    InvoiceStatus,
    Placement,
    PlacementLink,
)
from apps.sites.models import Product, Seller, Upload, UploadKind, UploadStatus
from apps.sites.uploads.service import journal
from apps.workspace import locks
from config.run_id import current_run_id

from .deals import Deal, Link, Money
from .reconcile import FIELD_TEXT, Field, Fill, Row, Verdict, compare_links, kept_entry
from .service import Report, report

# Поле сверки → поля размещения, которые оно пишет.
COLUMNS = {
    Field.ORDER: ("collaborator_order_id",),
    Field.ARTICLE: ("article_url",),
    Field.PAID: ("price_paid_cents", "currency"),
    Field.ANNOUNCE: ("announce_on_homepage",),
}


@dataclass
class Outcome:
    """Итог действия — для сообщения на экране."""

    changed: dict[int, list[Field]] = field(default_factory=dict)  # размещение → поля
    domains: dict[int, str] = field(default_factory=dict)
    busy: list[tuple[str, locks.Holder]] = field(default_factory=list)
    upload: Upload | None = None
    note: str = ""

    def message(self) -> str:
        parts = []
        if self.changed:
            counts: dict[Field, int] = {}
            for fields in self.changed.values():
                for name in fields:
                    counts[name] = counts.get(name, 0) + 1
            what = ", ".join(f"{FIELD_TEXT[name]} — {count}" for name, count in counts.items())
            parts.append(f"Записано в размещения: {len(self.changed)} ({what}).")
        elif not self.busy:
            parts.append(self.note or "Записывать нечего: всё уже как на Collaborator.")
        if self.busy:
            parts.append(locks.skipped_text(self.busy))
        return " ".join(parts)


class _NothingChanged(Exception):
    pass


@contextmanager
def _journaled(request: HttpRequest, product: Product, title: str) -> Iterator[Upload]:
    """Запись сверки в «Загрузках» с журналом; ничего не поменялось — её нет."""
    try:
        with transaction.atomic():
            upload = Upload.objects.create(
                kind=UploadKind.COLLABORATOR,
                seller=Seller.collaborator(),
                product=product,
                prices_date=timezone.localdate(),
                file_name=title,
                file_path="",
                file_sha256="",
                status=UploadStatus.DONE,
                author=request.user if request.user.is_authenticated else None,
                run_id=current_run_id(),
                written_at=timezone.now(),
                journaled=True,
            )
            with journal(upload):
                yield upload
            if not upload.changes.exists():
                raise _NothingChanged
    except _NothingChanged:
        pass


def fill(request: HttpRequest, product: Product, deal_ids: Iterable[int]) -> Outcome:
    """«Заполнить отмеченные»: только пустые поля, по сверке на сейчас."""
    wanted = set(deal_ids)
    found = report(product)
    rows = [row for row in found.groups.fill if row.deal.id in wanted]
    outcome = Outcome(domains=found.domains)
    if not rows:
        outcome.note = "Заполнять нечего: у отмеченных сделок пустых полей больше нет."
        return outcome
    placements, busy = _free(request, [row.placement.id for row in rows])
    outcome.busy = [(p.site.domain, holder) for p, holder in busy]
    title = f"Сверка с Collaborator: заполнить ({len(rows)})"
    with _journaled(request, product, title) as upload:
        for row in rows:
            placement = placements.get(row.placement.id)
            if placement is None:
                continue
            changed = [fill.field for fill in row.fills if _fill(placement, fill)]
            if changed:
                _save(placement, changed)
                outcome.changed[placement.pk] = changed
        upload.result = _result("fill", outcome)
        upload.save(update_fields=["result"])
    outcome.upload = _kept_upload(outcome, upload)
    return outcome


def take(request: HttpRequest, product: Product, deal_id: int, what: Field) -> Outcome:
    """«Взять из Collaborator» — одно поле; анкоры — все ссылки размещения."""
    found = report(product)
    outcome = Outcome(domains=found.domains)
    row = found.row_of(deal_id)
    differ = row.differ_of(what) if row is not None else None
    if row is None or differ is None or differ.kept:
        outcome.note = "Уже совпадает: брать нечего."
        return outcome
    if not differ.takeable:
        outcome.note = (
            "«Заплачено» ставит счёт — правьте счёт."
            if what == Field.PAID
            else "Статус меняется в панели размещения."
        )
        return outcome
    placements, busy = _free(request, [row.placement.id])
    outcome.busy = [(p.site.domain, holder) for p, holder in busy]
    placement = placements.get(row.placement.id)
    if placement is None:
        return outcome
    title = f"Сверка с Collaborator: {FIELD_TEXT[what]} · {placement.site.domain}"
    with _journaled(request, product, title) as upload:
        if what == Field.ANCHORS:
            if _rewrite_links(placement, row.deal):
                outcome.changed[placement.pk] = [what]
        elif _take(placement, row.deal, what):
            _save(placement, [what])
            outcome.changed[placement.pk] = [what]
        upload.result = _result("take", outcome)
        upload.save(update_fields=["result"])
    outcome.upload = _kept_upload(outcome, upload)
    return outcome


def keep(request: HttpRequest, product: Product, deal_id: int, what: Field) -> str:
    """«Оставить как в системе»: расхождение не показывается, пока пара та же."""
    row = report(product).row_of(deal_id)
    differ = row.differ_of(what) if row is not None else None
    if differ is None:
        return "Уже совпадает: оставлять нечего."
    user = request.user.pk if request.user.is_authenticated else None
    entry = kept_entry(differ, user, timezone.now().isoformat())
    with transaction.atomic():
        stored = CollaboratorDeal.objects.select_for_update().filter(deal_id=deal_id).first()
        if stored is None:
            return "Сделки нет в снимке — обновите из Collaborator."
        stored.kept = {**stored.kept, what: entry}
        stored.save(update_fields=["kept"])
    return f"Оставлено как в системе: {FIELD_TEXT[what]}."


def unkeep(deal_id: int, what: Field) -> str:
    """«Вернуть в сверку»: расхождение снова видно."""
    with transaction.atomic():
        stored = CollaboratorDeal.objects.select_for_update().filter(deal_id=deal_id).first()
        if stored is None or what not in stored.kept:
            return "Возвращать нечего."
        stored.kept = {name: entry for name, entry in stored.kept.items() if name != what}
        stored.save(update_fields=["kept"])
    return f"Снова в сверке: {FIELD_TEXT[what]}."


def _free(
    request: HttpRequest, ids: list[int]
) -> tuple[dict[int, Placement], list[tuple[Placement, locks.Holder]]]:
    links = PlacementLink.objects.order_by("link_index", "pk")
    queryset = (
        Placement.objects.filter(pk__in=ids)
        .select_related("site")
        .prefetch_related(Prefetch("links", queryset=links))
        .order_by("pk")
    )
    free, busy = locks.split(
        request, queryset, lambda p: locks.placements_key(p.site_id, p.product_id)
    )
    return {p.pk: p for p in free}, busy


def _from_invoice(placement: Placement) -> bool:
    return (
        InvoiceItem.objects.filter(placement=placement)
        .exclude(invoice__status=InvoiceStatus.CANCELLED)
        .exists()
    )


def _fill(placement: Placement, fill: Fill) -> bool:
    """Записать значение, только если поле в базе всё ещё пусто."""
    value = fill.value
    if fill.field == Field.ORDER and not (placement.collaborator_order_id or "").strip():
        placement.collaborator_order_id = str(value)
        return True
    if fill.field == Field.ARTICLE and not (placement.article_url or "").strip():
        placement.article_url = str(value)
        return True
    if (
        fill.field == Field.PAID
        and placement.price_paid_cents is None
        and isinstance(value, Money)
        and not _from_invoice(placement)
    ):
        placement.price_paid_cents, placement.currency = value.cents, value.currency
        return True
    if fill.field == Field.ANNOUNCE and placement.announce_on_homepage is None:
        placement.announce_on_homepage = True
        return True
    return False


def _take(placement: Placement, deal: Deal, what: Field) -> bool:
    if what == Field.ORDER:
        placement.collaborator_order_id = str(deal.id)
    elif what == Field.ARTICLE and deal.article_url:
        placement.article_url = deal.article_url
    elif what == Field.PAID and deal.total is not None and not _from_invoice(placement):
        placement.price_paid_cents, placement.currency = deal.total.cents, deal.total.currency
    elif what == Field.ANNOUNCE:
        placement.announce_on_homepage = True
    else:
        return False
    return True


def _save(placement: Placement, fields: list[Field]) -> None:
    columns = [column for name in fields for column in COLUMNS.get(name, ())]
    # update_fields без статуса: статус площадки по размещению не двигается.
    placement.save(update_fields=[*columns, "updated_at"])


def _rewrite_links(placement: Placement, deal: Deal) -> bool:
    """Ссылки размещения — как анкоры сделки (ключ и вид — как в форме, ADR-069).

    Совпавшие остаются как есть, у «текст другой» и «адрес другой» меняются
    текст, адрес, ключ и вид, лишние удаляются, недостающие добавляются со
    следующим номером. Ключ ищется в списке анкоров размещения (ADR-075).
    """
    objects = list(placement.links.all())
    checks = compare_links([Link(o.anchor, o.target_url) for o in objects], deal.anchors)
    if all(check.verdict == Verdict.OK for check in checks):
        return False
    anchor_list = regions.for_placement(placement).anchor_list
    pool = (
        keyword_matching.candidates(placement.product_id, anchor_list.pk)
        if anchor_list is not None
        else []
    )
    unused = list(objects)

    def obj(link: Link | None) -> PlacementLink | None:
        for item in unused:
            if link is not None and (item.anchor, item.target_url) == (link.anchor, link.url):
                unused.remove(item)
                return item
        return None

    next_index = max((o.link_index or 0 for o in objects), default=0) + 1
    for check in checks:
        current = obj(check.system)
        wanted = check.collaborator
        if check.verdict == Verdict.OK:
            continue
        if check.verdict == Verdict.EXTRA and current is not None:
            current.delete()
            continue
        if wanted is None:
            continue
        found, kind = keyword_matching.bind(wanted.anchor, pool, url=wanted.url)
        keyword_id = found.keyword.id if found.keyword is not None else None
        if current is None:
            PlacementLink.objects.create(
                placement=placement,
                anchor=wanted.anchor,
                target_url=wanted.url,
                keyword_id=keyword_id,
                anchor_type=kind,
                link_index=next_index,
            )
            next_index += 1
            continue
        current.anchor, current.target_url = wanted.anchor, wanted.url
        current.keyword_id, current.anchor_type = keyword_id, kind
        current.save(update_fields=["anchor", "target_url", "keyword", "anchor_type"])
    if placement.anchor_list_id is None and anchor_list is not None:
        Placement.objects.filter(pk=placement.pk).update(anchor_list=anchor_list)
    return True


def _result(action: str, outcome: Outcome) -> dict[str, Any]:
    return {
        "action": action,
        "sites": len(outcome.changed),
        "placements": {str(pk): list(fields) for pk, fields in outcome.changed.items()},
    }


def _kept_upload(outcome: Outcome, upload: Upload) -> Upload | None:
    """Запись в «Загрузках» осталась, только если что-то поменялось."""
    return upload if outcome.changed and Upload.objects.filter(pk=upload.pk).exists() else None


def row_for_deal(product: Product, deal_id: int) -> tuple[Row | None, Report]:
    found = report(product)
    return found.row_of(deal_id), found
