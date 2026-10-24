"""Блок 3 модели данных: ключи продукта и их позиции в выдаче.

Как модели повторяют `schema.sql` — ADR-029. Ключ всегда привязан к
продукту (ADR-007, ADR-030). Сколько ссылок размещено и ждёт под ключ,
не хранится — считается из ссылок размещений (`v_keyword_coverage`, E1-05).

Анкоры продукта (E3-05, ADR-059): ключ — анкор под позицию в выдаче,
безанкорный анкор (бренд, адрес, нейтральное слово) — строка той же таблицы со
своим типом и долей внутри безанкорки. Целевые доли по типам страниц и
странам — `page_type_shares` и `country_shares`, общие для продукта.

Анкоры продукта живут списками по регионам (ADR-075): у США и Бразилии свои
анкоры, адреса и позиции, хотя тексты бывают одни и те же («convert»).
"""

from typing import Any, ClassVar

from django.db import models

from apps.sites import countries
from apps.sites.models import MetricSource, Product
from config.db import PgCurrentDate, PgEnumField, PgNow

# Регион первого списка продукта, если его заводит не загрузка: анкоры до
# регионов — это США (пользователь, 24.10.2026).
DEFAULT_REGION = "us"


class AnchorType(models.TextChoices):
    """Тип анкора. Тип `anchor_type` создаёт миграция `keywords.0001`, им
    пользуются и ключи, и ссылки размещений."""

    EXACT = "exact", "Прямой"
    DILUTED = "diluted", "Разбавленный"
    BRANDED = "branded", "Бренд"
    URL = "url", "Голый адрес"
    GENERIC = "generic", "Нейтральное слово"


class AnchorList(models.Model):
    """Список анкоров продукта для региона (ADR-075).

    Регион — страна выдачи: позиции анкоров списка снимаются по ней, а в
    карточке размещения список выбирается по языку площадки. Списки заводит
    загрузка файла анкоров с новым регионом. Первый список продукта — основной:
    в него попадают анкоры без указанного списка и листы безанкорки без пометки
    региона.
    """

    product = models.ForeignKey(
        Product,
        models.PROTECT,
        verbose_name="продукт",
        related_name="anchor_lists",
        db_index=False,
    )
    # Страна строчными, как в справочнике стран (`apps.sites.countries`): us, br.
    country = models.CharField("регион", max_length=2)
    created_at = models.DateTimeField("заведён", db_default=PgNow())

    class Meta:
        db_table = "anchor_lists"
        verbose_name = "список анкоров"
        verbose_name_plural = "списки анкоров"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["product", "country"],
                name="anchor_lists_product_id_country_key",
                violation_error_message="Список анкоров этого региона у продукта уже есть.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product} · {self.region_name}"

    @property
    def region_name(self) -> str:
        return countries.name(self.country)

    @property
    def flag(self) -> str | None:
        return countries.flag_code(self.country)

    @classmethod
    def of_product(cls, product_id: int) -> list["AnchorList"]:
        """Списки продукта: основной первым, дальше — по порядку заведения."""
        return list(cls.objects.filter(product_id=product_id).order_by("created_at", "pk"))

    @classmethod
    def main_of(cls, product_id: int) -> "AnchorList | None":
        """Основной список продукта — заведённый первым; списков нет — None."""
        return cls.objects.filter(product_id=product_id).order_by("created_at", "pk").first()

    @classmethod
    def ensure_main(cls, product_id: int) -> "AnchorList":
        """Основной список продукта; нет списков — заводит его: США (`DEFAULT_REGION`)."""
        found = cls.main_of(product_id)
        if found is None:
            found = cls.objects.create(product_id=product_id, country=DEFAULT_REGION)
        return found


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
    # Продукт — тот же, что у списка: его оставили ради фильтров и запросов по
    # продукту, ставит его `save` из списка.
    anchor_list = models.ForeignKey(
        AnchorList,
        models.PROTECT,
        verbose_name="список анкоров",
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
    # Доля внутри своей группы, %: у безанкорного — из листа «Распределение
    # безанкорки» (Convertio — 56,86). У ключа пусто: его доля — от типа страницы.
    share = models.DecimalField("доля, %", max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "keywords"
        verbose_name = "ключ"
        verbose_name_plural = "ключи"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["anchor_list", "keyword"],
                name="keywords_anchor_list_id_keyword_key",
                violation_error_message="Такой анкор в этом списке уже есть.",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tool"], condition=models.Q(is_active=True), name="idx_keywords_tool"
            ),
        ]

    def __str__(self) -> str:
        return self.keyword

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Список не указан — основной список продукта: так анкор заводят импорт
        # таблицы, форма ключа и всё, что было до регионов (ADR-075).
        if self.anchor_list_id is None:
            self.anchor_list = AnchorList.ensure_main(self.product_id)
        elif self.product_id is None:
            self.product_id = self.anchor_list.product_id
        super().save(*args, **kwargs)

    @property
    def is_naked(self) -> bool:
        """Безанкорный: бренд, голый адрес, нейтральное слово."""
        return self.anchor_type in NAKED_TYPES


