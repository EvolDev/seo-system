from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("sites", "0026_seller_ratings")]

    operations = [
        migrations.AddField(
            model_name="siteprice",
            name="link_type",
            field=models.TextField(
                blank=True,
                null=True,
                choices=[("dofollow", "dofollow"), ("nofollow", "nofollow")],
                verbose_name="тип ссылки",
            ),
        ),
        migrations.AddField(
            model_name="siteprice",
            name="link_type_manual",
            field=models.BooleanField(default=False, db_default=False, editable=False),
        ),
        migrations.RunSQL(
            """
UPDATE site_prices p SET link_type = s.link_type
FROM sites s, sellers seller
WHERE p.site_id = s.id AND p.seller_id = seller.id AND seller.is_collaborator
  AND s.source = 'Collaborator'
  AND p.placement_type = 'guest_post' AND s.link_type IN ('dofollow', 'nofollow')
  AND p.id IN (SELECT DISTINCT ON (x.site_id, x.seller_id, x.placement_type) x.id
               FROM site_prices x
               ORDER BY x.site_id, x.seller_id, x.placement_type, x.checked_at DESC, x.id DESC);
""",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            """
CREATE OR REPLACE VIEW v_site_offers AS
SELECT o.id, o.site_id, o.seller_id, sl.name AS seller, o.placement_type,
       o.placement_cents, o.currency,
       round(o.placement_cents / eur_rate(o.currency))::integer AS placement_eur_cents,
       o.announce_cents, o.writing_cents, o.gray_cents, o.extra,
       o.reviewed_at, o.checked_at, o.link_type
FROM (SELECT DISTINCT ON (x.site_id, x.seller_id, x.placement_type) *
      FROM site_prices x
      ORDER BY x.site_id, x.seller_id, x.placement_type, x.checked_at DESC, x.id DESC) o
JOIN sellers sl ON sl.id = o.seller_id;
CREATE OR REPLACE VIEW v_site_latest AS
SELECT s.id, s.domain, s.language, s.topics, s.declared_topics,
       s.links_allowed, pr.link_type, s.marks_as_ad,
       l.dr, l.organic_traffic, l.total_keywords,
       l.top_geo, l.top_geo_traffic, l.top_geo_at, l.metrics_at,
       l.metrics_trusted, ms.name AS metrics_seller,
       pr.id AS price_id, pr.seller_id AS price_seller_id, ps.name AS price_seller,
       pr.placement_type AS price_type,
       pr.placement_cents, pr.announce_cents, pr.writing_cents,
       pr.currency AS price_currency, pr.checked_at AS prices_at,
       round(pr.placement_cents / eur_rate(pr.currency))::integer AS placement_eur_cents,
       round(pr.writing_cents / eur_rate(pr.currency))::integer AS writing_eur_cents,
       round((pr.placement_cents + coalesce(pr.announce_cents, 0)) / eur_rate(pr.currency))::integer
         AS reference_total_cents,
       l.new_price_id, l.new_price_cents, l.new_price_currency, l.new_price_pending,
       l.cheaper_id, cs.name AS cheaper_seller, l.cheaper_cents, l.cheaper_currency,
       l.cheaper_eur_cents, l.cheaper_pending,
       EXISTS (SELECT 1 FROM site_prices x
               WHERE x.site_id = s.id AND x.reviewed_at IS NULL) AS offers_pending,
       g.ratio AS gray_ratio,
       coalesce(ln.notes, 0) AS notes_count, ln.body AS last_note, ln.created_at AS last_note_at,
       rt.rating_avg, coalesce(rt.rating_count, 0) AS rating_count
FROM sites s
LEFT JOIN site_latest l ON l.site_id = s.id
LEFT JOIN sellers ms ON ms.id = l.metrics_seller_id
LEFT JOIN sellers cs ON cs.id = l.cheaper_seller_id
LEFT JOIN site_prices pr ON pr.id = s.price_id
LEFT JOIN sellers ps ON ps.id = pr.seller_id
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
""",
            reverse_sql="""
DROP VIEW v_product_site_latest; DROP VIEW v_site_latest; DROP VIEW v_site_offers;
CREATE VIEW v_site_offers AS
SELECT o.id, o.site_id, o.seller_id, sl.name AS seller, o.placement_type,
       o.placement_cents, o.currency,
       round(o.placement_cents / eur_rate(o.currency))::integer AS placement_eur_cents,
       o.announce_cents, o.writing_cents, o.gray_cents, o.extra,
       o.reviewed_at, o.checked_at
FROM (SELECT DISTINCT ON (x.site_id, x.seller_id, x.placement_type)
             x.id, x.site_id, x.seller_id, x.placement_type,
             x.placement_cents, x.currency, x.announce_cents, x.writing_cents,
             x.gray_cents, x.extra, x.reviewed_at, x.checked_at
      FROM site_prices x
      ORDER BY x.site_id, x.seller_id, x.placement_type, x.checked_at DESC, x.id DESC) o
JOIN sellers sl ON sl.id = o.seller_id;

CREATE VIEW v_site_latest AS
SELECT s.id, s.domain, s.language, s.topics, s.declared_topics,
       s.links_allowed, s.link_type, s.marks_as_ad,
       l.dr, l.organic_traffic, l.total_keywords,
       l.top_geo, l.top_geo_traffic, l.top_geo_at, l.metrics_at,
       l.metrics_trusted, ms.name AS metrics_seller,
       pr.id AS price_id, pr.seller_id AS price_seller_id, ps.name AS price_seller,
       pr.placement_type AS price_type,
       pr.placement_cents, pr.announce_cents, pr.writing_cents,
       pr.currency AS price_currency, pr.checked_at AS prices_at,
       round(pr.placement_cents / eur_rate(pr.currency))::integer AS placement_eur_cents,
       round(pr.writing_cents / eur_rate(pr.currency))::integer AS writing_eur_cents,
       round((pr.placement_cents + coalesce(pr.announce_cents, 0)) / eur_rate(pr.currency))::integer
         AS reference_total_cents,
       l.new_price_id, l.new_price_cents, l.new_price_currency, l.new_price_pending,
       l.cheaper_id, cs.name AS cheaper_seller, l.cheaper_cents, l.cheaper_currency,
       l.cheaper_eur_cents, l.cheaper_pending,
       EXISTS (SELECT 1 FROM site_prices x
               WHERE x.site_id = s.id AND x.reviewed_at IS NULL) AS offers_pending,
       g.ratio AS gray_ratio,
       coalesce(ln.notes, 0) AS notes_count, ln.body AS last_note, ln.created_at AS last_note_at,
       rt.rating_avg, coalesce(rt.rating_count, 0) AS rating_count
FROM sites s
LEFT JOIN site_latest l ON l.site_id = s.id
LEFT JOIN sellers ms ON ms.id = l.metrics_seller_id
LEFT JOIN sellers cs ON cs.id = l.cheaper_seller_id
LEFT JOIN site_prices pr ON pr.id = s.price_id
LEFT JOIN sellers ps ON ps.id = pr.seller_id
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

""",
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="siteoffer",
                    name="link_type",
                    field=models.TextField(
                        null=True,
                        choices=[("dofollow", "dofollow"), ("nofollow", "nofollow")],
                        verbose_name="тип ссылки",
                    ),
                ),
            ]
        ),
    ]
