# Представления от рабочей цены площадки (ADR-043) — текстом из schema.sql 1.7,
# сырым SQL (ADR-003, ADR-029). CREATE OR REPLACE не годится: новые колонки
# встают в середину, и тип reference_total меняется, — поэтому DROP и CREATE.
# v_product_site_latest стоит на v_site_latest и пересоздаётся вместе с ним.
# Пересчёт в евро — функция eur_rate(), текущие предложения — v_site_offers:
# одно правило на все представления и экраны.

from django.db import migrations

OLD_SITE_LATEST = """
                CREATE VIEW v_site_latest AS
                SELECT s.id, s.domain, s.language, s.topics, s.declared_topics,
                       s.links_allowed, s.link_type, s.marks_as_ad,
                       m.dr, m.organic_traffic, m.total_keywords, m.top_geo, m.checked_at AS metrics_at,
                       pr.placement_cents, pr.announce_cents, pr.writing_cents, pr.checked_at AS prices_at,
                       coalesce(pr.placement_cents, 0) + coalesce(pr.announce_cents, 0) AS reference_total_cents,
                       g.ratio AS gray_ratio
                FROM sites s
                LEFT JOIN LATERAL (SELECT * FROM site_metrics x WHERE x.site_id = s.id
                                   ORDER BY checked_at DESC LIMIT 1) m ON true
                LEFT JOIN LATERAL (SELECT * FROM site_prices x WHERE x.site_id = s.id
                                   ORDER BY checked_at DESC LIMIT 1) pr ON true
                LEFT JOIN LATERAL (SELECT * FROM gray_scans x WHERE x.site_id = s.id
                                   ORDER BY checked_at DESC LIMIT 1) g ON true
                WHERE NOT s.is_deleted;
"""

