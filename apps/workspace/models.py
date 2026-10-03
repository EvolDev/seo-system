"""Рабочее место пользователя — блок 7 модели данных (ADR-050)."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from config.db import PgNow


class SavedFilter(models.Model):
    """Набор фильтров списка админки: свой у каждого пользователя.

    `screen` — список, «приложение.модель» (`sites.productsitelatest`);
    `query` — строка адреса списка без номера страницы: фильтры, поиск,
    регион, сортировка. Удалённый набор — с пометкой `deleted_at`, а не
    стёрт: «Отменить» его возвращает.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.PROTECT,
        verbose_name="пользователь",
        related_name="saved_filters",
        db_index=False,
    )
    screen = models.TextField("список")
    name = models.TextField("название")
    query = models.TextField("фильтры")
    created_at = models.DateTimeField("создан", db_default=PgNow())
    deleted_at = models.DateTimeField("удалён", null=True, blank=True)

    class Meta:
        db_table = "saved_filters"
        verbose_name = "набор фильтров"
        verbose_name_plural = "наборы фильтров"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Название одно на списке у пользователя, без учёта регистра;
            # удалённые не мешают. Индекс заодно ищет наборы списка.
            models.UniqueConstraint(
                models.F("user"),
                models.F("screen"),
                Lower("name"),
                condition=models.Q(deleted_at__isnull=True),
                name="saved_filters_name_key",
                violation_error_message="Набор с таким названием уже есть.",
            ),
        ]

    def __str__(self) -> str:
        return self.name
