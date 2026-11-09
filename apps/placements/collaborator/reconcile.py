"""Сверка сделки Collaborator с её размещением (E11-01, ADR-080).

Чистые функции: сделка и размещение — на входе, что заполнить и что
расходится — на выходе. Записывают действия (`actions.py`), показывают экраны.

**Готова к закрытию** — на Collaborator Pending approval и есть статья, в
системе «Заявка отправлена» или «Написание статьи». Сама система статус не
двигает: закрывает человек в панели («Размещено» → «Сохранить»), дата
публикации — день закрытия.

**Можно заполнить** — поле пусто в системе, а на Collaborator значение есть:
- номер заявки — номер сделки;
- адрес статьи;
- анонс на главной (если анонс платный) — только когда статья готова (Pending
  approval, Posted);
- «Заплачено» = Total — только у размещения «Размещено» и сделки с готовой
  статьёй (пользователь, 09.11.2026): у заявки его ставит закрытие в панели —
  «Заполнить из Collaborator», потом «Размещено» (`closing_fills`). «Заплачено»
  из счёта (ADR-055) не трогается.

**Расходится** — значение есть с обеих сторон, но другое; анкоры — по каждой
ссылке; статус — на Collaborator опубликовано или отказ, а в системе другое.
Отменённая сделка — только статус: в системе это «Отказ», почему — пишет
человек (08.11.2026). «Оставить как в системе» прячет расхождение, пока пара
значений (в системе, на Collaborator) та же.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from apps.keywords.matching import normalize, same_url
from apps.placements.indexation import normalize_url
from apps.sites.models import WorkStatus

from .deals import PENDING, POSTED, Deal, Link, Money
from .matching import Facts, Matched

# Статусы размещения, которые закрывает «Готова к закрытию».
CLOSABLE = frozenset({WorkStatus.ORDERED, WorkStatus.WRITING})
# Размещение ещё живо — отменённая сделка с ним расходится.
LIVE = frozenset({WorkStatus.IN_WORK, WorkStatus.ORDERED, WorkStatus.WRITING, WorkStatus.PLACED})


class Field(StrEnum):
    ORDER = "order"
    ARTICLE = "article"
    PAID = "paid"
    ANNOUNCE = "announce"
    ANCHORS = "anchors"
    STATUS = "status"


FIELD_TEXT = {
    Field.ORDER: "номер заявки",
    Field.ARTICLE: "адрес статьи",
    Field.PAID: "«Заплачено»",
    Field.ANNOUNCE: "анонс на главной",
    Field.ANCHORS: "анкоры",
    Field.STATUS: "статус",
}
# Порядок полей на экранах: как в панели размещения.
ORDER = (Field.STATUS, Field.ORDER, Field.PAID, Field.ARTICLE, Field.ANNOUNCE, Field.ANCHORS)


class Verdict(StrEnum):
    OK = "ok"
    TEXT = "text"
    URL = "url"
    EXTRA = "extra"
    MISSING = "missing"


VERDICT_TEXT = {
    Verdict.OK: "✓",
    Verdict.TEXT: "текст другой",
    Verdict.URL: "адрес другой",
    Verdict.EXTRA: "лишняя в системе",
    Verdict.MISSING: "нет в системе",
}


@dataclass(frozen=True)
class LinkCheck:
    system: Link | None
    collaborator: Link | None
    verdict: Verdict


@dataclass(frozen=True)
class Fill:
    field: Field
    value: str | bool | Money


@dataclass(frozen=True)
class Differ:
    field: Field
    # Пара для «Оставить как в системе» — значения в виде JSON.
    system: Any
    collaborator: Any
    kept: bool = False
    # «Взять из Collaborator» на сервере; статус — только в панели человеком,
    # «Заплачено» из счёта — только в счёте.
    takeable: bool = True
    # Статус, который выбрать в панели: «Размещено» или «Отказ».
    target_status: str | None = None

    @property
    def pair(self) -> list[Any]:
        return [self.system, self.collaborator]


@dataclass(frozen=True)
class Row:
    matched: Matched
    ready: bool = False
    fills: tuple[Fill, ...] = ()
    differs: tuple[Differ, ...] = ()
    links: tuple[LinkCheck, ...] = ()

    @property
    def deal(self) -> Deal:
        return self.matched.deal

    @property
    def placement(self) -> Facts:
        assert self.matched.placement is not None
        return self.matched.placement

    @property
    def open_differs(self) -> list[Differ]:
        return [d for d in self.differs if not d.kept]

    @property
    def kept_differs(self) -> list[Differ]:
        return [d for d in self.differs if d.kept]

    def fill_of(self, wanted: Field) -> Fill | None:
        return next((f for f in self.fills if f.field == wanted), None)

    def differ_of(self, wanted: Field) -> Differ | None:
        return next((d for d in self.differs if d.field == wanted), None)


def compare_links(system: Sequence[Link], collaborator: Sequence[Link]) -> list[LinkCheck]:
    """Ссылки размещения против анкоров сделки — по каждой ссылке.

    Пары ищутся по порядку: тот же текст и адрес — ✓; тот же адрес — «текст
    другой»; тот же текст — «адрес другой»; оставшиеся — «лишняя в системе» и
    «нет в системе». Текст — без регистра и лишних пробелов («convertio.co» и
    «https://convertio.co» — разные тексты), адрес — с точностью до `/` в конце.
    Порядок — как на Collaborator, лишние — в конце.
    """
    free = list(range(len(system)))
    paired: dict[int, tuple[int, Verdict]] = {}

    def take(index: int, same: Any, verdict: Verdict) -> None:
        for position in free:
            if same(system[position], collaborator[index]):
                paired[index] = (position, verdict)
                free.remove(position)
                return

    rules = (
        (Verdict.OK, lambda s, c: _text(s) == _text(c) and same_url(s.url, c.url)),
        (Verdict.TEXT, lambda s, c: same_url(s.url, c.url)),
        (Verdict.URL, lambda s, c: _text(s) == _text(c)),
    )
    for verdict, same in rules:
        for index in range(len(collaborator)):
            if index not in paired:
                take(index, same, verdict)
    checks = []
    for index, link in enumerate(collaborator):
        if index in paired:
            position, verdict = paired[index]
            checks.append(LinkCheck(system[position], link, verdict))
        else:
            checks.append(LinkCheck(None, link, Verdict.MISSING))
    checks += [LinkCheck(system[position], None, Verdict.EXTRA) for position in free]
    return checks


def reconcile(matched: Matched) -> Row:
    """Что заполнить и что расходится у сделки с размещением."""
    deal, facts = matched.deal, matched.placement
    if facts is None:
        return Row(matched)
    if deal.cancelled:
        if facts.status not in LIVE:
            return Row(matched)
        status = _differ(deal, Field.STATUS, facts.status, deal.status, status=True)
        return Row(matched, differs=(status,))

    fills: list[Fill] = []
    differs: list[Differ] = []
    number = str(deal.id)
    if not facts.order_id.strip():
        fills.append(Fill(Field.ORDER, number))
    elif facts.order_id.strip() != number:
        differs.append(_differ(deal, Field.ORDER, facts.order_id.strip(), number))
    if deal.article_url:
        if not facts.article_url:
            fills.append(Fill(Field.ARTICLE, deal.article_url))
        elif normalize_url(facts.article_url) != normalize_url(deal.article_url):
            differs.append(_differ(deal, Field.ARTICLE, facts.article_url, deal.article_url))
    if deal.article_ready and deal.total is not None and facts.status == WorkStatus.PLACED:
        if facts.paid is None and not facts.from_invoice:
            fills.append(Fill(Field.PAID, deal.total))
        elif facts.paid is not None and facts.paid != deal.total:
            differs.append(
                _differ(
                    deal,
                    Field.PAID,
                    [facts.paid.cents, facts.paid.currency],
                    [deal.total.cents, deal.total.currency],
                    takeable=not facts.from_invoice,
                )
            )
    if deal.article_ready and deal.announce is not None and deal.announce.cents > 0:
        if facts.announce is None:
            fills.append(Fill(Field.ANNOUNCE, True))
        elif facts.announce is False:
            differs.append(_differ(deal, Field.ANNOUNCE, False, True))
    links = compare_links(facts.links, deal.anchors) if deal.anchors else []
    if any(check.verdict != Verdict.OK for check in links):
        differs.append(
            _differ(
                deal,
                Field.ANCHORS,
                [[link.anchor, link.url] for link in facts.links],
                [[link.anchor, link.url] for link in deal.anchors],
            )
        )
    if deal.status == POSTED and facts.status != WorkStatus.PLACED:
        differs.append(_differ(deal, Field.STATUS, facts.status, deal.status, status=True))
    ready = deal.status == PENDING and bool(deal.article_url) and facts.status in CLOSABLE
    return Row(matched, ready, tuple(fills), tuple(differs), tuple(links))


def closing_fills(row: Row) -> list[Fill]:
    """Что поставит «Заполнить из Collaborator» в панели при закрытии — ещё и «Заплачено».

    В списке «Можно заполнить» «Заплачено» только у «Размещено»; закрывая заявку,
    человек сразу жмёт «Размещено» — тогда «Заплачено» нужно вместе с номером.
    """
    fills = list(row.fills)
    deal, facts = row.deal, row.placement
    closing = deal.article_ready and deal.total is not None and facts.paid is None
    if closing and not facts.from_invoice and row.fill_of(Field.PAID) is None:
        assert deal.total is not None
        fills.append(Fill(Field.PAID, deal.total))
    return sorted(fills, key=lambda fill: ORDER.index(fill.field))


def _differ(
    deal: Deal,
    what: Field,
    system: Any,
    collaborator: Any,
    *,
    takeable: bool = True,
    status: bool = False,
) -> Differ:
    pair = [system, collaborator]
    stored = deal.kept.get(what)
    kept = isinstance(stored, Mapping) and stored.get("pair") == pair
    target = None
    if status:
        target = WorkStatus.REJECTED if deal.cancelled else WorkStatus.PLACED
    return Differ(
        what,
        system,
        collaborator,
        kept=kept,
        takeable=takeable and not status,
        target_status=target,
    )


def _text(link: Link) -> str:
    return normalize(link.anchor)


@dataclass
class Groups:
    """Сделки продукта по группам экрана сверки."""

    ready: list[Row] = field(default_factory=list)
    fill: list[Row] = field(default_factory=list)
    differ: list[Row] = field(default_factory=list)
    missing: list[Matched] = field(default_factory=list)
    kept: list[Row] = field(default_factory=list)

    @classmethod
    def of(cls, matched: Sequence[Matched]) -> "Groups":
        groups = cls()
        for item in matched:
            if item.placement is None:
                if not item.deal.cancelled:
                    groups.missing.append(item)
                continue
            row = reconcile(item)
            if row.ready:
                groups.ready.append(row)
            if row.fills:
                groups.fill.append(row)
            if row.open_differs:
                groups.differ.append(row)
            if row.kept_differs:
                groups.kept.append(row)
        return groups

    def fill_counts(self) -> dict[Field, int]:
        return _counts(f.field for row in self.fill for f in row.fills)

    def differ_counts(self) -> dict[Field, int]:
        return _counts(d.field for row in self.differ for d in row.open_differs)

    def placements(self, group: str) -> set[int]:
        """Размещения группы — для фильтра «Collaborator» в «Размещениях»."""
        rows: list[Row] = getattr(self, group)
        return {row.placement.id for row in rows}


def _counts(fields: Any) -> dict[Field, int]:
    counts: dict[Field, int] = {}
    for item in fields:
        counts[item] = counts.get(item, 0) + 1
    return {name: counts[name] for name in ORDER if name in counts}


def summary(total: int, groups: Groups) -> str:
    """«186 сделок: 2 готовы к закрытию, 85 можно заполнить, 19 расходится, 2 нет в системе»."""
    parts = [
        f"{len(groups.ready)} {_plural(len(groups.ready), 'готова', 'готовы', 'готовы')}"
        " к закрытию",
        f"{len(groups.fill)} можно заполнить",
        f"{len(groups.differ)} расходится",
        f"{len(groups.missing)} нет в системе",
    ]
    return f"{total} {_plural(total, 'сделка', 'сделки', 'сделок')}: " + ", ".join(parts)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def json_value(value: str | bool | Money) -> Any:
    """Значение заполнения — для ответа JSON и страницы."""
    if isinstance(value, Money):
        return {"cents": value.cents, "currency": value.currency}
    return value


def kept_entry(differ: Differ, user_id: int | None, at: str) -> dict[str, Any]:
    return {"pair": differ.pair, "by": user_id, "at": at}


def deal_kept(kept: Mapping[str, Any], what: Field) -> Mapping[str, Any] | None:
    stored = kept.get(what)
    return stored if isinstance(stored, Mapping) else None
