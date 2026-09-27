"""Блок 3 модели данных: ключи продукта и их позиции в выдаче.

Как модели повторяют `schema.sql` — ADR-029. Ключ всегда привязан к
продукту (ADR-007, ADR-030). Сколько ссылок размещено и ждёт под ключ,
не хранится — считается из ссылок размещений (`v_keyword_coverage`, E1-05).
"""

from typing import ClassVar

from django.db import models

from apps.sites.models import MetricSource
from config.db import PgCurrentDate, PgEnumField


class AnchorType(models.TextChoices):
    """Тип анкора. Тип `anchor_type` создаёт миграция `keywords.0001`, им
    пользуются и ключи, и ссылки размещений."""

    EXACT = "exact", "Прямой"
    DILUTED = "diluted", "Разбавленный"
    BRANDED = "branded", "Бренд"
    URL = "url", "Голый адрес"
    GENERIC = "generic", "Нейтральное слово"


class Keyword(models.Model):
    """Ключ продукта: под него ставятся анкоры и снимаются позиции.

    `tool` — раздел сайта продукта, свободный текст: разделы у каждого
    продукта свои, список — локальная настройка `TOOL_CATEGORIES`.
    `page_type` — тип целевой страницы (колонка Type), не тип анкора.
    Ключ не удаляется — выключается снятием «активен».
    """

    product = models.ForeignKey(
        "sites.Product",
        models.PROTECT,
        verbose_name="продукт",
        related_name="keywords",
        db_index=False,
    )
    keyword = models.TextField("ключ")
    target_url = models.TextField("целевая страница")
    volume = models.IntegerField("объём", null=True, blank=True)
    global_volume = models.IntegerField("глобальный объём", null=True, blank=True)
    tool = models.TextField("раздел сайта", null=True, blank=True)
    page_type = models.TextField("тип страницы", null=True, blank=True)
    anchor_type = PgEnumField(
        "тип анкора",
        enum_type="anchor_type",
        choices=AnchorType.choices,
        null=True,
        blank=True,
    )
    is_active = models.BooleanField("активен", default=True, db_default=True)

    class Meta:
        db_table = "keywords"
        verbose_name = "ключ"
        verbose_name_plural = "ключи"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["product", "keyword"],
                name="keywords_product_id_keyword_key",
                violation_error_message="Такой ключ у этого продукта уже есть.",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tool"], condition=models.Q(is_active=True), name="idx_keywords_tool"
            ),
        ]

    def __str__(self) -> str:
        return self.keyword


class KeywordPosition(models.Model):
    """Снапшот позиции ключа в выдаче (ADR-008).

    Одна позиция на ключ, страну и дату: повторная запись за ту же дату
    отклоняется базой. `position` пусто — ключ вне топ-100.
    """

    keyword = models.ForeignKey(
        Keyword, models.PROTECT, verbose_name="ключ", related_name="positions", db_index=False
    )
    position = models.SmallIntegerField(
        "позиция", null=True, blank=True, help_text="Пусто — вне топ-100."
    )
    country = models.CharField("страна", max_length=2, default="US", db_default="US")
    source = PgEnumField(
        "источник",
        enum_type="metric_source",
        choices=MetricSource.choices,
        default=MetricSource.AHREFS_API,
        db_default=MetricSource.AHREFS_API,
    )
    checked_at = models.DateField("дата", db_default=PgCurrentDate())

    class Meta:
        db_table = "keyword_positions"
        verbose_name = "позиция ключа"
        verbose_name_plural = "позиции ключей"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["keyword", "country", "checked_at"],
                name="keyword_positions_keyword_id_country_checked_at_key",
                violation_error_message="Позиция этого ключа для этой страны за эту дату уже есть.",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["keyword", "-checked_at"], name="idx_positions_kw"),
        ]

    def __str__(self) -> str:
        return f"{self.keyword} · {self.country} · {self.checked_at:%d.%m.%Y}"
