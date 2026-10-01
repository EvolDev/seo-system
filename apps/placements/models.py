"""Блок 2 модели данных: размещения и ссылки в них.

Как модели повторяют `schema.sql` — ADR-029. Размещение всегда привязано
к продукту: размещение Clideo — такая же строка, как Convertio (ADR-007).
Размещения не удаляются: отмена — статусом «Отменено».

`run_id` размещения по умолчанию берётся из текущей цепочки, как у
журнала наблюдаемости (ADR-031): размещение, созданное импортом или
задачей, получает его само, заведённое руками — пусто.
"""

import datetime as dt
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models

from apps.keywords.models import AnchorType
from config.db import PgEnumField, PgNow
from config.run_id import current_run_id


class PlacementStatus(models.TextChoices):
    PLANNED = "planned", "Запланировано"
    ORDERED = "ordered", "Заявка отправлена"
    WRITING = "writing", "Пишется"
    REVIEW = "review", "На проверке"
    PUBLISHED = "published", "Опубликовано"
    REJECTED = "rejected", "Отклонено"
    CANCELLED = "cancelled", "Отменено"


class PlacementType(models.TextChoices):
    """Формат размещения — колонка «Тип ссылки» Excel. Не dofollow/nofollow."""

    GUEST_POST = "guest_post", "Guest Post"
    LINK_INSERTION = "link_insertion", "Link Insertion"


class Placement(models.Model):
    """Статья с нашими ссылками на площадке, для одного продукта.

    `article_url` хранится как есть и может быть на поддомене площадки
    (площадка `example.com`, статья `blog.example.com/…`): совпадения
    хоста статьи с доменом площадки не требуем. Итоговая цена до
    размещения не хранится (ADR-009), после — `price_paid_cents`.
    """

    site = models.ForeignKey(
        "sites.Site",
        models.PROTECT,
        verbose_name="площадка",
        related_name="placements",
        db_index=False,
    )
    product = models.ForeignKey(
        "sites.Product",
        models.PROTECT,
        verbose_name="продукт",
        related_name="placements",
        db_index=False,
    )
    article_url = models.TextField("адрес статьи", null=True, blank=True)
    status = PgEnumField(
        "статус",
        enum_type="placement_status",
        choices=PlacementStatus.choices,
        default=PlacementStatus.PLANNED,
        db_default=PlacementStatus.PLANNED,
    )
    collaborator_order_id = models.TextField("номер заявки", null=True, blank=True)
    placement_type = PgEnumField(
        "тип размещения",
        enum_type="placement_type",
        choices=PlacementType.choices,
        null=True,
        blank=True,
    )
    ad_label_requested = models.BooleanField(
        "пометку «реклама» заказали мы",
        default=False,
        db_default=False,
        help_text="Тогда пометка «реклама» в статье — не нарушение.",
    )
    ordered_at = models.DateTimeField("заявка отправлена", null=True, blank=True)
    published_at = models.DateTimeField("опубликовано", null=True, blank=True)
    price_paid_cents = models.IntegerField("заплачено, центы", null=True, blank=True)
    currency = models.CharField(
        "валюта", max_length=3, null=True, blank=True, default="EUR", db_default="EUR"
    )
    is_indexed = models.BooleanField(
        "в индексе", null=True, blank=True, help_text="Пусто — не проверялось."
    )
    indexed_checked_at = models.DateTimeField("индексация проверена", null=True, blank=True)
    skip_checks = models.BooleanField(
        "не проверять",
        default=False,
        db_default=False,
        help_text=(
            "Статья больше не нужна: система не проверяет её по расписанию."
            " Проверить вручную можно и так."
        ),
    )
    announce_on_homepage = models.BooleanField("анонс на главной", null=True, blank=True)
    clicks_from_homepage = models.SmallIntegerField("кликов от главной", null=True, blank=True)
    comment = models.TextField(
        "комментарий", null=True, blank=True, help_text="Что не так с полученной статьёй."
    )
    run_id = models.UUIDField("run_id", null=True, blank=True, default=current_run_id)
    created_at = models.DateTimeField("создано", db_default=PgNow())
    updated_at = models.DateTimeField("изменено", auto_now=True, db_default=PgNow())

    class Meta:
        db_table = "placements"
        verbose_name = "размещение"
        verbose_name_plural = "размещения"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["site", "product"], name="idx_placements_site"),
            models.Index(fields=["status"], name="idx_placements_status"),
            models.Index(fields=["-published_at"], name="idx_placements_pub"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.product} · {self.get_status_display()}"


