"""Сделки Collaborator: разбор ответа API и снимок в базе (E11-01, ADR-079).

Сделка в API — объект `deal/list` (поля — `docs/specs/browser-plugin.md`):
номер, проект, площадка (`creator`), статус текстом, анкоры, адрес статьи и
цены строками «571.64 EUR». Total (`price`) — всегда сумма частей: размещение,
написание, анонс, страховка. Дата публикации на Collaborator
(`dateOfPlacement`) — только справка: «Опубликовано» ставит статус «Размещено»
в день закрытия (ADR-080).

Снимок (`CollaboratorDeal`) обновляет `refresh` целиком: все страницы API за
одно нажатие, одной транзакцией. Не дошла хоть одна страница — снимок прежний.
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.integrations.collaborator import CollaboratorClient
from apps.integrations.collaborator.client import Progress
from apps.placements.models import CollaboratorDeal

POSTED = "Posted"
PENDING = "Pending approval"
# Сделка отменена — в системе это «Отказ», почему — пишет человек (08.11.2026).
CANCELLED = frozenset({"Rejected", "Rejected by the owner", "Withdrawn"})
# Статья готова, деньги не вернутся: «Заплачено» и анонс берём только у таких.
ARTICLE_READY = frozenset({PENDING, POSTED})


@dataclass(frozen=True)
class Money:
    cents: int
    currency: str


@dataclass(frozen=True)
class Link:
    anchor: str
    url: str


@dataclass(frozen=True)
class Deal:
    id: int
    status: str
    domain: str
    project_id: int | None = None
    project_name: str = ""
    card_id: int | None = None
    card_url: str = ""
    anchors: tuple[Link, ...] = ()
    article_url: str = ""
    total: Money | None = None
    publication: Money | None = None
    writing: Money | None = None
    announce: Money | None = None
    insurance: Money | None = None
    created_at: str = ""
    placed_at: str = ""
    publication_type: str = ""
    # «Оставить как в системе»: поле → {"pair": [в системе, на Collaborator], …}.
    kept: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def cancelled(self) -> bool:
        return self.status in CANCELLED

    @property
    def article_ready(self) -> bool:
        return self.status in ARTICLE_READY

    @property
    def parts(self) -> list[tuple[str, Money]]:
        """Из чего сложен Total — для блока в панели: «543.53 + 28.11»."""
        named = (
            ("размещение", self.publication),
            ("написание", self.writing),
            ("анонс", self.announce),
            ("страховка", self.insurance),
        )
        return [(name, money) for name, money in named if money is not None and money.cents]


def money(text: Any) -> Money | None:
    """«571.64 EUR» → 57164 цента и валюта; пусто или не сумма — None."""
    if not isinstance(text, str) or not text.strip():
        return None
    amount, _, currency = text.strip().partition(" ")
    try:
        cents = int((Decimal(amount) * 100).to_integral_value())
    except InvalidOperation:
        return None
    currency = currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        return None
    return Money(cents, currency)


def parse(item: Mapping[str, Any], kept: Mapping[str, Any] | None = None) -> Deal:
    """Сделка из объекта API или из снимка; без номера — ValueError."""
    deal_id = item.get("id")
    if not isinstance(deal_id, int) or isinstance(deal_id, bool):
        raise ValueError(f"у сделки нет номера: {deal_id!r}")
    project = _object(item, "project")
    creator = _object(item, "creator")
    anchors = tuple(
        Link(str(a.get("anchor") or ""), str(a.get("url") or ""))
        for a in item.get("anchors") or []
        if isinstance(a, dict)
    )
    return Deal(
        id=deal_id,
        status=str(item.get("status") or ""),
        domain=str(creator.get("name") or "").strip(),
        project_id=_int(project.get("id")),
        project_name=str(project.get("name") or ""),
        card_id=_int(creator.get("id")),
        card_url=str(creator.get("url") or ""),
        anchors=anchors,
        article_url=str(item.get("urlOfPublishedArticle") or "").strip(),
        total=money(item.get("price")),
        publication=money(item.get("pricePublication")),
        writing=money(item.get("priceSpelling")),
        announce=money(item.get("priceAnnouncement")),
        insurance=money(item.get("priceInsurance")),
        created_at=str(item.get("created_at") or ""),
        placed_at=str(item.get("dateOfPlacement") or ""),
        publication_type=str(item.get("publicationType") or ""),
        kept=dict(kept or {}),
    )


@dataclass(frozen=True)
class Refreshed:
    deals: int
    added: int
    removed: int
    at: dt.datetime


def refresh(client: CollaboratorClient, on_page: Progress | None = None) -> Refreshed:
    """Забрать все сделки и заменить ими снимок. «Оставлено как в системе» сохраняется."""
    items = client.deals(on_page)
    deals = [(item, parse(item)) for item in items]
    now = timezone.now()
    with transaction.atomic():
        before = set(CollaboratorDeal.objects.values_list("deal_id", flat=True))
        CollaboratorDeal.objects.bulk_create(
            [
                CollaboratorDeal(
                    deal_id=deal.id,
                    project_id=deal.project_id,
                    status=deal.status,
                    data=_stored(item),
                    fetched_at=now,
                )
                for item, deal in deals
            ],
            update_conflicts=True,
            unique_fields=["deal_id"],
            update_fields=["project_id", "status", "data", "fetched_at"],
        )
        ids = {deal.id for _, deal in deals}
        removed, _ = CollaboratorDeal.objects.exclude(deal_id__in=ids).delete()
    return Refreshed(deals=len(ids), added=len(ids - before), removed=removed, at=now)


def snapshot() -> tuple[list[Deal], dt.datetime | None]:
    """Сделки из снимка и когда их забрали; снимка нет — пусто и None."""
    rows = list(CollaboratorDeal.objects.order_by("deal_id"))
    deals = [parse(row.data, row.kept) for row in rows]
    fetched = max((row.fetched_at for row in rows), default=None)
    return deals, fetched


def _stored(item: Mapping[str, Any]) -> dict[str, Any]:
    """Сделка для снимка: без текста нашей статьи — он большой и сверке не нужен."""
    stored = dict(item)
    task = stored.get("task")
    if isinstance(task, dict):
        stored["task"] = {key: value for key, value in task.items() if key != "contentHtml"}
    return stored


def _object(item: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def by_id(deals: Iterable[Deal]) -> dict[int, Deal]:
    return {deal.id: deal for deal in deals}
