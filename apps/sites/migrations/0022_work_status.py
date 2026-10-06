# Общий статус работы: площадка и размещение говорят одними словами (ADR-062).

from django.db import migrations, models

import config.db

# Представления смотрят на колонки статуса, поэтому на время замены типа их
# нет; текст — как в schema.sql, только значения статусов новые.
DROP_VIEWS = "DROP VIEW v_keyword_coverage, v_link_health, v_monthly_spend, v_product_site_latest, v_site_funnel;"

VIEWS = """
CREATE VIEW v_keyword_coverage AS
SELECT
    k.id,
    k.product_id,
    k.keyword,
    k.tool,
    k.volume,
    k.target_url,
    (SELECT position FROM keyword_positions kp
      WHERE kp.keyword_id = k.id AND kp.country = 'US'
      ORDER BY checked_at DESC LIMIT 1) AS last_position,
    COUNT(pl.id) FILTER (WHERE p.status = 'placed') AS links_placed,
    COUNT(pl.id) FILTER (WHERE p.status IN ('in_work','ordered','writing')) AS links_waiting,
    k.global_volume,
    k.page_type,
    k.anchor_type::text AS anchor_type,
    k.share
FROM keywords k
LEFT JOIN placement_links pl ON pl.keyword_id = k.id
LEFT JOIN placements p ON p.id = pl.placement_id
WHERE k.is_active
GROUP BY k.id;
CREATE VIEW v_link_health AS
SELECT
    s.domain,
    p.id AS placement_id,
    p.published_at,
    pl.anchor,
    pl.is_alive,
    pl.last_checked_at,
    now() - p.published_at AS age
FROM placement_links pl
JOIN placements p ON p.id = pl.placement_id
JOIN sites s ON s.id = p.site_id
WHERE p.status = 'placed';
CREATE VIEW v_monthly_spend AS
SELECT date_trunc('month', created_at)::date AS month,
       provider AS item, currency, sum(cost_cents) AS cost_cents
FROM api_usage
GROUP BY 1, 2, 3
UNION ALL
SELECT date_trunc('month', created_at)::date, 'llm:' || model, currency, sum(cost_cents)
FROM llm_calls
GROUP BY 1, 2, 3
UNION ALL
SELECT date_trunc('month', published_at)::date, 'placements',
       coalesce(currency, 'EUR'), sum(price_paid_cents)
FROM placements
WHERE status = 'placed' AND published_at IS NOT NULL
GROUP BY 1, 3;
CREATE VIEW v_product_site_latest AS
SELECT ps.id, ps.product_id, ps.site_id, ps.status, ps.comment, ps.imported_undecided,
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
                     AND x.status = 'placed') pp ON true
LEFT JOIN LATERAL (SELECT array_agg(DISTINCT pr.name ORDER BY pr.name) AS names
                   FROM placements x JOIN products pr ON pr.id = x.product_id
                   WHERE x.site_id = ps.site_id AND x.product_id <> ps.product_id
                     AND x.status = 'placed') op ON true;
CREATE VIEW v_site_funnel AS
SELECT ps.product_id, ps.status, ps.imported_undecided, count(*) AS sites
FROM product_sites ps
JOIN sites s ON s.id = ps.site_id
WHERE NOT s.is_deleted
GROUP BY ps.product_id, ps.status, ps.imported_undecided;
"""

OLD_VIEWS = """
CREATE VIEW v_keyword_coverage AS
SELECT
    k.id,
    k.product_id,
    k.keyword,
    k.tool,
    k.volume,
    k.target_url,
    (SELECT position FROM keyword_positions kp
      WHERE kp.keyword_id = k.id AND kp.country = 'US'
      ORDER BY checked_at DESC LIMIT 1) AS last_position,
    COUNT(pl.id) FILTER (WHERE p.status = 'published') AS links_placed,
    COUNT(pl.id) FILTER (WHERE p.status IN ('planned','ordered','writing','review')) AS links_waiting,
    k.global_volume,
    k.page_type,
    k.anchor_type::text AS anchor_type,
    k.share
FROM keywords k
LEFT JOIN placement_links pl ON pl.keyword_id = k.id
LEFT JOIN placements p ON p.id = pl.placement_id
WHERE k.is_active
GROUP BY k.id;
CREATE VIEW v_link_health AS
SELECT
    s.domain,
    p.id AS placement_id,
    p.published_at,
    pl.anchor,
    pl.is_alive,
    pl.last_checked_at,
    now() - p.published_at AS age
FROM placement_links pl
JOIN placements p ON p.id = pl.placement_id
JOIN sites s ON s.id = p.site_id
WHERE p.status = 'published';
CREATE VIEW v_monthly_spend AS
SELECT date_trunc('month', created_at)::date AS month,
       provider AS item, currency, sum(cost_cents) AS cost_cents
FROM api_usage
GROUP BY 1, 2, 3
UNION ALL
SELECT date_trunc('month', created_at)::date, 'llm:' || model, currency, sum(cost_cents)
FROM llm_calls
GROUP BY 1, 2, 3
UNION ALL
SELECT date_trunc('month', published_at)::date, 'placements',
       coalesce(currency, 'EUR'), sum(price_paid_cents)
FROM placements
WHERE status = 'published' AND published_at IS NOT NULL
GROUP BY 1, 3;
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
CREATE VIEW v_site_funnel AS
SELECT ps.product_id, ps.status, ps.imported_undecided, count(*) AS sites
FROM product_sites ps
JOIN sites s ON s.id = ps.site_id
WHERE NOT s.is_deleted
GROUP BY ps.product_id, ps.status, ps.imported_undecided;
"""

