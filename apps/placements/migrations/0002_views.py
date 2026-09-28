# Представления — текстом из schema.sql, сырым SQL, а не через ORM (ADR-003, ADR-029).

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("placements", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
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
            """,
            reverse_sql="DROP VIEW v_link_health",
        ),
    ]
