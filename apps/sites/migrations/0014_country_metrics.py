"""Трафик по странам, выгрузки Ahrefs без продавца, топ-регион (ADR-045, schema.sql 1.9).

- `site_country_metrics` — снимок трафика и ключей площадки в стране. Туда же
  переезжает US Traff из таблицы (`site_metrics.us_traffic`): это трафик США
  у каждой площадки, а не топ-регион. Колонка убирается, чтобы трафик США
  не хранился в двух местах.
- `uploads`: продавец пуст только у выгрузки Ahrefs, `country` — страна выгрузки.
- `v_site_latest`: топ-регион и его трафик — из последнего замера, где они
  есть: замер каталога без гео их не стирает. Колонки встают в середину —
  поэтому DROP и CREATE, вместе с v_product_site_latest, который стоит на нём.
- `v_site_country_latest` — последний замер по каждой стране.
"""

import django.db.models.deletion
from django.db import migrations, models

import config.db

OLD_SITE_LATEST = """
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
       coalesce(ln.notes, 0) AS notes_count, ln.body AS last_note, ln.created_at AS last_note_at
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
LEFT JOIN (SELECT DISTINCT ON (x.site_id) x.site_id, x.body, x.created_at,
                  count(*) OVER (PARTITION BY x.site_id) AS notes
           FROM site_notes x
           ORDER BY x.site_id, x.created_at DESC, x.id DESC) ln ON ln.site_id = s.id
WHERE NOT s.is_deleted;
"""

OLD_PRODUCT_SITE_LATEST = """
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
"""

NEW_SITE_LATEST = """
CREATE VIEW v_site_latest AS
SELECT s.id, s.domain, s.language, s.topics, s.declared_topics,
       s.links_allowed, s.link_type, s.marks_as_ad,
       m.dr, m.organic_traffic, m.total_keywords,
       tg.top_geo, tg.top_geo_traffic, tg.checked_at AS top_geo_at, m.checked_at AS metrics_at,
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
       coalesce(ln.notes, 0) AS notes_count, ln.body AS last_note, ln.created_at AS last_note_at
FROM sites s
LEFT JOIN LATERAL (SELECT x.dr, x.organic_traffic, x.total_keywords, x.checked_at,
                          x.seller_id, x.seller_id IS NULL OR xs.metrics_trusted AS trusted
                   FROM site_metrics x LEFT JOIN sellers xs ON xs.id = x.seller_id
                   WHERE x.site_id = s.id
                   ORDER BY x.seller_id IS NULL OR xs.metrics_trusted DESC, x.checked_at DESC
                   LIMIT 1) m ON true
LEFT JOIN sellers ms ON ms.id = m.seller_id
LEFT JOIN LATERAL (SELECT x.top_geo, x.top_geo_traffic, x.checked_at
                   FROM site_metrics x LEFT JOIN sellers xs ON xs.id = x.seller_id
                   WHERE x.site_id = s.id AND x.top_geo IS NOT NULL
                   ORDER BY x.seller_id IS NULL OR xs.metrics_trusted DESC, x.checked_at DESC
                   LIMIT 1) tg ON true
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
LEFT JOIN (SELECT DISTINCT ON (x.site_id) x.site_id, x.body, x.created_at,
                  count(*) OVER (PARTITION BY x.site_id) AS notes
           FROM site_notes x
           ORDER BY x.site_id, x.created_at DESC, x.id DESC) ln ON ln.site_id = s.id
WHERE NOT s.is_deleted;
"""

NEW_PRODUCT_SITE_LATEST = """
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

COUNTRY_LATEST = """
CREATE VIEW v_site_country_latest AS
SELECT DISTINCT ON (x.site_id, x.country)
       x.id, x.site_id, x.country, x.organic_traffic, x.total_keywords, x.source, x.checked_at
