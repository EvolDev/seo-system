"""Какие настройки бывают и какое значение действует для продукта.

Ключи — `13-CONFIG.md` §2.2–2.3. Любую из них продукт может переопределить
своим значением (ADR-035). Ключа нет в списке — его нельзя ввести в админке:
настройку, которую не читает код, никто не заметит. Новая настройка —
строка здесь вместе с кодом, который её читает.
"""

from typing import Any

from django.db.models import F, Q

from apps.content.models import DomainSetting

SETTING_KEYS: dict[str, str] = {
    # Аудит площадки — 13-CONFIG.md §2.2
    "TRAFFIC_ZONES": "Зоны органического трафика: green, yellow",
    "DR_ZONES": "Зоны DR: green, yellow",
    "KEYWORDS_ZONES": "Зоны числа ключей в органике: green, yellow",
    "GRAY_ZONES": "Зоны доли серых страниц, %: green, yellow",
    "PRICE_REFERENCE": "Ценовой ориентир, EUR: total_eur, writing_eur, announce_eur",
    "PROJECT_TOPICS": "Тематики продукта — в написании Collaborator",
    "SPECIAL_TOPICS": "Особые тематики Collaborator",
    "GRAY_TERMS": "Категории и термины серости",
    # Контент — 13-CONFIG.md §2.3
    "SIMILARITY_THRESHOLD": "Порог близости статей: max_cosine",
    "FACT_ROTATION_WINDOW": "Окно ротации фактов: last_n_articles",
    "REVISION_LIMIT": "Предел итераций правок: max_iterations",
    "ANCHOR_REUSE_WINDOW": "Повтор анкора: days, max_on_similar_sites",
    "AUTHORITY_DOMAINS": "Белый список авторитетных доменов",
    "TOOL_CATEGORIES": "Разделы сайта продукта — значения keywords.tool",
}

# Эти настройки есть у каждого продукта — на его странице для них свои поля.
PRODUCT_KEYS = ("PRICE_REFERENCE", "PROJECT_TOPICS", "TOOL_CATEGORIES", "AUTHORITY_DOMAINS")


def set_product_setting(product_id: int, key: str, value: Any | None) -> None:
    """Задаёт локальное значение продукта; `None` — удаляет его.

    Без локального значения продукт снова работает по общему (ADR-035):
    настройка — конфигурация, не история, поэтому строка удаляется.
    Значение не изменилось — строка не трогается, `updated_at` прежний.
    """
    if key not in SETTING_KEYS:
        raise ValueError(f"Неизвестная настройка {key!r}")
    current = DomainSetting.objects.filter(key=key, product_id=product_id).first()
    if value is None:
        if current is not None:
            current.delete()
    elif current is None:
        DomainSetting.objects.create(key=key, product_id=product_id, value=value)
    elif current.value != value:
        current.value = value
        current.save(update_fields=["value", "updated_at"])


def get_setting(key: str, product_id: int | None) -> Any | None:
    """Значение настройки для продукта: его локальное, иначе общее, иначе None.

    `product_id=None` — только общее значение.
    """
    scope = Q(product__isnull=True)
    if product_id is not None:
        scope |= Q(product_id=product_id)
    return (
        DomainSetting.objects.filter(scope, key=key)
        .order_by(F("product_id").asc(nulls_last=True))
        .values_list("value", flat=True)
        .first()
    )
