"""Статусы площадки: просмотрено, заявка отправлена, отбрасываю, отказала площадка (ADR-047).

- Тип `site_status` создаётся заново, значения — в порядке окна статуса:
  «На аудите» со второго места в конец значением, добавленным в старый тип,
  не переставить, а по порядку значений сортирует колонка «статус».
- `rejected` убирается. В таблице линкбилдинга «Отклонена» значило наш отказ —
  на рабочей базе у всех 62 таких площадок в причине «…отбрасываем», — поэтому
  все `rejected` становятся `discarded`, причины не трогаются. Отказ площадки —
  новое значение `declined`.
- Колонку `product_sites.status` читают два представления: сменить тип колонки,
  пока они есть, Postgres не даёт — они пересоздаются тем же текстом.
- Догоняющий проход: площадка у продукта с заявкой в работе — «Заявка
  отправлена», с публикацией — «Размещались». Как правило `apps.sites.statuses`,
  но только по лесенке: про прежний отказ неизвестно, был он до размещения или
  после, — его решает человек.

Всё в одной транзакции: значения нового типа, созданного в ней же, можно
использовать сразу (ограничение есть только у `ALTER TYPE … ADD VALUE`).
Откат: просмотрено — новая, заявка отправлена — одобрена, оба отказа —
отклонена.
"""

from django.db import migrations

import config.db

SITE_FUNNEL = """
CREATE VIEW v_site_funnel AS
SELECT ps.product_id, ps.status, ps.imported_undecided, count(*) AS sites
FROM product_sites ps
JOIN sites s ON s.id = ps.site_id
WHERE NOT s.is_deleted
GROUP BY ps.product_id, ps.status, ps.imported_undecided;
"""

PRODUCT_SITE_LATEST = """
CREATE VIEW v_product_site_latest AS
SELECT ps.id, ps.product_id, ps.site_id, ps.status, ps.reject_reason, ps.imported_undecided,
       l.domain, l.language, l.topics, l.declared_topics,
       l.links_allowed, l.link_type, l.marks_as_ad,
       l.dr, l.organic_traffic, l.total_keywords, l.top_geo, l.top_geo_traffic, l.top_geo_at,
       l.metrics_at,
       l.metrics_trusted, l.metrics_seller,
       l.price_id, l.price_seller_id, l.price_seller, l.price_type,
       l.placement_cents, l.announce_cents, l.writing_cents, l.price_currency, l.prices_at,
       l.placement_eur_cents, l.writing_eur_cents, l.reference_total_cents,
       CASE WHEN l.price_type = 'link_insertion' THEN NULL
            ELSE l.writing_eur_cents IS NULL OR l.writing_eur_cents > w.writing_cents END AS we_write,
       l.reference_total_cents
         + CASE WHEN w.writing_cents IS NULL THEN NULL
                WHEN l.writing_eur_cents <= w.writing_cents THEN l.writing_eur_cents
                ELSE 0 END AS expected_spend_cents,
       l.new_price_id, l.new_price_cents, l.new_price_currency, l.new_price_pending,
       l.cheaper_id, l.cheaper_seller, l.cheaper_cents, l.cheaper_currency,
       l.cheaper_eur_cents, l.cheaper_pending, l.offers_pending,
       l.gray_ratio,
       l.notes_count, l.last_note, l.last_note_at,
       a.verdict AS last_verdict, a.score AS last_score, a.created_at AS audited_at,
       pp.published AS placements_published,
       op.names AS other_products_placed
FROM product_sites ps
JOIN v_site_latest l ON l.id = ps.site_id
LEFT JOIN LATERAL (SELECT (x.value->>'writing_eur')::integer * 100 AS writing_cents
                   FROM domain_settings x
                   WHERE x.key = 'PRICE_REFERENCE'
                     AND (x.product_id = ps.product_id OR x.product_id IS NULL)
                   ORDER BY x.product_id NULLS LAST LIMIT 1) w ON true
LEFT JOIN LATERAL (SELECT * FROM site_audits x
                   WHERE x.site_id = ps.site_id AND x.product_id = ps.product_id
                   ORDER BY created_at DESC LIMIT 1) a ON true
LEFT JOIN LATERAL (SELECT count(*) AS published FROM placements x
                   WHERE x.site_id = ps.site_id AND x.product_id = ps.product_id
                     AND x.status = 'published') pp ON true
LEFT JOIN LATERAL (SELECT array_agg(DISTINCT pr.name ORDER BY pr.name) AS names
                   FROM placements x JOIN products pr ON pr.id = x.product_id
                   WHERE x.site_id = ps.site_id AND x.product_id <> ps.product_id
                     AND x.status = 'published') op ON true;
"""

