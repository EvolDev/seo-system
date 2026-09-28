# Представления — текстом из schema.sql, сырым SQL, а не через ORM (ADR-003, ADR-029).

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0002_site_lists"),
        ("content", "0002_domain_settings"),
        ("placements", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE VIEW v_site_funnel AS
                SELECT ps.product_id, ps.status, ps.imported_undecided, count(*) AS sites
                FROM product_sites ps
                JOIN sites s ON s.id = ps.site_id
                WHERE NOT s.is_deleted
                GROUP BY ps.product_id, ps.status, ps.imported_undecided;
            """,
            reverse_sql="DROP VIEW v_site_funnel",
        ),
        # Площадка «на сегодня», только факты. reference_total — размещение + анонс,
        # написание в него не входит никогда.
        migrations.RunSQL(
            sql="""
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
            """,
            reverse_sql="DROP VIEW v_site_latest",
        ),
        # we_write и expected_spend вычисляются здесь и больше нигде. Порог
        # написания — writing_eur из PRICE_REFERENCE, локальное перекрывает общее;
        # настройки нет — expected_spend пусто.
        migrations.RunSQL(
            sql="""
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
            """,
            reverse_sql="DROP VIEW v_product_site_latest",
        ),
    ]
