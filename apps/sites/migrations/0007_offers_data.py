# Перенос данных к рабочей цене и истории заметок (ADR-043).
#
# - Collaborator — продавец-каталог, его метрикам доверяем.
# - Все цены из таблицы линкбилдинга — его предложения (публикация, это
#   db_default колонки), уже разобранные: по ним решать нечего.
# - Рабочая цена площадки — её последнее предложение.
# - Заметки площадок и причины отказа — в историю заметок с источником
#   «таблица линкбилдинга»: других путей у них не было, только импорт.
#
# Модели здесь — исторические (apps.get_model), а не из models.py: миграция
# должна работать с той схемой, какая была на этом шаге.

from typing import Any

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.db.models import F, Q

TABLE_SOURCE = "таблица линкбилдинга"


def forward(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    seller_model: Any = apps.get_model("sites", "Seller")
    price_model: Any = apps.get_model("sites", "SitePrice")
    site_model: Any = apps.get_model("sites", "Site")
    note_model: Any = apps.get_model("sites", "SiteNote")
    product_site_model: Any = apps.get_model("sites", "ProductSite")

    collaborator = seller_model.objects.create(
        name="Collaborator",
        contacts="collaborator.pro",
        currency="EUR",
        is_collaborator=True,
        metrics_trusted=True,
    )
    price_model.objects.update(seller=collaborator, reviewed_at=F("checked_at"))
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sites s SET price_id = (
                SELECT x.id FROM site_prices x WHERE x.site_id = s.id
                ORDER BY x.checked_at DESC, x.id DESC LIMIT 1)
            """
        )

    empty = Q(notes__isnull=True) | Q(notes="")
    notes = [
        note_model(
            site_id=site.pk, body=site.notes, source=TABLE_SOURCE, created_at=site.created_at
        )
        for site in site_model.objects.exclude(empty).order_by("pk")
    ]
    no_reason = Q(reject_reason__isnull=True) | Q(reject_reason="")
    notes += [
        note_model(
            site_id=row.site_id,
            product_id=row.product_id,
            body=row.reject_reason,
            source=TABLE_SOURCE,
            created_at=row.updated_at,
        )
        for row in product_site_model.objects.exclude(no_reason).order_by("pk")
    ]
    note_model.objects.bulk_create(notes)


def backward(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    seller_model: Any = apps.get_model("sites", "Seller")
    price_model: Any = apps.get_model("sites", "SitePrice")
    site_model: Any = apps.get_model("sites", "Site")
    note_model: Any = apps.get_model("sites", "SiteNote")

    # Заметка площадки из таблицы — та, что без продукта; причины отказа и так
    # лежат в решении по продукту.
    for note in note_model.objects.filter(source=TABLE_SOURCE, product__isnull=True):
        site_model.objects.filter(pk=note.site_id).update(notes=note.body)
    note_model.objects.all().delete()
    site_model.objects.update(price=None)
    price_model.objects.update(seller=None, reviewed_at=None)
    seller_model.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0006_sellers_offers_notes_rates"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