DROP_VIEWS = "DROP VIEW v_product_site_latest; DROP VIEW v_site_funnel;"

NEW_TYPE = """
ALTER TYPE site_status RENAME TO site_status_old;
CREATE TYPE site_status AS ENUM
    ('new','viewed','approved','ordered','placed','discarded','declined','blacklisted','auditing');
ALTER TABLE product_sites ALTER COLUMN status DROP DEFAULT;
ALTER TABLE product_sites ALTER COLUMN status TYPE site_status
    USING (CASE status::text WHEN 'rejected' THEN 'discarded' ELSE status::text END)::site_status;
ALTER TABLE product_sites ALTER COLUMN status SET DEFAULT 'new';
DROP TYPE site_status_old;
"""

OLD_TYPE = """
ALTER TYPE site_status RENAME TO site_status_new;
CREATE TYPE site_status AS ENUM
    ('new','auditing','approved','rejected','placed','blacklisted');
ALTER TABLE product_sites ALTER COLUMN status DROP DEFAULT;
ALTER TABLE product_sites ALTER COLUMN status TYPE site_status
    USING (CASE status::text
             WHEN 'viewed' THEN 'new'
             WHEN 'ordered' THEN 'approved'
             WHEN 'discarded' THEN 'rejected'
             WHEN 'declined' THEN 'rejected'
             ELSE status::text END)::site_status;
ALTER TABLE product_sites ALTER COLUMN status SET DEFAULT 'new';
DROP TYPE site_status_new;
"""

# Сначала публикации, потом заявки: площадка с тем и другим — «Размещались».
CATCH_UP = """
UPDATE product_sites ps
SET status = 'placed', imported_undecided = false, updated_at = now()
WHERE ps.status IN ('new','viewed','approved','ordered')
  AND EXISTS (SELECT 1 FROM placements x
              WHERE x.site_id = ps.site_id AND x.product_id = ps.product_id
                AND x.status = 'published');
UPDATE product_sites ps
SET status = 'ordered', imported_undecided = false, updated_at = now()
WHERE ps.status IN ('new','viewed','approved')
  AND EXISTS (SELECT 1 FROM placements x
              WHERE x.site_id = ps.site_id AND x.product_id = ps.product_id
                AND x.status IN ('ordered','writing','review'));
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0014_country_metrics"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_VIEWS + NEW_TYPE + SITE_FUNNEL + PRODUCT_SITE_LATEST,
            reverse_sql=DROP_VIEWS + OLD_TYPE + SITE_FUNNEL + PRODUCT_SITE_LATEST,
        ),
        migrations.RunSQL(sql=CATCH_UP, reverse_sql=migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="productsite",
            name="status",
            field=config.db.PgEnumField(
                choices=[
                    ("new", "Новая"),
                    ("viewed", "Просмотрено"),
                    ("approved", "Одобрена"),
                    ("ordered", "Заявка отправлена"),
                    ("placed", "Размещались"),
                    ("discarded", "Отбрасываю"),
                    ("declined", "Отказала площадка"),
                    ("blacklisted", "Чёрный список"),
                    ("auditing", "На аудите"),
                ],
                db_default="new",
                default="new",
                enum_type="site_status",
                verbose_name="статус",
            ),
        ),
    ]