FROM site_country_metrics x
ORDER BY x.site_id, x.country, x.checked_at DESC, x.id DESC;
"""

# Только наши замеры: трафик по странам продавцы не присылают.
MOVE_US_TRAFFIC = """
INSERT INTO site_country_metrics (site_id, country, organic_traffic, source, checked_at)
SELECT site_id, 'us', us_traffic, source, checked_at
FROM site_metrics
WHERE us_traffic IS NOT NULL AND seller_id IS NULL;
"""

RESTORE_US_TRAFFIC = """
UPDATE site_metrics m SET us_traffic = c.organic_traffic
FROM site_country_metrics c
WHERE c.site_id = m.site_id AND c.country = 'us' AND c.source = m.source
  AND c.checked_at = m.checked_at AND m.seller_id IS NULL;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0013_ahrefs_batch_enums"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP VIEW v_product_site_latest; DROP VIEW v_site_latest;",
            reverse_sql=OLD_SITE_LATEST + OLD_PRODUCT_SITE_LATEST,
        ),
        migrations.CreateModel(
            name="SiteCountryMetric",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("country", models.CharField(max_length=2, verbose_name="страна")),
                (
                    "organic_traffic",
                    models.IntegerField(blank=True, null=True, verbose_name="органический трафик"),
                ),
                (
                    "total_keywords",
                    models.IntegerField(blank=True, null=True, verbose_name="ключей в органике"),
                ),
                (
                    "source",
                    config.db.PgEnumField(
                        choices=[
                            ("ahrefs_api", "Ahrefs API"),
                            ("serp_api", "SERP API"),
                            ("manual", "Вручную"),
                            ("csv_import", "Импорт из файла"),
                            ("collaborator_api", "Collaborator API"),
                            ("ahrefs_batch", "Ahrefs Batch Analysis"),
                        ],
                        db_default="manual",
                        default="manual",
                        enum_type="metric_source",
                        verbose_name="источник",
                    ),
                ),
                ("raw", models.JSONField(blank=True, null=True, verbose_name="сырой ответ")),
                (
                    "checked_at",
                    models.DateTimeField(db_default=config.db.PgNow(), verbose_name="дата замера"),
                ),
                (
                    "site",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="country_metrics",
                        to="sites.site",
                        verbose_name="площадка",
                    ),
                ),
            ],
            options={
                "verbose_name": "метрики по стране",
                "verbose_name_plural": "метрики по странам",
                "db_table": "site_country_metrics",
                "indexes": [
                    models.Index(
                        fields=["site", "country", "-checked_at"], name="idx_country_metrics_site"
                    )
                ],
            },
        ),
        migrations.RunSQL(sql=MOVE_US_TRAFFIC, reverse_sql=RESTORE_US_TRAFFIC),
        migrations.RemoveField(model_name="sitemetric", name="us_traffic"),
        migrations.AddField(
            model_name="upload",
            name="country",
            field=models.CharField(
                blank=True, max_length=2, null=True, verbose_name="страна выгрузки"
            ),
        ),
        migrations.AlterField(
            model_name="upload",
            name="seller",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="uploads",
                to="sites.seller",
                verbose_name="продавец",
            ),
        ),
        migrations.AddConstraint(
            model_name="upload",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("seller__isnull", False), ("kind", "ahrefs_batch"), _connector="OR"
                ),
                name="uploads_seller_check",
                violation_error_message="У прайса и каталога должен быть продавец.",
            ),
        ),
        migrations.RunSQL(
            sql=NEW_SITE_LATEST + NEW_PRODUCT_SITE_LATEST + COUNTRY_LATEST,
            reverse_sql=(
                "DROP VIEW v_site_country_latest; DROP VIEW v_product_site_latest; "
                "DROP VIEW v_site_latest;"
            ),
        ),
        migrations.CreateModel(
            name="SiteCountryLatest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("country", models.CharField(max_length=2, verbose_name="страна")),
                (
                    "organic_traffic",
                    models.IntegerField(null=True, verbose_name="органический трафик"),
                ),
                (
                    "total_keywords",
                    models.IntegerField(null=True, verbose_name="ключей в органике"),
                ),
                (
                    "source",
                    config.db.PgEnumField(
                        choices=[
                            ("ahrefs_api", "Ahrefs API"),
                            ("serp_api", "SERP API"),
                            ("manual", "Вручную"),
                            ("csv_import", "Импорт из файла"),
                            ("collaborator_api", "Collaborator API"),
                            ("ahrefs_batch", "Ahrefs Batch Analysis"),
                        ],
                        enum_type="metric_source",
                        verbose_name="источник",
                    ),
                ),
                ("checked_at", models.DateTimeField(verbose_name="дата замера")),
            ],
            options={
                "verbose_name": "последний замер по стране",
                "verbose_name_plural": "последние замеры по странам",
                "db_table": "v_site_country_latest",
                "managed": False,
            },
        ),
    ]
