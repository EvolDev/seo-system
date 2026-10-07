# Оценка площадки звёздочкой (E1-21, ADR-064): таблица оценок и среднее в
# представлениях. Среднее считается одной группировкой на все площадки, а не
# подзапросом на строку: на 45 000 площадок каталога такой подзапрос уже
# замедлял заметки (E1-08).
#
# Представления пересоздаются оба: v_product_site_latest стоит на v_site_latest,
# поэтому сначала сносится зависимое.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from config.db import PgNow

DROP = "DROP VIEW v_product_site_latest; DROP VIEW v_site_latest;"

NEW_VIEWS = """
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
       coalesce(ln.notes, 0) AS notes_count, ln.body AS last_note, ln.created_at AS last_note_at,
       rt.rating_avg, coalesce(rt.rating_count, 0) AS rating_count
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
LEFT JOIN (SELECT x.site_id, round(avg(x.value), 1) AS rating_avg,
                  count(*) AS rating_count
           FROM site_ratings x
           GROUP BY x.site_id) rt ON rt.site_id = s.id
WHERE NOT s.is_deleted;
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
       l.rating_avg, l.rating_count,
       a.verdict AS last_verdict, a.score AS last_score, a.created_at AS audited_at,
       pp.published AS placements_published,
       op.names AS other_products
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
LEFT JOIN LATERAL (SELECT array_agg(pr.name ORDER BY pr.name) AS names
                   FROM products pr
                   WHERE pr.id <> ps.product_id
                     AND EXISTS (SELECT 1 FROM placements x
                                 WHERE x.site_id = ps.site_id AND x.product_id = pr.id)) op ON true;
"""

OLD_VIEWS = """
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
       op.names AS other_products
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
LEFT JOIN LATERAL (SELECT array_agg(pr.name ORDER BY pr.name) AS names
                   FROM products pr
                   WHERE pr.id <> ps.product_id
                     AND EXISTS (SELECT 1 FROM placements x
                                 WHERE x.site_id = ps.site_id AND x.product_id = pr.id)) op ON true;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0023_other_products"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteRating",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("value", models.SmallIntegerField(verbose_name="оценка")),
                (
                    "created_at",
                    models.DateTimeField(db_default=PgNow(), verbose_name="первая оценка"),
                ),
                ("updated_at", models.DateTimeField(db_default=PgNow(), verbose_name="изменена")),
                (
                    "site",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ratings",
                        to="sites.site",
                        verbose_name="площадка",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="site_ratings",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="кто оценил",
                    ),
                ),
            ],
            options={
                "verbose_name": "оценка площадки",
                "verbose_name_plural": "оценки площадок",
                "db_table": "site_ratings",
            },
        ),
        migrations.AddIndex(
            model_name="siterating",
            index=models.Index(fields=["site"], name="idx_site_ratings_site"),
        ),
        migrations.AddConstraint(
            model_name="siterating",
            constraint=models.UniqueConstraint(
                fields=("site", "user"), name="site_ratings_site_user_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="siterating",
            constraint=models.CheckConstraint(
                condition=models.Q(value__gte=1, value__lte=5),
                name="site_ratings_value_check",
                violation_error_message="Оценка — от 1 до 5 звёзд.",
            ),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(DROP + NEW_VIEWS, DROP + OLD_VIEWS),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="productsitelatest",
                    name="rating_avg",
                    field=models.DecimalField(
                        decimal_places=1, max_digits=3, null=True, verbose_name="оценка"
                    ),
                ),
                migrations.AddField(
                    model_name="productsitelatest",
                    name="rating_count",
                    field=models.BigIntegerField(default=0, verbose_name="оценок"),
                    preserve_default=False,
                ),
            ],
        ),
    ]