class PlacementLink(models.Model):
    """Одна ссылка внутри статьи размещения.

    Задание — анкор, куда ведёт, ключ, тип анкора, номер ссылки. Ключа
    нет у безанкорных и брендовых ссылок. Ключ — того же продукта, что и
    размещение, иначе ссылка засчиталась бы в покрытие чужого ключа.

    Остальное пишет проверка страницы (E2-04, E2-05): `rel`, позиция,
    предложение вокруг ссылки, живость. Время пропажи `lost_at` пишется
    один раз — только через `mark_lost`.

    Позиция ссылки в статье (ADR-032) — как «Статистика» Word: сколько
    знаков стоит перед первым символом анкора, если текст статьи с самого
    начала вставить в Word.

    - Текст статьи — с самого начала, заголовок входит. Меню, подвал и
      сайдбар не входят: текст статьи на странице выделяет одна функция
      для краулера и проверок статьи (E2-04, E7-01).
    - Теги HTML и разметка Markdown (`**`, `#`, `[…](…)`) не считаются.
    - Границы абзацев, заголовков, пунктов списка и переносы строк не
      считаются — Word не считает знаки абзаца. Маркеры и номера списков
      тоже.
    - Пробелы между словами — как их показывает браузер: несколько
      подряд в исходнике страницы — один пробел.
    - `char_offset` — знаков с пробелами (неразрывный пробел — тоже
      пробел). По нему правила `LINK_POSITION_FIRST` и
      `LINK_POSITION_SECOND`. `char_offset_no_spaces` — знаков без
      пробелов, для сверки с Word.

    Пример: заголовок «How to convert video», абзац «Converting is easy.»,
    абзац «Use mp4 to mp3 …» с анкором «mp4 to mp3» — `char_offset` 43,
    `char_offset_no_spaces` 37.
    """

    placement = models.ForeignKey(
        Placement, models.PROTECT, verbose_name="размещение", related_name="links", db_index=False
    )
    keyword = models.ForeignKey(
        "keywords.Keyword",
        models.PROTECT,
        verbose_name="ключ",
        related_name="links",
        null=True,
        blank=True,
        db_index=False,
        help_text="Пусто — безанкорная или брендовая ссылка.",
    )
    anchor = models.TextField("анкор")
    target_url = models.TextField("куда ведёт")
    anchor_type = PgEnumField(
        "тип анкора",
        enum_type="anchor_type",
        choices=AnchorType.choices,
        null=True,
        blank=True,
    )
    rel = models.TextField(
        "rel", null=True, blank=True, help_text="Атрибут на странице; пусто — нет, dofollow."
    )
    char_offset = models.IntegerField("знаков до ссылки, с пробелами", null=True, blank=True)
    char_offset_no_spaces = models.IntegerField(
        "знаков до ссылки, без пробелов", null=True, blank=True
    )
    context_sentence = models.TextField("предложение со ссылкой", null=True, blank=True)
    link_index = models.SmallIntegerField("номер ссылки", null=True, blank=True)
    extraction_test_passed = models.BooleanField(
        "тест на извлечение пройден", null=True, blank=True
    )
    is_alive = models.BooleanField("ссылка на месте", null=True, blank=True)
    last_checked_at = models.DateTimeField("проверена", null=True, blank=True)
    first_seen_at = models.DateTimeField("появилась", null=True, blank=True)
    lost_at = models.DateTimeField("пропала", null=True, blank=True)

    class Meta:
        db_table = "placement_links"
        verbose_name = "ссылка"
        verbose_name_plural = "ссылки"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["placement"], name="idx_links_placement"),
            models.Index(fields=["keyword"], name="idx_links_keyword"),
            models.Index(fields=["is_alive", "last_checked_at"], name="idx_links_health"),
        ]

    def __str__(self) -> str:
        return f"{self.anchor} → {self.target_url}"

    def clean(self) -> None:
        try:
            placement = self.placement
        except Placement.DoesNotExist:
            return
        keyword = self.keyword
        if (
            keyword is not None
            and placement.product_id is not None
            and keyword.product_id != placement.product_id
        ):
            raise ValidationError(
                {"keyword": "Ключ другого продукта — выберите ключ продукта этого размещения."}
            )

    def mark_lost(self, when: dt.datetime) -> bool:
        """Записывает время пропажи ссылки, если его ещё нет. Возвращает, записано ли.

        Пропажа пишется один раз — при первом подтверждении. Повторные
        проверки время не перезаписывают; вернувшаяся ссылка его не
        стирает — площадка её удаляла. Один `UPDATE … WHERE lost_at IS
        NULL`, поэтому и две одновременные проверки не перезапишут
        первое время.
        """
        updated = PlacementLink.objects.filter(pk=self.pk, lost_at__isnull=True).update(
            lost_at=when
        )
        self.refresh_from_db(fields=["lost_at"])
        return updated == 1
