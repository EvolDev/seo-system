"""Блок 2 модели данных: размещения и ссылки в них.

Как модели повторяют `schema.sql` — ADR-029. Размещение всегда привязано
к продукту: размещение Clideo — такая же строка, как Convertio (ADR-007).
Размещения не удаляются: отмена — статусом «Отменено».

`run_id` размещения по умолчанию берётся из текущей цепочки, как у
журнала наблюдаемости (ADR-031): размещение, созданное импортом или
задачей, получает его само, заведённое руками — пусто.
"""

import datetime as dt
from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction

from apps.keywords.models import AnchorType
from apps.sites import statuses

# Формат размещения — колонка «Тип ссылки» Excel; общий с ценами площадки (ADR-043).
from apps.sites.models import PlacementType, SiteStatus, StatusSource
from config.changes import stamped
from config.db import PgCurrentDate, PgEnumField, PgNow
from config.run_id import current_run_id


class PlacementStatus(models.TextChoices):
    PLANNED = "planned", "Запланировано"
    ORDERED = "ordered", "Заявка отправлена"
    WRITING = "writing", "Пишется"
    REVIEW = "review", "На проверке"
    PUBLISHED = "published", "Опубликовано"
    REJECTED = "rejected", "Отклонено"
    CANCELLED = "cancelled", "Отменено"


