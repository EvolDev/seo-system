"""Заранее посчитанная «площадка на сегодня» (E1-11, ADR-065).

`v_site_latest` раньше считал метрики, топ-регион и пометки разбора цен
на каждый показ списка — по всем 45 000 площадкам, даже когда на экране
сотня. Замер 07.10.2026: страница «Площадок» 2,6 с, из них 1,9 с SQL.
Теперь дорогие части лежат в таблице `site_latest`, а представление их
читает.

Правила подсчёта живут **здесь и только здесь** — это тот же SQL, что
стоял в представлении. Дешёвое (серость, заметки, оценки, «есть
неразобранные») представление по-прежнему считает вживую.

Пересчёт адресный: правка площадки пересчитывает её одну, загрузка —
свои площадки пачкой, смена курсов валют — всё (в евро пересчитываются
пометки «дешевле»).
"""

from collections.abc import Iterable
from typing import Any

from django.db import connection

#: Колонки копии в порядке подсчёта — один список на вставку и на сверку.
COLUMNS = (
    "dr",
    "organic_traffic",
    "total_keywords",
    "metrics_at",
    "metrics_trusted",
    "metrics_seller_id",
    "top_geo",
    "top_geo_traffic",
    "top_geo_at",
    "new_price_id",
    "new_price_cents",
    "new_price_currency",
    "new_price_pending",
    "cheaper_id",
    "cheaper_seller_id",
    "cheaper_cents",
    "cheaper_currency",
    "cheaper_eur_cents",
    "cheaper_pending",
)

#: Сами правила подсчёта — тот же SQL, что стоял в представлении.
#: `%(where)s` — отбор площадок. Вставку и сверку строим вокруг него,
#: чтобы правила были записаны один раз.
_RULES = """
SELECT s.id, m.dr, m.organic_traffic, m.total_keywords, m.checked_at, m.trusted,
       m.seller_id, tg.top_geo, tg.top_geo_traffic, tg.checked_at,
       np.id, np.placement_cents, np.currency, np.reviewed_at IS NULL,
       ch.id, ch.seller_id, ch.placement_cents, ch.currency,
       ch.placement_eur_cents, ch.reviewed_at IS NULL, now()
FROM sites s
LEFT JOIN LATERAL (SELECT x.dr, x.organic_traffic, x.total_keywords, x.checked_at,
                          x.seller_id, x.seller_id IS NULL OR xs.metrics_trusted AS trusted
                   FROM site_metrics x LEFT JOIN sellers xs ON xs.id = x.seller_id
                   WHERE x.site_id = s.id
                   ORDER BY x.seller_id IS NULL OR xs.metrics_trusted DESC, x.checked_at DESC
                   LIMIT 1) m ON true
LEFT JOIN LATERAL (SELECT x.top_geo, x.top_geo_traffic, x.checked_at
                   FROM site_metrics x LEFT JOIN sellers xs ON xs.id = x.seller_id
                   WHERE x.site_id = s.id AND x.top_geo IS NOT NULL
                   ORDER BY x.seller_id IS NULL OR xs.metrics_trusted DESC, x.checked_at DESC
                   LIMIT 1) tg ON true
LEFT JOIN site_prices pr ON pr.id = s.price_id
LEFT JOIN LATERAL (SELECT o.id, o.placement_cents, o.currency, o.reviewed_at
                   FROM v_site_offers o
                   WHERE o.site_id = s.id AND o.seller_id = pr.seller_id
                     AND o.placement_type = pr.placement_type AND o.id <> pr.id) np ON true
LEFT JOIN LATERAL (SELECT o.id, o.seller_id, o.placement_cents, o.currency,
                          o.placement_eur_cents, o.reviewed_at
                   FROM v_site_offers o
                   WHERE o.site_id = s.id AND o.placement_type = pr.placement_type
                     AND o.seller_id <> pr.seller_id
                     AND o.placement_eur_cents < round(pr.placement_cents / eur_rate(pr.currency))
                   ORDER BY o.placement_eur_cents, o.id LIMIT 1) ch ON true
WHERE %(where)s
"""


_UPSERT = """
INSERT INTO site_latest (site_id, {columns}, computed_at)
{rules}
ON CONFLICT (site_id) DO UPDATE SET
{sets},
    computed_at = now()
"""


def _sql(where: str) -> str:
    """Вставка с правилами внутри: колонки и присвоения — из одного списка."""
    sets = ",\n".join(f"    {name} = EXCLUDED.{name}" for name in COLUMNS)
    return _UPSERT.format(columns=", ".join(COLUMNS), rules=_RULES % {"where": where}, sets=sets)


def refresh(site_ids: Iterable[int]) -> int:
    """Пересчитать названные площадки. Пустой список — ничего не делаем."""
    ids = list(dict.fromkeys(site_ids))
    if not ids:
        return 0
    with connection.cursor() as cursor:
        cursor.execute(_sql("s.id = ANY(%s)"), [ids])
        return int(cursor.rowcount)


def refresh_all() -> int:
    """Пересчитать всё: после смены курсов валют и командой восстановления.

    Удалённые площадки тоже считаем: строка в `sites` остаётся (ADR-060),
    и площадку могут вернуть.
    """
    with connection.cursor() as cursor:
        cursor.execute(_sql("true"))
        return int(cursor.rowcount)


def stale(limit: int = 50) -> list[dict[str, Any]]:
    """Площадки, где копия разошлась с живым подсчётом, — для команды сверки.

    Считает теми же правилами (`_RULES`) и сравнивает построчно: сверяются
    сами правила, а не их пересказ. `IS NOT DISTINCT FROM` — чтобы два
    пустых значения считались равными.
    """
    same = " AND ".join(f"a.{name} IS NOT DISTINCT FROM b.{name}" for name in COLUMNS)
    live = _RULES % {"where": "true"}
    with connection.cursor() as cursor:
        cursor.execute(
            f"""WITH b (site_id, {", ".join(COLUMNS)}, computed_at) AS ({live})
                SELECT b.site_id, s.domain
                FROM b
                LEFT JOIN site_latest a ON a.site_id = b.site_id
                JOIN sites s ON s.id = b.site_id
                WHERE a.site_id IS NULL OR NOT ({same})
                ORDER BY b.site_id
                LIMIT %s""",
            [limit],
        )
        return [{"site_id": row[0], "domain": row[1]} for row in cursor.fetchall()]
