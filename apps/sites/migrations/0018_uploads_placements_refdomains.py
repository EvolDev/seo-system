"""Загрузки размещений и ссылающиеся домены Ahrefs (E1-09, ADR-051, schema.sql 1.13).

- `uploads`: продукт — у размещений и ссылающихся доменов обязателен;
  сотрудник — «от кого» у файла размещений; продавец не обязателен и у них.
- `product_ref_domains` — кто ссылается на продукт по выгрузке Ahrefs
  «Referring domains»: отдельно от площадок, по паре продукт + домен.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import config.db


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0017_upload_kinds"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductRefDomain",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("domain", models.TextField(verbose_name="домен")),
                (
                    "first_seen_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="First seen у Ahrefs.",
                        null=True,
                        verbose_name="ссылается с",
                    ),
                ),
                (
                    "lost_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Lost у Ahrefs.",
                        null=True,
                        verbose_name="ссылка пропала",
                    ),
                ),
                ("seen_on", models.DateField(verbose_name="в выгрузке от")),
                (
                    "missing_since",
                    models.DateField(blank=True, null=True, verbose_name="нет в выгрузках с"),
                ),
                (
                    "created_at",
                    models.DateTimeField(db_default=config.db.PgNow(), verbose_name="добавлен"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True, db_default=config.db.PgNow(), verbose_name="изменён"
                    ),
                ),
            ],
            options={
                "verbose_name": "ссылающийся домен",
                "verbose_name_plural": "ссылающиеся домены",
                "db_table": "product_ref_domains",
            },
        ),
        migrations.RemoveConstraint(
            model_name="upload",
            name="uploads_seller_check",
        ),
        migrations.AddField(
            model_name="upload",
            name="employee",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="сотрудник",
            ),
        ),
        migrations.AddField(
            model_name="upload",
            name="product",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="uploads",
                to="sites.product",
                verbose_name="продукт",
            ),
        ),
        migrations.AddConstraint(
            model_name="upload",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("seller__isnull", False),
                    ("kind__in", ["ahrefs_batch", "placements", "ahrefs_refdomains"]),
                    _connector="OR",
                ),
                name="uploads_seller_check",
                violation_error_message="У прайса и каталога должен быть продавец.",
            ),
        ),
        migrations.AddConstraint(
            model_name="upload",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("product__isnull", False),
                    ("kind__in", ["price_list", "collaborator_catalog", "ahrefs_batch"]),
                    _connector="OR",
                ),
                name="uploads_product_check",
                violation_error_message="У размещений и ссылающихся доменов должен быть продукт.",
            ),
        ),
        migrations.AddField(
            model_name="productrefdomain",
            name="product",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ref_domains",
                to="sites.product",
                verbose_name="продукт",
            ),
        ),
        migrations.AddField(
            model_name="productrefdomain",
            name="upload",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="sites.upload",
                verbose_name="выгрузка",
            ),
        ),
        migrations.AddConstraint(
            model_name="productrefdomain",
            constraint=models.UniqueConstraint(
                fields=("product", "domain"), name="product_ref_domains_product_id_domain_key"
            ),
        ),
    ]