DROP_TRIGGERS = """
DROP TRIGGER product_sites_status_insert ON product_sites;
DROP TRIGGER product_sites_status_update ON product_sites;
DROP TRIGGER placements_status_insert ON placements;
DROP TRIGGER placements_status_update ON placements;
"""

# Текст — как в schema.sql: Postgres не даёт менять тип колонки, от которой
# зависит триггер, поэтому на время замены их нет.
TRIGGERS = """
CREATE TRIGGER product_sites_status_insert AFTER INSERT ON product_sites
    FOR EACH ROW WHEN (NEW.status <> 'new') EXECUTE FUNCTION log_site_status_change();
CREATE TRIGGER product_sites_status_update AFTER UPDATE OF status ON product_sites
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION log_site_status_change();
CREATE TRIGGER placements_status_insert AFTER INSERT ON placements
    FOR EACH ROW EXECUTE FUNCTION log_placement_status_change();
CREATE TRIGGER placements_status_update AFTER UPDATE OF status ON placements
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION log_placement_status_change();
"""

# Старые значения → новые. «Одобрена» и «Запланировано» — это «В работе»,
# «Размещались» и «Опубликовано» — «Размещено», отказы площадки и отменённые
# размещения — «Отказ», «На проверке» — «Написание статьи», «На аудите» — «В работе».
TO_WORK = """
CREATE TYPE work_status AS ENUM
    ('new','viewed','in_work','ordered','writing','placed','discarded','rejected','blacklisted');

ALTER TABLE product_sites ALTER COLUMN status DROP DEFAULT;
ALTER TABLE product_sites ALTER COLUMN status TYPE work_status
    USING (CASE status::text
             WHEN 'approved' THEN 'in_work'
             WHEN 'auditing' THEN 'in_work'
             WHEN 'declined' THEN 'rejected'
             ELSE status::text END)::work_status;
ALTER TABLE product_sites ALTER COLUMN status SET DEFAULT 'new';

ALTER TABLE placements ALTER COLUMN status DROP DEFAULT;
ALTER TABLE placements ALTER COLUMN status TYPE work_status
    USING (CASE status::text
             WHEN 'planned' THEN 'in_work'
             WHEN 'review' THEN 'writing'
             WHEN 'published' THEN 'placed'
             WHEN 'cancelled' THEN 'rejected'
             ELSE status::text END)::work_status;
ALTER TABLE placements ALTER COLUMN status SET DEFAULT 'in_work';

ALTER TABLE site_status_changes ALTER COLUMN from_status TYPE work_status
    USING (CASE from_status::text
             WHEN 'approved' THEN 'in_work'
             WHEN 'auditing' THEN 'in_work'
             WHEN 'declined' THEN 'rejected'
             ELSE from_status::text END)::work_status;
ALTER TABLE site_status_changes ALTER COLUMN to_status TYPE work_status
    USING (CASE to_status::text
             WHEN 'approved' THEN 'in_work'
             WHEN 'auditing' THEN 'in_work'
             WHEN 'declined' THEN 'rejected'
             ELSE to_status::text END)::work_status;

ALTER TABLE placement_status_changes ALTER COLUMN from_status TYPE work_status
    USING (CASE from_status::text
             WHEN 'planned' THEN 'in_work'
             WHEN 'review' THEN 'writing'
             WHEN 'published' THEN 'placed'
             WHEN 'cancelled' THEN 'rejected'
             ELSE from_status::text END)::work_status;
ALTER TABLE placement_status_changes ALTER COLUMN to_status TYPE work_status
    USING (CASE to_status::text
             WHEN 'planned' THEN 'in_work'
             WHEN 'review' THEN 'writing'
             WHEN 'published' THEN 'placed'
             WHEN 'cancelled' THEN 'rejected'
             ELSE to_status::text END)::work_status;

DROP TYPE site_status;
DROP TYPE placement_status;

ALTER TABLE product_sites RENAME COLUMN reject_reason TO comment;
"""