# Какой статус площадки у продукта ставит статус размещения (ADR-047): заявка в
# работе — «Заявка отправлена», публикация — «Размещались». Остальные не ставят.
SITE_STATUS_BY_PLACEMENT: dict[str, SiteStatus] = {
    PlacementStatus.ORDERED: SiteStatus.ORDERED,
    PlacementStatus.WRITING: SiteStatus.ORDERED,
    PlacementStatus.REVIEW: SiteStatus.ORDERED,
    PlacementStatus.PUBLISHED: SiteStatus.PLACED,
}
# Поля, от которых зависит статус площадки; в update_fields — имя или колонка.
_SITE_STATUS_FIELDS = frozenset({"status", "site", "site_id", "product", "product_id"})


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
    # «Прочие данные» строки файла размещений: колонки без своего поля,
    # заголовок → значение (E1-09). Из файла ничего не теряется (ADR-041).
    extra = models.JSONField("прочие данные из файла", null=True, blank=True)
    # Через кого куплено и кто из сотрудников вёл (ADR-041). Сотрудник —
    # пользователь системы, можно без права входа.
    seller = models.ForeignKey(
        "sites.Seller",
        models.PROTECT,
        verbose_name="продавец",
        related_name="placements",
        null=True,
        blank=True,
        db_index=False,
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.PROTECT,
        verbose_name="сотрудник",
        related_name="placements",
        null=True,
        blank=True,
        db_index=False,
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

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Сохранить; сменился статус размещения — подвинуть статус площадки.

        Площадка у продукта идёт за размещением только вперёд
        (`apps.sites.statuses`). «Сменился» — против строки в базе до записи:
        правка комментария или проверка индексации статус площадки не трогают,
        и статус, поставленный человеком руками, остаётся.
        """
        update_fields = kwargs.get("update_fields")
        watched = update_fields is None or not _SITE_STATUS_FIELDS.isdisjoint(update_fields)
        before = None
        if watched and not self._state.adding:
            before = (
                Placement.objects.filter(pk=self.pk)
                .values_list("status", "site_id", "product_id")
                .first()
            )
        # atomic — размещение и статус площадки записываются вместе или никак.
        # Смену статуса размещения запишет триггер, кто и откуда — отметка (ADR-049).
        with transaction.atomic():
            with stamped():
                super().save(*args, **kwargs)
            target = SITE_STATUS_BY_PLACEMENT.get(self.status)
            moved = before != (self.status, self.site_id, self.product_id)
            if watched and target is not None and moved:
                statuses.advance(self.site_id, self.product_id, target, placement_id=self.pk)


class PlacementStatusChange(models.Model):
    """Смена статуса размещения — строка истории (ADR-049).

    Пишет только триггер `placements_status_*` в базе; кто и откуда —
    отметка кода (`config.changes.stamped`). `from_status` пусто —
    размещение создано.
    """

    placement = models.ForeignKey(
        Placement,
        models.PROTECT,
        verbose_name="размещение",
        related_name="status_changes",
        db_index=False,
    )
    from_status = PgEnumField(
        "был",
        enum_type="placement_status",
        choices=PlacementStatus.choices,
        null=True,
        blank=True,
    )
    to_status = PgEnumField("стал", enum_type="placement_status", choices=PlacementStatus.choices)
    source = PgEnumField(
        "откуда", enum_type="status_source", choices=StatusSource.choices, null=True, blank=True
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.PROTECT,
        verbose_name="кто",
        related_name="+",
        null=True,
        blank=True,
        db_index=False,
    )
    run_id = models.UUIDField("run_id", null=True, blank=True)
    changed_at = models.DateTimeField("когда", db_default=PgNow())

    class Meta:
        db_table = "placement_status_changes"
        verbose_name = "смена статуса размещения"
        verbose_name_plural = "история статусов размещений"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["placement", "-changed_at"], name="idx_placement_status_changes"),
        ]

    def __str__(self) -> str:
        return f"{self.placement_id}: {self.from_status} → {self.to_status}"


class InvoiceStatus(models.TextChoices):
    ISSUED = "issued", "Выставлен"
    PAID = "paid", "Оплачен"
    CANCELLED = "cancelled", "Отменён"


class Invoice(models.Model):
    """Счёт продавца: одно размещение или пачка (ADR-055).

    Сделка напрямую — заявки с продавцом, деньги по ним — счёт. Что он
    закрывает и за сколько — строки `InvoiceItem`. «Заплачено» размещения
    пересчитывает `apps.placements.invoices` при записи счёта. Отменённый счёт
    не считается. Счета не удаляются.
    """

    seller = models.ForeignKey(
        "sites.Seller",
        models.PROTECT,
        verbose_name="продавец",
        related_name="invoices",
        db_index=False,
    )
    number = models.TextField(
        "номер счёта", null=True, blank=True, help_text="Как у продавца, если есть."
    )
    amount_cents = models.IntegerField("сумма, центы")
    currency = models.CharField("валюта", max_length=3, default="EUR", db_default="EUR")
    pay_url = models.TextField(
        "ссылка на оплату",
        null=True,
        blank=True,
        help_text="Счёт PayPal или другая страница оплаты.",
    )
    issued_on = models.DateField("выставлен", db_default=PgCurrentDate())
    status = PgEnumField(
        "статус",
        enum_type="invoice_status",
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.ISSUED,
        db_default=InvoiceStatus.ISSUED,
    )
    paid_on = models.DateField("оплачен", null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.PROTECT,
        verbose_name="оплатил",
        related_name="+",
        null=True,
        blank=True,
        db_index=False,
    )
    comment = models.TextField("комментарий", null=True, blank=True)
    created_at = models.DateTimeField("заведён", db_default=PgNow())
    updated_at = models.DateTimeField("изменён", auto_now=True, db_default=PgNow())

    class Meta:
        db_table = "invoices"
        verbose_name = "счёт"
        verbose_name_plural = "счета"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gt=0),
                name="invoices_amount_cents_check",
                violation_error_message="Сумма счёта — больше нуля.",
            ),
            models.UniqueConstraint(
                fields=["pay_url"],
                condition=models.Q(pay_url__isnull=False),
                name="invoices_pay_url_key",
                violation_error_message="Счёт с этой ссылкой на оплату уже есть.",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["seller", "status"], name="idx_invoices_seller"),
        ]

    def __str__(self) -> str:
        return f"Счёт {self.number or self.pk or 'новый'} · {self.seller}"


class InvoiceItem(models.Model):
    """Строка счёта: размещение и его доля в валюте счёта (ADR-055).

    Сумма долей — сумма счёта, это проверяет форма. Строку убирают из счёта,
    пока он не оплачен.
    """

    invoice = models.ForeignKey(
        Invoice, models.PROTECT, verbose_name="счёт", related_name="items", db_index=False
    )
    placement = models.ForeignKey(
        Placement,
        models.PROTECT,
        verbose_name="размещение",
        related_name="invoice_items",
        db_index=False,
    )
    amount_cents = models.IntegerField("доля, центы")

    class Meta:
        db_table = "invoice_items"
        verbose_name = "строка счёта"
        verbose_name_plural = "строки счёта"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gte=0),
                name="invoice_items_amount_cents_check",
                violation_error_message="Доля не может быть меньше нуля.",
            ),
            models.UniqueConstraint(
                fields=["invoice", "placement"],
                name="invoice_items_invoice_id_placement_id_key",
                violation_error_message="Это размещение уже в счёте.",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["placement"], name="idx_invoice_items_placement"),
        ]

    def __str__(self) -> str:
        return f"{self.invoice_id}: {self.placement_id}"


class PlacementLink(models.Model):
    """Одна ссылка внутри статьи размещения.

    Задание — анкор из списка анкоров продукта (`keyword`) и куда ведёт; текст
    и тип анкора берутся из него, номер ставится сам (E3-05, ADR-059). Без
    анкора из списка — только старые ссылки из таблицы («Read more»). Анкор —
    того же продукта, что и размещение, иначе ссылка засчиталась бы в покрытие
    чужого анкора.

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
                {"keyword": "Анкор другого продукта — выберите анкор продукта этого размещения."}
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
