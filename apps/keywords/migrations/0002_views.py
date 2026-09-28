# Представления — текстом из schema.sql, сырым SQL, а не через ORM (ADR-003, ADR-029).

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("keywords", "0001_initial"),
        ("placements", "0001_initial"),
    ]

    operations = [
        # links_placed — только published; links_waiting — planned, ordered,
        # writing, review. Больше не проставляется руками.
        migrations.RunSQL(
            sql="""
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
                    COUNT(pl.id) FILTER (WHERE p.status IN ('planned','ordered','writing','review')) AS links_waiting
                FROM keywords k
                LEFT JOIN placement_links pl ON pl.keyword_id = k.id
                LEFT JOIN placements p ON p.id = pl.placement_id
                WHERE k.is_active
                GROUP BY k.id;
            """,
            reverse_sql="DROP VIEW v_keyword_coverage",
        ),
    ]
