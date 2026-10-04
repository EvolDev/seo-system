"""Какие настройки бывают и какое значение действует для продукта.

Ключи — `13-CONFIG.md` §2.2–2.3. Любую из них продукт может переопределить
своим значением (ADR-035). Ключа нет в списке — его нельзя ввести в админке:
настройку, которую не читает код, никто не заметит. Новая настройка —
строка здесь вместе с кодом, который её читает.
"""

from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any

from django.core.exceptions import ImproperlyConfigured
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
    # Проверки — 13-CONFIG.md §2.4
    "INDEXATION_SCHEDULE": (
        "Проверка индексации: enabled, first_check_days, retry_days, recheck_days, alert_after_days"
    ),
    # Загрузка цен — 13-CONFIG.md §2.5
    "OFFER_RECHECK": "Повторный разбор предложения продавца: min_change_pct",
    "UPLOAD_PRICE_CAP": "Цена из файла похожа на ошибку, если больше: eur",
}

# Эти настройки есть у каждого продукта — на его странице для них свои поля.
PRODUCT_KEYS = ("PRICE_REFERENCE", "PROJECT_TOPICS", "TOOL_CATEGORIES", "AUTHORITY_DOMAINS")


@dataclass(frozen=True)
class IndexationSchedule:
    """Сроки проверки индексации — настройка `INDEXATION_SCHEDULE` (04-DOMAIN-RULES.md §4).

    `enabled` — проверять ли по расписанию; кнопка в карточке работает
    всегда. Сроки — в днях: первая проверка после публикации, повтор, пока
    статьи нет в индексе, перепроверка статьи в индексе, оповещение после
    стольких дней без индекса (0 — при первой же неудаче).
    """

    enabled: bool
    first_check_days: int
    retry_days: int
    recheck_days: int
    alert_after_days: int

    @classmethod
    def parse(cls, value: Any) -> "IndexationSchedule":
        """Значение настройки → сроки; не та форма — ValueError с понятным текстом."""
        names = [field.name for field in fields(cls)]
        if not isinstance(value, dict) or set(value) != set(names):
            raise ValueError("Нужен объект ровно с полями: " + ", ".join(names) + ".")
        if not isinstance(value["enabled"], bool):
            raise ValueError("enabled — true или false.")
        for name in names[1:]:
            # bool в Python — тоже int: true в поле срока не пропускаем.
            number = value[name]
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                raise ValueError(f"{name} — целое число дней, не меньше нуля.")
        for name in ("retry_days", "recheck_days"):
            if value[name] < 1:
                raise ValueError(f"{name} — не меньше одного дня.")
        return cls(**value)


@dataclass(frozen=True)
class OfferRecheck:
    """Когда разобранное предложение продавца снова ждёт решения — `OFFER_RECHECK` (ADR-044).

    Предложение другого продавца, которое человек уже видел и оставил, при
    следующей загрузке снова попадает в разбор, только если цена изменилась
    больше чем на `min_change_pct` процентов: цены в евро плывут с курсом.
    """

    min_change_pct: float

    @classmethod
    def parse(cls, value: Any) -> "OfferRecheck":
        if not isinstance(value, dict) or set(value) != {"min_change_pct"}:
            raise ValueError("Нужен объект ровно с полем min_change_pct.")
        number = value["min_change_pct"]
        if not isinstance(number, int | float) or isinstance(number, bool) or number < 0:
            raise ValueError("min_change_pct — число процентов, не меньше нуля.")
        return cls(float(number))


@dataclass(frozen=True)
class UploadPriceCap:
    """Цена из файла выше `eur` евро похожа на ошибку — `UPLOAD_PRICE_CAP` (ADR-051).

    Такая цена не записывается, строка попадает в сводку до записи: в файле
    размещений бывают суммы в тысячу раз больше обычной — потерялся
    десятичный разделитель (`241 258` вместо €241,26).
    """

    eur: float

    @classmethod
    def parse(cls, value: Any) -> "UploadPriceCap":
        if not isinstance(value, dict) or set(value) != {"eur"}:
            raise ValueError("Нужен объект ровно с полем eur.")
        number = value["eur"]
        if not isinstance(number, int | float) or isinstance(number, bool) or number <= 0:
            raise ValueError("eur — сумма в евро, больше нуля.")
        return cls(float(number))

    @property
    def eur_cents(self) -> int:
        return round(self.eur * 100)


# Настройки с проверкой формы значения: ошибку видно в админке при вводе,
# а не в упавшей ночью задаче.
SETTING_PARSERS: dict[str, Callable[[Any], object]] = {
    "INDEXATION_SCHEDULE": IndexationSchedule.parse,
    "OFFER_RECHECK": OfferRecheck.parse,
    "UPLOAD_PRICE_CAP": UploadPriceCap.parse,
}


def validate_setting(key: str, value: Any) -> None:
    """ValueError, если значение не подходит настройке; без проверки формы — ничего."""
    parser = SETTING_PARSERS.get(key)
    if parser is not None:
        parser(value)


def indexation_schedule(product_id: int | None) -> IndexationSchedule:
    """Действующие сроки проверки индексации для продукта."""
    value = get_setting("INDEXATION_SCHEDULE", product_id)
    if value is None:
        # Общее значение заводит миграция content.0003 — его стёрли руками.
        raise ImproperlyConfigured("Нет настройки INDEXATION_SCHEDULE — заведите общее значение.")
    try:
        return IndexationSchedule.parse(value)
    except ValueError as error:
        raise ImproperlyConfigured(f"INDEXATION_SCHEDULE: {error}") from error


def offer_recheck() -> OfferRecheck:
    """Действующий порог повторного разбора: общее значение, загрузки — не под продукт."""
    value = get_setting("OFFER_RECHECK", None)
    if value is None:
        # Общее значение заводит миграция content.0004 — его стёрли руками.
        raise ImproperlyConfigured("Нет настройки OFFER_RECHECK — заведите общее значение.")
    try:
        return OfferRecheck.parse(value)
    except ValueError as error:
        raise ImproperlyConfigured(f"OFFER_RECHECK: {error}") from error


def upload_price_cap() -> UploadPriceCap:
    """Действующий порог «цена похожа на ошибку»: общее значение, загрузки — не под продукт."""
    value = get_setting("UPLOAD_PRICE_CAP", None)
    if value is None:
        # Общее значение заводит миграция content.0005 — его стёрли руками.
        raise ImproperlyConfigured("Нет настройки UPLOAD_PRICE_CAP — заведите общее значение.")
    try:
        return UploadPriceCap.parse(value)
    except ValueError as error:
        raise ImproperlyConfigured(f"UPLOAD_PRICE_CAP: {error}") from error


def set_product_setting(product_id: int, key: str, value: Any | None) -> None:
    """Задаёт локальное значение продукта; `None` — удаляет его.

    Без локального значения продукт снова работает по общему (ADR-035):
    настройка — конфигурация, не история, поэтому строка удаляется.
    Значение не изменилось — строка не трогается, `updated_at` прежний.
    """
    if key not in SETTING_KEYS:
        raise ValueError(f"Неизвестная настройка {key!r}")
    if value is not None:
        validate_setting(key, value)
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
