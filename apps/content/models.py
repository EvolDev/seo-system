"""Блоки 4–5 модели данных: настройки и промпты.

Пока только `prompt_templates` и `prompt_variants` — на них ссылается
`llm_calls` (ADR-031), и `domain_settings` — из неё читает порог написания
`v_product_site_latest` (E1-05). Статьи, пул знаний и правила появятся в
своих задачах. Как модели повторяют `schema.sql` — ADR-029.
"""

from typing import ClassVar

from django.db import models

from config.db import PgNow


class PromptTemplate(models.Model):
    """Задача и имя промпта, без текста.

    `product` пусто — общий шаблон, заполнен — локальный для продукта;
    локальный перекрывает общий той же задачи (ADR-030).
    """

    product = models.ForeignKey(
        "sites.Product",
        models.PROTECT,
        verbose_name="продукт",
        related_name="prompt_templates",
        null=True,
        blank=True,
        db_index=False,
    )
    task = models.TextField("задача")
    name = models.TextField("название")
    is_active = models.BooleanField("активен", default=True, db_default=True)
    created_at = models.DateTimeField("создан", db_default=PgNow())

    class Meta:
        db_table = "prompt_templates"
        verbose_name = "шаблон промпта"
        verbose_name_plural = "шаблоны промптов"

    def __str__(self) -> str:
        return f"{self.task} · {self.name}"


class PromptVariant(models.Model):
    """Конкретная формулировка промпта. Доля принятых — из двух счётчиков."""

    template = models.ForeignKey(
        PromptTemplate,
        models.PROTECT,
        verbose_name="шаблон",
        related_name="variants",
        db_index=False,
    )
    label = models.TextField("метка")
    body = models.TextField("текст промпта")
    times_used = models.IntegerField("использован, раз", default=0, db_default=0)
    times_accepted = models.IntegerField("принят, раз", default=0, db_default=0)
    is_active = models.BooleanField("активен", default=True, db_default=True)
    created_at = models.DateTimeField("создан", db_default=PgNow())

    class Meta:
        db_table = "prompt_variants"
        verbose_name = "вариант промпта"
        verbose_name_plural = "варианты промптов"

    def __str__(self) -> str:
        return f"{self.template} · {self.label}"


class DomainSetting(models.Model):
    """Порог, ориентир или список без критичности: ключ → значение в JSONB.

    `product` пусто — общее значение, заполнен — локальное для продукта;
    локальное перекрывает общее с тем же ключом (ADR-030). Какое значение
    действует для продукта — `apps.content.domain_settings.get_setting`.
    Какие ключи бывают — там же, `SETTING_KEYS`.
    """

    key = models.TextField("ключ")
    product = models.ForeignKey(
        "sites.Product",
        models.PROTECT,
        verbose_name="продукт",
        related_name="domain_settings",
        null=True,
        blank=True,
        db_index=False,
    )
    value = models.JSONField("значение")
    description = models.TextField("описание", null=True, blank=True)
    updated_at = models.DateTimeField("изменена", auto_now=True, db_default=PgNow())

    class Meta:
        db_table = "domain_settings"
        verbose_name = "настройка"
        verbose_name_plural = "настройки"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # NULLS NOT DISTINCT: двух общих значений одного ключа тоже быть не может.
            models.UniqueConstraint(
                fields=["key", "product"],
                name="domain_settings_key_product_id_key",
                nulls_distinct=False,
                violation_error_message="Эта настройка у этого продукта уже есть.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key} · {self.product or 'общее'}"