OLD_PRODUCT_SITE_LATEST = """
                CREATE VIEW v_product_site_latest AS
                SELECT ps.id, ps.product_id, ps.site_id, ps.status, ps.reject_reason, ps.imported_undecided,
                       l.domain, l.language, l.topics, l.declared_topics,
                       l.links_allowed, l.link_type, l.marks_as_ad,
                       l.dr, l.organic_traffic, l.total_keywords, l.top_geo, l.metrics_at,
                       l.placement_cents, l.announce_cents, l.writing_cents, l.prices_at,
                       l.reference_total_cents,
                       (l.writing_cents IS NULL OR l.writing_cents > w.writing_cents) AS we_write,
                       l.reference_total_cents
                         + CASE WHEN w.writing_cents IS NULL THEN NULL
                                WHEN l.writing_cents <= w.writing_cents THEN l.writing_cents
                                ELSE 0 END AS expected_spend_cents,
                       l.gray_ratio,
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


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0008_offers_required"),
        ("placements", "0004_seller_employee"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP VIEW v_product_site_latest; DROP VIEW v_site_latest;",
            reverse_sql=OLD_SITE_LATEST + OLD_PRODUCT_SITE_LATEST,
        ),
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION eur_rate(cur char(3)) RETURNS numeric
                LANGUAGE sql STABLE AS $$
                    SELECT CASE WHEN cur = 'EUR' THEN 1
                                ELSE (SELECT r.rate FROM exchange_rates r WHERE r.currency = cur
                                      ORDER BY r.rate_date DESC LIMIT 1) END
                $$;
            """,
            reverse_sql="DROP FUNCTION eur_rate(char)",
        ),
        migrations.RunSQL(
            sql="""
                CREATE VIEW v_site_offers AS
                SELECT o.id, o.site_id, o.seller_id, sl.name AS seller, o.placement_type,
                       o.placement_cents, o.currency,
                       round(o.placement_cents / eur_rate(o.currency))::integer AS placement_eur_cents,
                       o.announce_cents, o.writing_cents, o.gray_cents, o.extra,
                       o.reviewed_at, o.checked_at
                FROM (SELECT DISTINCT ON (x.site_id, x.seller_id, x.placement_type) *
                      FROM site_prices x
                      ORDER BY x.site_id, x.seller_id, x.placement_type, x.checked_at DESC, x.id DESC) o
                JOIN sellers sl ON sl.id = o.seller_id;
            """,
            reverse_sql="DROP VIEW v_site_offers",
        ),
        migrations.RunSQL(
            sql="""
                CREATE VIEW v_site_latest AS
                SELECT s.id, s.domain, s.language, s.topics, s.declared_topics,
                       s.links_allowed, s.link_type, s.marks_as_ad,
                       m.dr, m.organic_traffic, m.total_keywords, m.top_geo, m.checked_at AS metrics_at,
                       m.trusted AS metrics_trusted, ms.name AS metrics_seller,
                       pr.id AS price_id, pr.seller_id AS price_seller_id, ps.name AS price_seller,
                       pr.placement_type AS price_type,
                       pr.placement_cents, pr.announce_cents, pr.writing_cents,
                       pr.currency AS price_currency, pr.checked_at AS prices_at,
                       round(pr.placement_cents / eur_rate(pr.currency))::integer AS placement_eur_cents,
                       round(pr.writing_cents / eur_rate(pr.currency))::integer AS writing_eur_cents,
                       round((pr.placement_cents + coalesce(pr.announce_cents, 0)) / eur_rate(pr.currency))::integer
                         AS reference_total_cents,
                       np.id AS new_price_id, np.placement_cents AS new_price_cents,
                       np.currency AS new_price_currency, np.reviewed_at IS NULL AS new_price_pending,
                       ch.id AS cheaper_id, ch.seller AS cheaper_seller, ch.placement_cents AS cheaper_cents,
                       ch.currency AS cheaper_currency, ch.placement_eur_cents AS cheaper_eur_cents,
                       ch.reviewed_at IS NULL AS cheaper_pending,
                       EXISTS (SELECT 1 FROM site_prices x
                               WHERE x.site_id = s.id AND x.reviewed_at IS NULL) AS offers_pending,
                       g.ratio AS gray_ratio,
                       nc.notes AS notes_count, ln.body AS last_note, ln.created_at AS last_note_at
                FROM sites s
                LEFT JOIN LATERAL (SELECT x.dr, x.organic_traffic, x.total_keywords, x.top_geo, x.checked_at,
                                          x.seller_id, x.seller_id IS NULL OR xs.metrics_trusted AS trusted
                                   FROM site_metrics x LEFT JOIN sellers xs ON xs.id = x.seller_id
                                   WHERE x.site_id = s.id
                                   ORDER BY x.seller_id IS NULL OR xs.metrics_trusted DESC, x.checked_at DESC
                                   LIMIT 1) m ON true
                LEFT JOIN sellers ms ON ms.id = m.seller_id
                LEFT JOIN site_prices pr ON pr.id = s.price_id
                LEFT JOIN sellers ps ON ps.id = pr.seller_id
                LEFT JOIN LATERAL (SELECT o.id, o.placement_cents, o.currency, o.reviewed_at
                                   FROM v_site_offers o
                                   WHERE o.site_id = s.id AND o.seller_id = pr.seller_id
                                     AND o.placement_type = pr.placement_type AND o.id <> pr.id) np ON true
                LEFT JOIN LATERAL (SELECT o.id, o.seller, o.placement_cents, o.currency, o.placement_eur_cents,
                                          o.reviewed_at
                                   FROM v_site_offers o
                                   WHERE o.site_id = s.id AND o.placement_type = pr.placement_type
                                     AND o.seller_id <> pr.seller_id
                                     AND o.placement_eur_cents < round(pr.placement_cents / eur_rate(pr.currency))
                                   ORDER BY o.placement_eur_cents, o.id LIMIT 1) ch ON true
                LEFT JOIN LATERAL (SELECT * FROM gray_scans x WHERE x.site_id = s.id
                                   ORDER BY checked_at DESC LIMIT 1) g ON true
                LEFT JOIN LATERAL (SELECT count(*) AS notes FROM site_notes x WHERE x.site_id = s.id) nc ON true
                LEFT JOIN LATERAL (SELECT x.body, x.created_at FROM site_notes x WHERE x.site_id = s.id
                                   ORDER BY x.created_at DESC, x.id DESC LIMIT 1) ln ON true
                WHERE NOT s.is_deleted;
            """,
            reverse_sql="DROP VIEW v_site_latest",
        ),
        migrations.RunSQL(
            sql="""
                CREATE VIEW v_product_site_latest AS
                SELECT ps.id, ps.product_id, ps.site_id, ps.status, ps.reject_reason, ps.imported_undecided,
                       l.domain, l.language, l.topics, l.declared_topics,
                       l.links_allowed, l.link_type, l.marks_as_ad,
                       l.dr, l.organic_traffic, l.total_keywords, l.top_geo, l.metrics_at,
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
            """,
            reverse_sql="DROP VIEW v_product_site_latest",
        ),
    ]