# Безанкорные типы (глоссарий: «безанкорка»).
NAKED_TYPES = (AnchorType.BRANDED, AnchorType.URL, AnchorType.GENERIC)


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


class PageTypeShare(models.Model):
    """Целевая доля типа страниц продукта — лист «Распределение по типам страниц».

    `target_pct` — доля ссылок на страницы этого типа среди всех ссылок
    продукта; `exact_pct`, `diluted_pct`, `naked_pct` — как делить ссылки
    внутри типа: прямой, разбавленный, безанкор. Проценты, 15 — это 15%.
    Это настройка, а не замер: правится на странице «Анкоры» (ADR-059).
    """

    product = models.ForeignKey(
        Product, models.PROTECT, verbose_name="продукт", related_name="+", db_index=False
    )
    page_type = models.TextField("тип страницы")
    target_pct = models.DecimalField(
        "целевая доля, %", max_digits=5, decimal_places=2, null=True, blank=True
    )
    exact_pct = models.DecimalField(
        "прямой, %", max_digits=5, decimal_places=2, null=True, blank=True
    )
    diluted_pct = models.DecimalField(
        "разбавленный, %", max_digits=5, decimal_places=2, null=True, blank=True
    )
    naked_pct = models.DecimalField(
        "безанкор, %", max_digits=5, decimal_places=2, null=True, blank=True
    )
    position = models.SmallIntegerField("порядок", default=0, db_default=0)
    updated_at = models.DateTimeField("изменено", db_default=PgNow())

    class Meta:
        db_table = "page_type_shares"
        verbose_name = "доля типа страниц"
        verbose_name_plural = "доли типов страниц"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["product", "page_type"],
                name="page_type_shares_product_id_page_type_key",
                violation_error_message="Такой тип страниц у продукта уже есть.",
            ),
        ]

    def __str__(self) -> str:
        return self.page_type


class CountryShare(models.Model):
    """Целевая доля размещений по стране — лист «Распределение по странам».

    `country` пусто — «остальные страны».
    """

    product = models.ForeignKey(
        Product, models.PROTECT, verbose_name="продукт", related_name="+", db_index=False
    )
    country = models.CharField("страна", max_length=2, null=True, blank=True)
    target_pct = models.DecimalField("целевая доля, %", max_digits=5, decimal_places=2)
    updated_at = models.DateTimeField("изменено", db_default=PgNow())

    class Meta:
        db_table = "country_shares"
        verbose_name = "доля страны"
        verbose_name_plural = "доли стран"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # «Остальные» (пусто) — тоже одна строка на продукт.
            models.UniqueConstraint(
                fields=["product", "country"],
                name="country_shares_product_id_country_key",
                nulls_distinct=False,
                violation_error_message="Эта страна у продукта уже есть.",
            ),
        ]

    def __str__(self) -> str:
        return self.country or "остальные"


class KeywordCoverage(models.Model):
    """Анкор продукта с покрытием — строка `v_keyword_coverage` (E1-05, E3-05).

    Только чтение: представление. Размещено и ждут — по ссылкам размещений,
    позиция — последний снимок по стране списка (ADR-075). Экран «Анкоры» —
    список этих строк.
    """

    product = models.ForeignKey(
        Product, models.DO_NOTHING, verbose_name="продукт", related_name="+", db_constraint=False
    )
    anchor_list = models.ForeignKey(
        AnchorList,
        models.DO_NOTHING,
        verbose_name="список анкоров",
        related_name="+",
        db_constraint=False,
    )
    keyword = models.TextField("анкор")
    tool = models.TextField("раздел сайта", null=True)
    volume = models.IntegerField("объём", null=True)
    target_url = models.TextField("куда ведёт")
    last_position = models.SmallIntegerField("позиция", null=True)
    links_placed = models.BigIntegerField("размещено")
    links_waiting = models.BigIntegerField("ждут")
    global_volume = models.IntegerField("глобальный объём", null=True)
    page_type = models.TextField("тип страницы", null=True)
    anchor_type = models.TextField("тип анкора", null=True)
    is_active = models.BooleanField("активен", default=True)
    share = models.DecimalField("доля, %", max_digits=5, decimal_places=2, null=True)

    class Meta:
        managed = False
        db_table = "v_keyword_coverage"
        verbose_name = "анкор"
        verbose_name_plural = "анкоры"

    def __str__(self) -> str:
        return self.keyword
