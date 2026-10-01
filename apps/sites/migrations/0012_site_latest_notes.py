"""v_site_latest: заметки — одним подзапросом на все площадки (E1-08).

Число заметок и последняя заметка считались подзапросом на каждую строку,
и Postgres для маленькой таблицы заметок выбирал полный её просмотр — по
два на каждую из 45 000 площадок каталога: около секунды из двух на
странице «Площадки». Теперь заметки собираются один раз и соединяются по
площадке. Колонки представления те же — CREATE OR REPLACE, зависимое
v_product_site_latest пересоздавать не нужно.
"""

from django.db import migrations

NEW = """
CREATE OR REPLACE VIEW v_site_latest AS
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

OLD = """
CREATE OR REPLACE VIEW v_site_latest AS
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
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0011_uploads"),
    ]

    operations = [
        migrations.RunSQL(sql=NEW, reverse_sql=OLD),
    ]
