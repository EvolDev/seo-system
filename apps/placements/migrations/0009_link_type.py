from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("placements", "0008_work_status"), ("sites", "0027_offer_link_type")]

    operations = [
        migrations.AddField(
            model_name="placement",
            name="link_type",
            field=models.TextField(
                blank=True,
                null=True,
                choices=[("dofollow", "dofollow"), ("nofollow", "nofollow")],
                verbose_name="тип ссылки",
            ),
        ),
        migrations.AddField(
            model_name="placement",
            name="link_type_manual",
            field=models.BooleanField(default=False, db_default=False, editable=False),
        ),
        migrations.RunSQL(
            """UPDATE placements p SET link_type = price.link_type
               FROM sites s JOIN site_prices price ON price.id = s.price_id
               WHERE p.site_id = s.id AND p.seller_id = price.seller_id
                 AND p.placement_type = price.placement_type;""",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
