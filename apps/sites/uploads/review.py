"""Разбор загрузки: вкладки, строки, решения и их отмена (ADR-043, ADR-044).

Вкладка строки — по положению на момент записи (`UploadItem.review_group`).
Решено ли — по самому предложению: `reviewed_at` и рабочая цена площадки.
Поэтому решение, принятое в «Площадках» или в карточке, разбор видит.

Решения — те же функции, что у карточки и «Площадок» (`apps.sites.offers`):
«Сделать рабочей» разбирает все неразобранные предложения этой услуги у
площадки, «Оставить» — только выбранное. Отмена возвращает рабочую цену и
снимает отметку «разобрано» ровно с тех предложений, которые решение
разобрало: это помнит страница и присылает обратно.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.core.paginator import Page, Paginator
from django.db import transaction
from django.db.models import (
    BooleanField,
    Case,
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Value,
    When,
)
from django.utils import timezone

from apps.placements.models import Placement
from apps.sites import offers
from apps.sites.display import Amount, Delta, delta
from apps.sites.models import (
    ProductSite,
    ReviewGroup,
    Site,
    SiteAudit,
    SiteMetric,
    SiteNote,
    SitePrice,
    SiteStatus,
    StatusSource,
    Upload,
    UploadItem,
)
from apps.sites.rates import latest_rates, to_eur_cents
from apps.sites.uploads.plan import REJECTED_STATUSES, refusal_text
from apps.sites.uploads.plan import blocked_ids as plan_blocked_ids
from config.changes import bind_change, stamped

PAGE_SIZE = 100
ISSUES = "issues"
# Убрать площадку из разбора: удалить из базы или заблокировать для загрузок.
REMOVE_ACTIONS = ("delete", "block")
# Вкладки, где бывают решения; «Новые» и «Без изменений» — только посмотреть.
DECIDE = (
    ReviewGroup.CHEAPER,
    ReviewGroup.CHANGED,
    ReviewGroup.REJECTED,
    ReviewGroup.PRICIER,
    ReviewGroup.OTHER_SERVICE,
)
TAB_NOTES: dict[str, str] = {
    ReviewGroup.CHEAPER: (
        "Другой продавец предлагает ту же услугу дешевле рабочей цены. Сравниваем только цену "
        "размещения в евро по курсу ЕЦБ; написание и анонс — справочно."
    ),
    ReviewGroup.CHANGED: (
        "Тот же продавец прислал другую цену. ▼ — подешевело, ▲ — подорожало. Рабочая цена "
        "переходит на новую сама; решать нужно, только если по площадке заявка в работе."
    ),
    ReviewGroup.NEW: (
        "Площадок не было в базе или у них не было цены. Рабочей стала первая цена — публикация, "
        "если она есть. Площадки добавлены в рабочий список загрузки."
    ),
    ReviewGroup.REJECTED: (
        "Площадки, от которых мы отказывались, которые отказали нам или в чёрном списке. Цена "
        "записана, рабочую можно сменить, но сначала посмотрите причину."
    ),
    ReviewGroup.PRICIER: "Другой продавец предлагает дороже рабочей цены. Обычно — «Оставить».",
    ReviewGroup.OTHER_SERVICE: (
        "У площадки рабочая цена за другую услугу (например, публикация), а в файле — вставка "
        "ссылки. Не сравниваем, решаете вы."
    ),
    ReviewGroup.SAME: "Цена та же, что уже есть. Решать ничего не нужно.",
    ISSUES: "Строки, которые не удалось разобрать, площадки, которые в файле встречаются дважды, "
    "и адреса вместо доменов.",
}
SORTS = {"gain": "сначала самая большая выгода", "dr": "по DR"}
FILE_SORTS = {"gain": "как в файле", "dr": "по DR"}
# Вкладки, где у строки есть разница с рабочей ценой той же услуги.
GAIN_GROUPS = (ReviewGroup.CHEAPER, ReviewGroup.CHANGED, ReviewGroup.REJECTED, ReviewGroup.PRICIER)


class EurRate(Func):
    """`eur_rate(валюта)` из `schema.sql`: последний курс ЕЦБ за 1 евро."""

    function = "eur_rate"
    output_field = DecimalField()


@dataclass(frozen=True)
class Tab:
    key: str
    title: str
    pending: int  # ждут решения
    total: int

    @property
    def badge(self) -> int:
        return self.pending if self.pending else self.total


@dataclass
class Row:
    item: UploadItem
    state: str  # pending, working, kept, auto, info
    label: str
    price: Amount
    ref: Amount | None
    change: Delta | None
    dr: int | None = None
    traffic: int | None = None
    metrics_seller: str | None = None  # метрики со слов продавца — пометка «прод.»
    rejected: list[str] = field(default_factory=list)

    @property
    def decidable(self) -> bool:
        return self.state == "pending"


def tabs(upload: Upload) -> list[Tab]:
    stats: dict[str, dict[str, Any]] = {
        row["review_group"]: dict(row)
        for row in visible(UploadItem.objects.filter(upload=upload))
        .values("review_group")
        .annotate(total=Count("pk"), pending=Count("pk", filter=_pending_q()))
    }
    result = []
    for group in (
        ReviewGroup.CHEAPER,
        ReviewGroup.CHANGED,
        ReviewGroup.NEW,
        ReviewGroup.REJECTED,
        ReviewGroup.PRICIER,
        ReviewGroup.OTHER_SERVICE,
        ReviewGroup.SAME,
    ):
        stat: dict[str, Any] = stats.get(group, {})
        result.append(Tab(group, str(group.label), stat.get("pending", 0), stat.get("total", 0)))
    issues = _issues_count(upload)
    result.append(Tab(ISSUES, "Ошибки и дубли", 0, issues))
    return result


def progress(upload: Upload) -> tuple[int, int]:
    """(разобрано, ждало решения) — по строкам, которым при записи нужно было решение."""
    stats = visible(UploadItem.objects.filter(upload=upload, needs_decision=True)).aggregate(
        total=Count("pk"), pending=Count("pk", filter=_pending_q())
    )
    return stats["total"] - stats["pending"], stats["total"]


def first_tab(upload: Upload, all_tabs: Sequence[Tab]) -> str:
    """Вкладка по умолчанию: первая, где ждут решения, иначе первая непустая."""
    for tab in all_tabs:
        if tab.pending:
            return tab.key
    for tab in all_tabs:
        if tab.total:
            return tab.key
    return ReviewGroup.NEW


def page(
    upload: Upload, group: str, *, sort: str = "gain", hide_done: bool = True, number: int = 1
) -> tuple[Page[UploadItem], list[Row]]:
    """Страница строк вкладки: сначала ждущие решения, затем по выгоде или DR."""
    queryset = visible(UploadItem.objects.filter(upload=upload, review_group=group)).select_related(
        "site__price__seller", "price__seller", "ref_price__seller"
    )
    if hide_done:
        queryset = queryset.exclude(needs_decision=True, price__reviewed_at__isnull=False)
    queryset = queryset.annotate(
        waiting=Case(
            When(_pending_q(), then=Value(0)), default=Value(1), output_field=IntegerField()
        )
    )
    if sort == "gain" and group not in GAIN_GROUPS:
        # Разницы с рабочей ценой здесь нет — порядок строк файла. Пересчёт
        # в евро на 45 000 строк каталога стоил бы полторы секунды впустую.
        queryset = queryset.order_by("waiting", "line", "pk")
    elif sort == "dr":
        queryset = queryset.annotate(dr=Subquery(_latest_metric().values("dr")[:1])).order_by(
            "waiting", F("dr").desc(nulls_last=True), "pk"
        )
    else:
        gain = ExpressionWrapper(
            F("price__placement_cents") / EurRate(F("price__currency"))
            - F("ref_price__placement_cents") / EurRate(F("ref_price__currency")),
            output_field=DecimalField(),
        )
        queryset = queryset.annotate(gain=gain).order_by(
            "waiting", F("gain").asc(nulls_last=True), "pk"
        )
    paginator: Paginator[UploadItem] = Paginator(queryset, PAGE_SIZE)
    current = paginator.get_page(number)
    return current, rows(list(current.object_list))


def rows(items: Sequence[UploadItem]) -> list[Row]:
    rates = latest_rates()
    site_ids = [item.site_id for item in items]
    metrics = _metrics(site_ids)
    rejected = _rejected(site_ids)
    result = []
    for item in items:
        price = _amount(item.price, rates)
        ref = _amount(item.ref_price, rates) if item.ref_price is not None else None
        change = (
            delta(price, ref)
            if ref is not None and item.review_group != ReviewGroup.OTHER_SERVICE
            else None
        )
        state, label = _state(item)
        dr, traffic, seller = metrics.get(item.site_id, (None, None, None))
        result.append(
            Row(
                item=item,
                state=state,
                label=label,
                price=price,
                ref=ref,
                change=change,
                dr=dr,
                traffic=traffic,
                metrics_seller=seller,
                rejected=rejected.get(item.site_id, []),
            )
        )
    return result


def decide(
    upload: Upload, item_ids: Iterable[int], action: str, *, author: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    """Решение по строкам разбора. Возвращает, что вернуть при отмене, и что не вышло.

    - `fix` — «Сделать рабочей», `keep` — «Оставить»: по ценам строк, ждущих решения;
    - `delete` — «Удалить из базы»: пометка «удалена», только без истории;
    - `block` — «Заблокировать»: чёрный список у всех продуктов, загрузки её пропускают.
    """
    if action in REMOVE_ACTIONS:
        # Блокировка меняет статусы — в истории это «загрузка» (ADR-049).
        with bind_change(StatusSource.UPLOAD, _actor_id(author)):
            return _remove(upload, item_ids, action, author=author)
    items = list(
        UploadItem.objects.filter(upload=upload, pk__in=list(item_ids), needs_decision=True)
        .select_related("price")
        .order_by("pk")
    )
    undo: list[dict[str, Any]] = []
    for item in items:
        with transaction.atomic():
            site = Site.all_objects.select_for_update().get(pk=item.site_id)
            before = site.price_id
            pending = set(_pending_ids(site.pk))
            if action == "fix":
                offers.set_working_price(item.price, author=author)
            else:
                offers.keep_current([item.price])
            reviewed = sorted(pending - set(_pending_ids(site.pk)))
        undo.append(
            {
                "type": "price",
                "site": site.pk,
                "price": before,
                "reviewed": reviewed,
                "item": item.pk,
            }
        )
    return undo, []


def history(site_id: int) -> list[str]:
    """Что у площадки уже было с нами — такую не удаляют, только блокируют."""
    found: list[str] = []
    if Placement.objects.filter(site_id=site_id).exists():
        found.append("размещения")
    if SiteAudit.objects.filter(site_id=site_id).exists():
        found.append("аудиты")
    if ProductSite.objects.filter(site_id=site_id).exclude(status=SiteStatus.NEW).exists():
        found.append("решения по продуктам")
    if SiteNote.objects.filter(site_id=site_id, author__isnull=False).exists():
        found.append("ваши заметки")
    return found


def _remove(
    upload: Upload, item_ids: Iterable[int], action: str, *, author: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    sites: dict[int, int] = {}
    for item in UploadItem.objects.filter(upload=upload, pk__in=list(item_ids)).order_by("pk"):
        sites.setdefault(item.site_id, item.pk)
    undo: list[dict[str, Any]] = []
    problems: list[str] = []
    source = f"разбор загрузки «{upload.file_name}»"
    for site_id, item_id in sites.items():
        with transaction.atomic():
            site = Site.all_objects.select_for_update().get(pk=site_id)
            if action == "delete":
                found = history(site_id)
                if found:
                    what = ", ".join(found)
                    problems.append(
                        f"{site.domain}: есть {what} — удалить нельзя, только блокировать"
                    )
                    continue
            pending = _pending_ids(site_id)
            SitePrice.objects.filter(pk__in=pending).update(reviewed_at=timezone.now())
            entry: dict[str, Any] = {
                "type": action, "site": site_id, "reviewed": pending, "item": item_id,
            }  # fmt: skip
            if action == "delete":
                site.is_deleted = True
                site.save(update_fields=["is_deleted", "updated_at"])
                offers.add_note(site, f"Удалена из базы — {source}", author=author)
            else:
                rows = ProductSite.objects.filter(site_id=site_id, product__is_active=True)
                entry["statuses"] = {
                    str(row.pk): [row.status, row.imported_undecided] for row in rows
                }
                for row in rows:
                    row.status = SiteStatus.BLACKLISTED
                    row.save(update_fields=["status", "updated_at"])
                offers.add_note(
                    site, f"Заблокирована — {source}: загрузки её пропускают", author=author
                )
        undo.append(entry)
    return undo, problems


def undo(entries: Iterable[dict[str, Any]], *, author: Any) -> int:
    """Отмена решений: рабочая цена, «не разобрано», пометка «удалена», статусы до блокировки."""
    done = 0
    for entry in entries:
        site_id = int(entry["site"])
        reviewed = [int(pk) for pk in entry.get("reviewed") or []]
        kind = entry.get("type", "price")
        with transaction.atomic():
            site = Site.all_objects.select_for_update().get(pk=site_id)
            if kind == "delete" and site.is_deleted:
                site.is_deleted = False
                site.save(update_fields=["is_deleted", "updated_at"])
                offers.add_note(site, "Возвращена в базу — отмена удаления", author=author)
            elif kind == "block":
                # Статус до блокировки — в истории «загрузка», от того, кто отменил.
                with (
                    bind_change(StatusSource.UPLOAD, _actor_id(author)),
                    stamped(),
                ):
                    for pk, (status, undecided) in (entry.get("statuses") or {}).items():
                        ProductSite.objects.filter(pk=int(pk), site_id=site_id).update(
                            status=status, imported_undecided=bool(undecided)
                        )
                offers.add_note(site, "Разблокирована — отмена блокировки", author=author)
            elif kind == "price":
                price_id = entry.get("price")
                if price_id is not None and site.price_id != int(price_id):
                    previous = SitePrice.objects.filter(pk=int(price_id), site_id=site_id).first()
                    if previous is not None:
                        offers.set_working_price(previous, author=author)
            # Отменяем после смены цены: смена разбирает предложения этой услуги.
            SitePrice.objects.filter(pk__in=reviewed, site_id=site_id).update(reviewed_at=None)
        done += 1
    return done


def row_states(item_ids: Iterable[int]) -> dict[int, dict[str, str]]:
    """Состояние строк после решения — для обновления страницы без перезагрузки.

    Строки удалённых и заблокированных площадок — `removed`: страница их убирает.
    """
    items = list(UploadItem.objects.filter(pk__in=list(item_ids)).select_related("site", "price"))
    blocked = plan_blocked_ids(item.site_id for item in items)
    result = {}
    for item in items:
        if item.site.is_deleted:
            result[item.pk] = {"state": "removed", "label": "удалена из базы"}
        elif item.site_id in blocked:
            result[item.pk] = {"state": "removed", "label": "заблокирована"}
        else:
            state, label = _state(item)
            result[item.pk] = {"state": state, "label": label}
    return result


def visible(queryset: QuerySet[UploadItem]) -> QuerySet[UploadItem]:
    """Без удалённых и заблокированных площадок: из разбора они пропадают."""
    active = ProductSite.objects.filter(site_id=OuterRef("site_id"), product__is_active=True)
    return (
        queryset.filter(site__is_deleted=False)
        .annotate(
            _any_product=Exists(active),
            _not_black=Exists(active.exclude(status=SiteStatus.BLACKLISTED)),
        )
        .exclude(_any_product=True, _not_black=False)
    )


def _state(item: UploadItem) -> tuple[str, str]:
    working = item.site.price_id == item.price_id
    if item.needs_decision:
        if item.price.reviewed_at is None:
            return "pending", ""
        return ("working", "✓ рабочая цена") if working else ("kept", "оставлена прежняя")
    if item.auto_applied:
        return "auto", "стала рабочей сама" if working else "была рабочей, сменили"
    if item.review_group == ReviewGroup.NEW:
        return "info", "рядом с рабочей" if not working else "рабочая цена"
    if item.review_group in DECIDE:
        return "info", "решать не нужно: вы уже видели почти ту же цену"
    return "info", "решать не нужно"


def _pending_q() -> Q:
    return Q(needs_decision=True, price__reviewed_at__isnull=True)


def _pending_ids(site_id: int) -> list[int]:
    return list(
        SitePrice.objects.filter(site_id=site_id, reviewed_at__isnull=True).values_list(
            "pk", flat=True
        )
    )


def _amount(price: SitePrice, rates: dict[str, Decimal]) -> Amount:
    cents = price.placement_cents
    eur = to_eur_cents(cents, price.currency, rates) if cents is not None else None
    return Amount(cents, price.currency, eur)


def _latest_metric() -> QuerySet[SiteMetric]:
    """Последний замер площадки: доверенный, нет такого — со слов продавца (как в v_site_latest)."""
    trusted = ExpressionWrapper(
        Q(seller__isnull=True) | Q(seller__metrics_trusted=True), output_field=BooleanField()
    )
    return (
        SiteMetric.objects.filter(site_id=OuterRef("site_id"))
        .annotate(
            trusted=trusted,
            ours=ExpressionWrapper(Q(seller__isnull=True), output_field=BooleanField()),
        )
        .order_by("-trusted", "-checked_at", "-ours", "-pk")
    )


def _metrics(site_ids: Sequence[int]) -> dict[int, tuple[int | None, int | None, str | None]]:
    trusted = ExpressionWrapper(
        Q(seller__isnull=True) | Q(seller__metrics_trusted=True), output_field=BooleanField()
    )
    latest = (
        SiteMetric.objects.filter(site_id__in=list(site_ids))
        .annotate(
            trusted=trusted,
            ours=ExpressionWrapper(Q(seller__isnull=True), output_field=BooleanField()),
        )
        .select_related("seller")
        .order_by("site_id", "-trusted", "-checked_at", "-ours", "-pk")
        .distinct("site_id")
    )
    result = {}
    for metric in latest:
        seller = (
            None
            if getattr(metric, "trusted", True)
            else (metric.seller.name if metric.seller else None)
        )
        result[metric.site_id] = (metric.dr, metric.organic_traffic, seller)
    return result


def _rejected(site_ids: Sequence[int]) -> dict[int, list[str]]:
    rows = (
        ProductSite.objects.filter(
            site_id__in=list(site_ids), status__in=REJECTED_STATUSES, product__is_active=True
        )
        .select_related("product")
        .order_by("product__name")
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row.site_id, []).append(refusal_text(row))
    return result


def _issues_count(upload: Upload) -> int:
    data = upload.summary or {}
    return sum(int(data.get(f"{name}_total") or 0) for name in ("errors", "duplicates", "urls"))


def _actor_id(author: Any) -> int | None:
    """Пользователь для истории статусов; без входа (тесты, команды) — пусто."""
    pk = getattr(author, "pk", None)
    return int(pk) if pk is not None else None
