"""Анкоры продукта: доли «цель / факт» и рекомендации (E3-05, ADR-059).

Сводка — сырым SQL по `v_keyword_coverage` (ADR-003): сколько ссылок под
каждый анкор размещено и ждёт. Доля типа страниц — ссылки на анкоры этого типа
среди всех ссылок на анкоры с типом, как на листе «Распределение по типам
страниц»: размещённые, ждущие и суммарно.

Вид ссылок внутри типа (прямой / разбавленный / безанкор, ADR-069) считается
по самим ссылкам: одна ссылка засчитывается одному ключу и одному виду. У
старых ссылок без записанного вида берётся вид ключа — их число показывается
отдельно, это запасной вариант, а не проверка опубликованного текста.

Рекомендации — чистая функция от сводки (`recommend`), без LLM: по шагу за раз
берётся тип страниц с самым большим недобором до цели, внутри типа — анкор,
у которого ссылок меньше всего, и он засчитывается, как будто уже поставлен.
Так список ведёт к целевым долям, а не повторяет один тип десять раз. Внутри
типа выбор двоичный: ключевая ссылка (прямая + разбавленная) или безанкор —
разбавит ли человек ключ, рекомендация не решает.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import connection

from apps.keywords.models import NAKED_TYPES, PageTypeShare

# Безанкорные типы строкой — как их отдаёт представление.
NAKED = frozenset(str(kind) for kind in NAKED_TYPES)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Anchor:
    """Анкор продукта со счётчиками ссылок — строка `v_keyword_coverage`."""

    id: int
    keyword: str
    target_url: str
    page_type: str | None
    anchor_type: str | None
    share: Decimal | None
    placed: int
    waiting: int
    position: int | None
    volume: int | None
    global_volume: int | None
    tool: str | None

    @property
    def links(self) -> int:
        return self.placed + self.waiting

    @property
    def is_naked(self) -> bool:
        return self.anchor_type in NAKED


@dataclass(frozen=True)
class Share:
    """Целевые доли типа страниц, %: пусто — не задано."""

    page_type: str
    target: Decimal | None
    exact: Decimal | None = None
    diluted: Decimal | None = None
    naked: Decimal | None = None


@dataclass(frozen=True)
class KindStat:
    """Ссылки типа страниц по виду: размещённые и ждущие вместе."""

    exact: int = 0
    diluted: int = 0
    naked: int = 0
    unknown: int = 0  # ни у ссылки, ни у ключа вид не записан
    by_key: int = 0  # из них вид взят у ключа: у ссылки не записан

    @property
    def total(self) -> int:
        return self.exact + self.diluted + self.naked + self.unknown

    def pct(self, name: str) -> Decimal | None:
        return percent(getattr(self, name), self.total)


@dataclass(frozen=True)
class TypeStat:
    """Тип страниц: цель и факт — размещённые, ждущие, суммарно (% и штуки)."""

    page_type: str
    target: Decimal | None
    placed: int
    waiting: int
    placed_pct: Decimal | None
    waiting_pct: Decimal | None
    total_pct: Decimal | None
    share: Share | None
    kinds: KindStat = KindStat()

    @property
    def total(self) -> int:
        return self.placed + self.waiting

    @property
    def gap(self) -> Decimal | None:
        """Цель минус факт, п.п.: больше нуля — недобор."""
        if self.target is None or self.total_pct is None:
            return None
        return self.target - self.total_pct


@dataclass(frozen=True)
class Overview:
    """Анкоры продукта целиком: строки, типы страниц, итоги."""

    anchors: tuple[Anchor, ...]
    types: tuple[TypeStat, ...]
    shares: tuple[Share, ...]
    placed: int
    waiting: int
    # Ссылки без анкора из списка: старые «click here», адрес вместо ключа.
    loose_placed: int
    loose_waiting: int

    @property
    def total(self) -> int:
        return self.placed + self.waiting


@dataclass(frozen=True)
class Recommendation:
    anchor: Anchor
    reason: str


def overview(product_id: int) -> Overview:
    """Сводка анкоров продукта: два запроса и доли из `page_type_shares`."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, keyword, target_url, page_type, anchor_type, share,
                   links_placed, links_waiting, last_position, volume, global_volume, tool
            FROM v_keyword_coverage
            WHERE product_id = %s AND is_active
            ORDER BY id
            """,
            [product_id],
        )
        anchors = tuple(Anchor(*row) for row in cursor.fetchall())
        # Непривязанные ссылки учитываем отдельно; выключенные анкоры исключены.
        cursor.execute(
            """
            SELECT COUNT(*) FILTER (WHERE p.status = 'placed'),
                   COUNT(*) FILTER (WHERE p.status IN ('in_work','ordered','writing'))
            FROM placement_links pl
            JOIN placements p ON p.id = pl.placement_id
            LEFT JOIN keywords k ON k.id = pl.keyword_id
            WHERE p.product_id = %s AND pl.keyword_id IS NULL
            """,
            [product_id],
        )
        loose_placed, loose_waiting = cursor.fetchone() or (0, 0)
        cursor.execute(
            """
            SELECT k.page_type,
                   CASE WHEN coalesce(pl.anchor_type, k.anchor_type)
                             IN ('branded', 'url', 'generic') THEN 'naked'
                        ELSE coalesce(pl.anchor_type, k.anchor_type)::text END,
                   pl.anchor_type IS NULL,
                   COUNT(*)
            FROM placement_links pl
            JOIN placements p ON p.id = pl.placement_id
            JOIN keywords k ON k.id = pl.keyword_id
            WHERE p.product_id = %s AND k.is_active AND k.page_type IS NOT NULL
              AND p.status IN ('placed', 'in_work', 'ordered', 'writing')
            GROUP BY 1, 2, 3
            """,
            [product_id],
        )
        kinds = kind_stats(cursor.fetchall())
    shares = tuple(
        Share(row.page_type, row.target_pct, row.exact_pct, row.diluted_pct, row.naked_pct)
        for row in PageTypeShare.objects.filter(product_id=product_id).order_by(
            "position", "page_type"
        )
    )
    return Overview(
        anchors=anchors,
        types=type_stats(anchors, shares, kinds),
        shares=shares,
        placed=sum(a.placed for a in anchors) + loose_placed,
        waiting=sum(a.waiting for a in anchors) + loose_waiting,
        loose_placed=loose_placed,
        loose_waiting=loose_waiting,
    )


def kind_stats(rows: Iterable[tuple[str, str | None, bool, int]]) -> dict[str, KindStat]:
    """Строки «тип страниц, вид, вид взят у ключа, ссылок» → вид по типам страниц."""
    counts: dict[str, dict[str, int]] = {}
    for page_type, kind, by_key, links in rows:
        item = counts.setdefault(page_type, {})
        name = kind if kind in ("exact", "diluted", "naked") else "unknown"
        item[name] = item.get(name, 0) + links
        if by_key and kind is not None:
            item["by_key"] = item.get("by_key", 0) + links
    return {page_type: KindStat(**item) for page_type, item in counts.items()}


def type_stats(
    anchors: Sequence[Anchor],
    shares: Sequence[Share],
    kinds: Mapping[str, KindStat] | None = None,
) -> tuple[TypeStat, ...]:
    """Типы страниц по порядку долей, затем типы анкоров без доли."""
    by_share = {share.page_type: share for share in shares}
    names = [share.page_type for share in shares]
    names += sorted({a.page_type for a in anchors if a.page_type and a.page_type not in by_share})
    typed = [a for a in anchors if a.page_type]
    all_placed = sum(a.placed for a in typed)
    all_waiting = sum(a.waiting for a in typed)
    stats = []
    for name in names:
        of_type = [a for a in typed if a.page_type == name]
        placed = sum(a.placed for a in of_type)
        waiting = sum(a.waiting for a in of_type)
        share = by_share.get(name)
        stats.append(
            TypeStat(
                page_type=name,
                target=share.target if share else None,
                placed=placed,
                waiting=waiting,
                placed_pct=percent(placed, all_placed),
                waiting_pct=percent(waiting, all_waiting),
                total_pct=percent(placed + waiting, all_placed + all_waiting),
                share=share,
                kinds=(kinds or {}).get(name, KindStat()),
            )
        )
    return tuple(stats)


def percent(part: int, whole: int) -> Decimal | None:
    if whole == 0:
        return None
    return (Decimal(part) * HUNDRED / Decimal(whole)).quantize(Decimal("0.1"))


@dataclass
class _Type:
    """Тип страниц по ходу подбора: сколько ссылок уже есть и засчитано."""

    share: Share
    links: int
    naked_links: int
    exact: list[Anchor] = field(default_factory=list)
    naked: list[Anchor] = field(default_factory=list)


def recommend(
    anchors: Iterable[Anchor],
    shares: Sequence[Share],
    *,
    limit: int,
    skip_top: int,
    exclude: Iterable[int] = (),
) -> list[Recommendation]:
    """Анкоры по порядку: что ставить, чтобы доли пришли к цели.

    `exclude` — анкоры, которые уже стоят в этом размещении. Ключ на позиции
    от 1 до `skip_top` не предлагается: он уже в топе. Безанкорному позиция не
    нужна. Без долей у продукта — просто анкоры, у которых ссылок меньше всего.
    """
    excluded = set(exclude)
    pool = [
        a
        for a in anchors
        if a.id not in excluded
        and (a.is_naked or a.position is None or not 1 <= a.position <= skip_top)
    ]
    extra: dict[int, int] = {}
    types = _types(pool, anchors, shares)
    if not types:
        ordered = sorted(pool, key=lambda a: (a.links, -(a.volume or 0), a.id))
        return [Recommendation(a, _few_links(a)) for a in ordered[:limit]]
    total = sum(t.links for t in types.values())
    reasons = {name: _reason(t, total) for name, t in types.items()}
    result: list[Recommendation] = []
    while len(result) < limit:
        chosen = _neediest(types, total)
        if chosen is None:
            break
        kind = _kind(chosen)
        candidates = chosen.naked if kind == "naked" else chosen.exact
        if kind == "naked":
            anchor = max(
                candidates, key=lambda a: (_naked_need(a, chosen, extra), a.share or 0, -a.id)
            )
            chosen.naked_links += 1
        else:
            anchor = min(
                candidates, key=lambda a: (a.links + extra.get(a.id, 0), -(a.volume or 0), a.id)
            )
        candidates.remove(anchor)
        extra[anchor.id] = extra.get(anchor.id, 0) + 1
        chosen.links += 1
        total += 1
        result.append(Recommendation(anchor, reasons[chosen.share.page_type]))
    return result


def _types(
    pool: Sequence[Anchor], anchors: Iterable[Anchor], shares: Sequence[Share]
) -> dict[str, _Type]:
    """Типы с целевой долей: ссылки по всем анкорам типа, кандидаты — из `pool`."""
    everything = list(anchors)
    types: dict[str, _Type] = {}
    for share in shares:
        if share.target is None or share.target <= 0:
            continue
        of_type = [a for a in everything if a.page_type == share.page_type]
        types[share.page_type] = _Type(
            share=share,
            links=sum(a.links for a in of_type),
            naked_links=sum(a.links for a in of_type if a.is_naked),
            exact=[a for a in pool if a.page_type == share.page_type and not a.is_naked],
            naked=[a for a in pool if a.page_type == share.page_type and a.is_naked],
        )
    return types


def _neediest(types: dict[str, _Type], total: int) -> _Type | None:
    """Тип с самым большим недобором: сколько ссылок не хватает до цели после следующей."""
    best: _Type | None = None
    best_key: tuple[Decimal, Decimal] | None = None
    for item in types.values():
        if not item.exact and not item.naked:
            continue
        target = item.share.target or Decimal(0)
        need = target * Decimal(total + 1) / HUNDRED - Decimal(item.links)
        key = (need, target)
        if best_key is None or key > best_key:
            best, best_key = item, key
    return best


def _kind(item: _Type) -> str:
    """Ключевая ссылка или безанкор внутри типа: чего не хватает до деления типа.

    Ключевая — прямая и разбавленная вместе: рекомендация предлагает ключ, а
    разбавит ли его человек, решает он (ADR-069). Ни одна из двух долей не
    задана — ключевым достаётся всё, кроме безанкорки.
    """
    if not item.naked:
        return "exact"
    if not item.exact:
        return "naked"
    naked = item.share.naked or Decimal(0)
    if item.share.exact is None and item.share.diluted is None:
        exact = HUNDRED - naked
    else:
        exact = (item.share.exact or Decimal(0)) + (item.share.diluted or Decimal(0))
    after = Decimal(item.links + 1)
    exact_links = item.links - item.naked_links
    naked_need = naked * after / HUNDRED - Decimal(item.naked_links)
    exact_need = exact * after / HUNDRED - Decimal(exact_links)
    return "naked" if naked_need > exact_need else "exact"


def _naked_need(anchor: Anchor, item: _Type, extra: dict[int, int]) -> Decimal:
    """Недобор безанкорного анкора до его доли внутри безанкорки типа."""
    share = anchor.share or Decimal(0)
    return share * Decimal(item.naked_links + 1) / HUNDRED - Decimal(
        anchor.links + extra.get(anchor.id, 0)
    )


def _reason(item: _Type, total: int) -> str:
    target = pct_text(item.share.target)
    now = pct_text(percent(item.links, total))
    return f"{item.share.page_type}: цель {target}, сейчас {now}"


def _few_links(anchor: Anchor) -> str:
    return "ссылок нет" if anchor.links == 0 else f"ссылок: {anchor.links}"


def pct_text(value: Decimal | None) -> str:
    """15.00 → «15%», 15.80 → «15,8%»; пусто — «—»."""
    if value is None:
        return "—"
    text = f"{value.quantize(Decimal('0.1')):f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + "%"
