# «Другие продукты» — размещения в любом статусе, а не только опубликованные (E1-19).
# Считаем от списка продуктов (их единицы) с EXISTS по индексу
# `idx_placements_site`, а не свёрткой всех размещений площадки: так боковой
# запрос стоит одинаково дёшево при любом их числе.

from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models

DROP = "DROP VIEW v_product_site_latest;"

NEW_VIEW = """
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

OLD_VIEW = """
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
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0022_work_status"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(DROP + NEW_VIEW, DROP + OLD_VIEW),
            ],
            state_operations=[
                migrations.RenameField(
                    model_name="productsitelatest",
                    old_name="other_products_placed",
                    new_name="other_products",
                ),
                migrations.AlterField(
                    model_name="productsitelatest",
                    name="other_products",
                    field=ArrayField(
                        base_field=models.TextField(),
                        null=True,
                        size=None,
                        verbose_name="другие продукты на площадке",
                    ),
                ),
            ],
        ),
    ]