BACK = """
ALTER TABLE product_sites RENAME COLUMN comment TO reject_reason;

CREATE TYPE site_status AS ENUM
    ('new','viewed','approved','ordered','placed','discarded','declined','blacklisted','auditing');
CREATE TYPE placement_status AS ENUM
    ('planned','ordered','writing','review','published','rejected','cancelled');

ALTER TABLE product_sites ALTER COLUMN status DROP DEFAULT;
ALTER TABLE product_sites ALTER COLUMN status TYPE site_status
    USING (CASE status::text WHEN 'in_work' THEN 'approved' ELSE status::text END)::site_status;
ALTER TABLE product_sites ALTER COLUMN status SET DEFAULT 'new';

ALTER TABLE placements ALTER COLUMN status DROP DEFAULT;
ALTER TABLE placements ALTER COLUMN status TYPE placement_status
    USING (CASE status::text
             WHEN 'in_work' THEN 'planned'
             WHEN 'placed' THEN 'published'
             WHEN 'discarded' THEN 'rejected'
             WHEN 'blacklisted' THEN 'rejected'
             WHEN 'new' THEN 'planned'
             WHEN 'viewed' THEN 'planned'
             ELSE status::text END)::placement_status;
ALTER TABLE placements ALTER COLUMN status SET DEFAULT 'planned';

ALTER TABLE site_status_changes ALTER COLUMN from_status TYPE site_status
    USING (CASE from_status::text WHEN 'in_work' THEN 'approved' ELSE from_status::text END)::site_status;
ALTER TABLE site_status_changes ALTER COLUMN to_status TYPE site_status
    USING (CASE to_status::text WHEN 'in_work' THEN 'approved' ELSE to_status::text END)::site_status;

ALTER TABLE placement_status_changes ALTER COLUMN from_status TYPE placement_status
    USING (CASE from_status::text
             WHEN 'in_work' THEN 'planned'
             WHEN 'placed' THEN 'published'
             WHEN 'discarded' THEN 'rejected'
             WHEN 'blacklisted' THEN 'rejected'
             WHEN 'new' THEN 'planned'
             WHEN 'viewed' THEN 'planned'
             ELSE from_status::text END)::placement_status;
ALTER TABLE placement_status_changes ALTER COLUMN to_status TYPE placement_status
    USING (CASE to_status::text
             WHEN 'in_work' THEN 'planned'
             WHEN 'placed' THEN 'published'
             WHEN 'discarded' THEN 'rejected'
             WHEN 'blacklisted' THEN 'rejected'
             WHEN 'new' THEN 'planned'
             WHEN 'viewed' THEN 'planned'
             ELSE to_status::text END)::placement_status;

DROP TYPE work_status;
"""

STATUSES = [
    ("new", "Новая"),
    ("viewed", "Просмотрено"),
    ("in_work", "В работе"),
    ("ordered", "Заявка отправлена"),
    ("writing", "Написание статьи"),
    ("placed", "Размещено"),
    ("discarded", "Отбрасываю"),
    ("rejected", "Отказ"),
    ("blacklisted", "Чёрный список"),
]


class Migration(migrations.Migration):
    dependencies = [("sites", "0021_upload_changes"), ("placements", "0007_invoices")]

    operations = [
        migrations.RunSQL(DROP_VIEWS, OLD_VIEWS),
        migrations.RunSQL(DROP_TRIGGERS, TRIGGERS),
        migrations.RunSQL(TO_WORK, BACK),
        migrations.RunSQL(TRIGGERS, DROP_TRIGGERS),
        migrations.RunSQL(VIEWS, DROP_VIEWS),
        # Колонки и тип уже изменены SQL выше — Django остаётся только узнать об этом.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameField(
                    model_name="productsite", old_name="reject_reason", new_name="comment"
                ),
                migrations.AlterField(
                    model_name="productsite",
                    name="comment",
                    field=models.TextField(blank=True, null=True, verbose_name="комментарий"),
                ),
                migrations.AlterField(
                    model_name="productsite",
                    name="status",
                    field=config.db.PgEnumField(
                        choices=STATUSES,
                        db_default="new",
                        default="new",
                        enum_type="work_status",
                        verbose_name="статус",
                    ),
                ),
                migrations.AlterField(
                    model_name="sitestatuschange",
                    name="from_status",
                    field=config.db.PgEnumField(
                        blank=True,
                        choices=STATUSES,
                        enum_type="work_status",
                        null=True,
                        verbose_name="был",
                    ),
                ),
                migrations.AlterField(
                    model_name="sitestatuschange",
                    name="to_status",
                    field=config.db.PgEnumField(
                        choices=STATUSES, enum_type="work_status", verbose_name="стал"
                    ),
                ),
                migrations.AlterField(
                    model_name="productsitelatest",
                    name="comment",
                    field=models.TextField(null=True, verbose_name="комментарий"),
                ),
                migrations.AlterField(
                    model_name="productsitelatest",
                    name="status",
                    field=config.db.PgEnumField(
                        choices=STATUSES, enum_type="work_status", verbose_name="статус"
                    ),
                ),
            ],
        ),
    ]
