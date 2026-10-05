"""Блок 1 модели данных: продукты, площадки, решения по ним, снапшоты.

Схема — `schema.sql` 1.13, как модели её повторяют — ADR-029: имена
таблиц, индексов и ограничений из схемы, значения по умолчанию в базе
(`db_default`), `on_delete=PROTECT`. Несколько продуктов — ADR-030:
площадка хранит только факты о себе, решение по ней — в `ProductSite`.
Продавцы, предложения, рабочая цена площадки, заметки, курсы — ADR-043.
Загрузки файлов продавцов и каталога, строки их разбора — ADR-044.
Выгрузки Ahrefs Batch Analysis и трафик по странам — ADR-045.
История смены статусов — ADR-049. Загрузки размещений и ссылающиеся
домены Ahrefs — ADR-051.
"""

from collections.abc import Iterable
from typing import Any, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.functions import Lower

from apps.sites.domains import normalize_domain
from config.changes import stamped
from config.db import PgEnumField, PgNow


class SiteStatus(models.TextChoices):
    """Статус площадки у продукта (ADR-047).

    Порядок — как в окне статуса и в фильтре: путь площадки в работу, потом
    отказы, последним — аудит. В типе Postgres значения в том же порядке,
    по нему сортирует колонка «статус». Как статус движется сам — `statuses`.
    """

    NEW = "new", "Новая"
    VIEWED = "viewed", "Просмотрено"
    APPROVED = "approved", "Одобрена"
    ORDERED = "ordered", "Заявка отправлена"
    PLACED = "placed", "Размещались"
    DISCARDED = "discarded", "Отбрасываю"
    DECLINED = "declined", "Отказала площадка"
    BLACKLISTED = "blacklisted", "Чёрный список"
    AUDITING = "auditing", "На аудите"


class MetricSource(models.TextChoices):
    AHREFS_API = "ahrefs_api", "Ahrefs API"
    SERP_API = "serp_api", "SERP API"
    MANUAL = "manual", "Вручную"
    CSV_IMPORT = "csv_import", "Импорт из файла"
    COLLABORATOR_API = "collaborator_api", "Collaborator API"
    AHREFS_BATCH = "ahrefs_batch", "Ahrefs Batch Analysis"


class PlacementType(models.TextChoices):
    """Услуга: формат размещения у предложения продавца и у размещения.

    Не dofollow/nofollow. Живёт здесь, а не в `placements`: её берут и
    цены площадки (ADR-043), а `placements` сам зависит от этого модуля.
    """

    GUEST_POST = "guest_post", "публикация"
    LINK_INSERTION = "link_insertion", "вставка ссылки"


class UploadKind(models.TextChoices):
    PRICE_LIST = "price_list", "Прайс продавца"
    COLLABORATOR_CATALOG = "collaborator_catalog", "Каталог Collaborator"
    AHREFS_BATCH = "ahrefs_batch", "Ahrefs Batch Analysis"
    PLACEMENTS = "placements", "Размещения продукта"
    REF_DOMAINS = "ahrefs_refdomains", "Ссылающиеся домены Ahrefs"
    ANCHORS = "anchors", "Анкоры продукта"


# Загрузки без продавца: замер Ahrefs наш, у файла размещений продавец — в
# строках, у загрузки он только «от кого», если файл от продавца (E1-09).
# Анкоры продукта (E3-05) — наши, продавца у них нет.
UPLOADS_WITHOUT_SELLER = (
    UploadKind.AHREFS_BATCH,
    UploadKind.PLACEMENTS,
    UploadKind.REF_DOMAINS,
    UploadKind.ANCHORS,
)
# Загрузки под продукт: чьи это размещения и на кого ссылаются домены.
UPLOADS_FOR_PRODUCT = (UploadKind.PLACEMENTS, UploadKind.REF_DOMAINS, UploadKind.ANCHORS)


class UploadStatus(models.TextChoices):
    NEW = "new", "Разметка колонок"
    CHECKING = "checking", "Проверяется"
    CHECKED = "checked", "Проверен, ждёт записи"
    WRITING = "writing", "Записывается"
    DONE = "done", "Записан"
    FAILED = "failed", "Ошибка"


class ReviewGroup(models.TextChoices):
    """Вкладка разбора загрузки: как предложение из файла соотносится с базой (ADR-044)."""

    CHEAPER = "cheaper", "Дешевле рабочей"
    CHANGED = "changed", "Цена изменилась"
    NEW = "new", "Новые"
    REJECTED = "rejected", "Отклоняли"
    PRICIER = "pricier", "Дороже рабочей"
    OTHER_SERVICE = "other_service", "Другая услуга"
    SAME = "same", "Без изменений"


class AuditVerdict(models.TextChoices):
    YES = "yes", "Да"
    NO = "no", "Нет"
    BORDERLINE = "borderline", "Пограничная"


class AuditAuthor(models.TextChoices):
    HUMAN = "human", "Человек"
    LLM = "llm", "LLM"
    SYSTEM = "system", "Система"


class StatusSource(models.TextChoices):
    """Откуда смена статуса площадки или размещения (ADR-049).

    Отметку ставит код (`config.changes`), строку истории пишет триггер.
    Пусто в истории — код смену не отметил.
    """

    PANEL = "panel", "панель"
    FORM = "form", "форма"
    PLACEMENT = "placement", "по размещению"
    IMPORT = "import", "импорт таблицы"
    UPLOAD = "upload", "загрузка"
    MIGRATION = "migration", "миграция"


class Product(models.Model):
    """Продвигаемый продукт. Заводит человек в админке (ADR-030)."""

    name = models.TextField("название")
    domain = models.TextField("домен")
    is_active = models.BooleanField("активен", default=True, db_default=True)
    created_at = models.DateTimeField("создан", db_default=PgNow())

    class Meta:
        db_table = "products"
        verbose_name = "продукт"
        verbose_name_plural = "продукты"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["domain"], name="products_domain_key"),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.domain = normalize_domain(self.domain)
        adding = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if adding:
                ensure_product_sites(product_ids=[self.pk])

    def clean(self) -> None:
        self.domain = _clean_domain(self.domain)

    def has_history(self) -> bool:
        """С продуктом уже работали: есть решение, аудит, размещение, ключ или промпт.

        Строки продукт × площадка, которые создались сами и остались
        нетронутыми («Новая», без причины, профиля и пометки импорта), и
        настройки продукта — не история (ADR-036).
        """
        touched = self.product_sites.exclude(
            status=SiteStatus.NEW,
            reject_reason__isnull=True,
            content_profile__isnull=True,
            imported_undecided=False,
        )
        return any(
            related.exists()
            for related in (
                touched,
                self.audits.all(),
                self.placements.all(),
                self.keywords.all(),
                self.prompt_templates.all(),
                self.site_notes.all(),
                # Статус меняли и вернули «Новую» — строка снова нетронутая, но
                # история смен у неё есть.
                SiteStatusChange.objects.filter(product_site__product=self),
            )
        )


class Seller(models.Model):
    """Продавец площадок: перекупщик со своим прайсом или каталог (ADR-041, ADR-043).

    Collaborator — тоже продавец, ровно один с `is_collaborator`: по нему
    загрузки находят каталог. Имя уникально без учёта регистра. Метрики
    продавца с `metrics_trusted` показываются как наши замеры.
    """

    name = models.TextField("имя")
    contacts = models.TextField("контакты", null=True, blank=True)
    notes = models.TextField("заметки", null=True, blank=True)
    currency = models.CharField("валюта прайсов", max_length=3, default="EUR", db_default="EUR")
    is_collaborator = models.BooleanField("каталог Collaborator", default=False, db_default=False)
    metrics_trusted = models.BooleanField(
        "метрикам доверяем",
        default=False,
        db_default=False,
        help_text="DR и трафик этого продавца показываются как наши замеры и идут в графики.",
    )
    # Разметка колонок его прайсов: заголовок → поле (ADR-044). Пишет загрузка.
    column_map = models.JSONField("разметка колонок", null=True, blank=True)
    created_at = models.DateTimeField("заведён", db_default=PgNow())

    class Meta:
        db_table = "sellers"
        verbose_name = "продавец"
        verbose_name_plural = "продавцы"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                Lower("name"),
                name="sellers_name_key",
                violation_error_message="Продавец с таким именем уже есть (регистр не важен).",
            ),
            models.UniqueConstraint(
                fields=["is_collaborator"],
                condition=models.Q(is_collaborator=True),
                name="sellers_collaborator_key",
                violation_error_message="Каталог Collaborator уже заведён.",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.name = " ".join(self.name.split())
        self.currency = self.currency.upper()
        super().save(*args, **kwargs)

    @classmethod
    def collaborator(cls) -> "Seller":
        """Каталог Collaborator — его заводит миграция `0007`."""
        return cls.objects.get(is_collaborator=True)


class ActiveSiteManager(models.Manager["Site"]):
    """Площадки без пометки «удалена» — менеджер по умолчанию."""

    def get_queryset(self) -> models.QuerySet["Site"]:
        return super().get_queryset().filter(is_deleted=False)


class Site(models.Model):
    """Площадка-донор: факты, которые не зависят от продукта.

    Удалённая (`is_deleted`) площадка не видна через `Site.objects`;
    через `Site.all_objects` — видна. Домен уникален и среди удалённых:
    импорт и поиск дублей ищут через `all_objects`. Заметки — история в
    `SiteNote` (`site.notes`), рабочая цена — `price`.
    """

    domain = models.TextField("домен")
    collaborator_url = models.TextField("карточка на Collaborator", null=True, blank=True)
    source = models.TextField("источник", null=True, blank=True)
    language = models.TextField("основной язык", null=True, blank=True)
    languages = ArrayField(models.TextField(), verbose_name="языки", null=True, blank=True)
    topics = ArrayField(models.TextField(), verbose_name="тематики", null=True, blank=True)
    declared_topics = ArrayField(
        models.TextField(), verbose_name="особые тематики", null=True, blank=True
    )
    site_type = models.TextField("тип сайта", null=True, blank=True)
    links_allowed = models.SmallIntegerField("ссылок разрешено", null=True, blank=True)
    link_type = models.TextField("тип ссылки", null=True, blank=True)
    marks_as_ad = models.BooleanField("пометка «реклама»", null=True, blank=True)
    content_selector = models.TextField("CSS-селектор статьи", null=True, blank=True)
    # Рабочая цена — решение человека о площадке, общее для всех продуктов
    # (ADR-043). Меняется только через apps.sites.offers.set_working_price.
    price = models.ForeignKey(
        "SitePrice",
        models.PROTECT,
        verbose_name="рабочая цена",
        related_name="+",
        null=True,
        blank=True,
        db_index=False,
    )
    is_deleted = models.BooleanField("удалена", default=False, db_default=False)
    created_at = models.DateTimeField("создана", db_default=PgNow())
    updated_at = models.DateTimeField("изменена", auto_now=True, db_default=PgNow())

    objects = ActiveSiteManager()
    all_objects = models.Manager["Site"]()

    class Meta:
        db_table = "sites"
        verbose_name = "площадка"
        # Все площадки базы; рабочий экран — «Площадки» (ProductSiteLatest).
        verbose_name_plural = "каталог площадок"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["domain"], name="sites_domain_key"),
        ]

    def __str__(self) -> str:
        return self.domain

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.domain = normalize_domain(self.domain)
        adding = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if adding:
                ensure_product_sites(site_ids=[self.pk])

    def clean(self) -> None:
        self.domain = _clean_domain(self.domain)
        # Проверка уникальности Django идёт через менеджер по умолчанию и
        # удалённых не видит — база же не пустит дубль и среди них.
        deleted = Site.all_objects.filter(domain=self.domain, is_deleted=True)
        if deleted.exclude(pk=self.pk).exists():
            raise ValidationError(
                {"domain": "Площадка с этим доменом помечена удалённой — снимите пометку у неё."}
            )


class ProductSite(models.Model):
    """Площадка в работе продукта: решение по ней для этого продукта.

    Строка есть для каждой пары продукт × площадка — создаёт их
    `ensure_product_sites`, вручную не заводятся.
    """

    product = models.ForeignKey(
        Product,
        models.PROTECT,
        verbose_name="продукт",
        related_name="product_sites",
        db_index=False,
    )
    site = models.ForeignKey(
        Site,
        models.PROTECT,
        verbose_name="площадка",
        related_name="product_sites",
        db_index=False,
    )
    status = PgEnumField(
        "статус",
        enum_type="site_status",
        choices=SiteStatus.choices,
        default=SiteStatus.NEW,
        db_default=SiteStatus.NEW,
    )
    reject_reason = models.TextField("причина отказа", null=True, blank=True)
    content_profile = models.JSONField("соответствие тематике", null=True, blank=True)
    imported_undecided = models.BooleanField(
        "импортирована без решения", default=False, db_default=False
    )
    created_at = models.DateTimeField("создана", db_default=PgNow())
    updated_at = models.DateTimeField("изменена", auto_now=True, db_default=PgNow())

    class Meta:
        db_table = "product_sites"
        verbose_name = "решение по площадке"
        verbose_name_plural = "решения по площадкам"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["site", "product"], name="product_sites_site_id_product_id_key"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["product", "status"], name="idx_product_sites_status"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.product}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Решение появилось — пометка «импортирована без решения» больше не
        # верна (ADR-033). Хоть из импорта, хоть из админки.
        if self.status != SiteStatus.NEW and self.imported_undecided:
            self.imported_undecided = False
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "imported_undecided"}
        # Смену статуса запишет триггер; кто и откуда — отметка (ADR-049).
        with transaction.atomic(), stamped():
            super().save(*args, **kwargs)


class SiteStatusChange(models.Model):
    """Смена статуса площадки у продукта — строка истории (ADR-049).

    Пишет только триггер `product_sites_status_*` в базе: код строк не
    создаёт, а отмечает, кто и откуда меняет (`config.changes.stamped`).
    `from_status` пусто — строка создана сразу не «Новой». `placement` —
    размещение, по которому статус сменила система (ADR-047).
    """

    product_site = models.ForeignKey(
        ProductSite,
        models.PROTECT,
        verbose_name="площадка у продукта",
        related_name="status_changes",
        db_index=False,
    )
    from_status = PgEnumField(
        "был", enum_type="site_status", choices=SiteStatus.choices, null=True, blank=True
    )
    to_status = PgEnumField("стал", enum_type="site_status", choices=SiteStatus.choices)
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
    placement = models.ForeignKey(
        "placements.Placement",
        models.PROTECT,
        verbose_name="по размещению",
        related_name="+",
        null=True,
        blank=True,
        db_index=False,
    )
    run_id = models.UUIDField("run_id", null=True, blank=True)
    changed_at = models.DateTimeField("когда", db_default=PgNow())

    class Meta:
        db_table = "site_status_changes"
        verbose_name = "смена статуса площадки"
        verbose_name_plural = "история статусов площадок"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["product_site", "-changed_at"], name="idx_site_status_changes"),
        ]

    def __str__(self) -> str:
        return f"{self.product_site_id}: {self.from_status} → {self.to_status}"


class SiteMetric(models.Model):
    """Снапшот метрик площадки. Новый замер — новая строка (ADR-008)."""

    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="metrics", db_index=False
    )
    dr = models.SmallIntegerField("DR", null=True, blank=True)
    organic_traffic = models.IntegerField("органический трафик", null=True, blank=True)
    top_geo = models.TextField("основное гео", null=True, blank=True)
    top_geo_traffic = models.IntegerField("трафик основного гео", null=True, blank=True)
    total_keywords = models.IntegerField("ключей в органике", null=True, blank=True)
    source = PgEnumField(
        "источник",
        enum_type="metric_source",
        choices=MetricSource.choices,
        default=MetricSource.MANUAL,
        db_default=MetricSource.MANUAL,
    )
    # Замер со слов продавца (ADR-043); пусто — наш: Ahrefs, вручную, таблица.
    seller = models.ForeignKey(
        Seller,
        models.PROTECT,
        verbose_name="со слов продавца",
        related_name="metrics",
        null=True,
        blank=True,
        db_index=False,
    )
    # Сырой ответ источника целиком: понадобится поле, которого нет в колонках.
    raw = models.JSONField("сырой ответ", null=True, blank=True)
    checked_at = models.DateTimeField("дата замера", db_default=PgNow())

    class Meta:
        db_table = "site_metrics"
        verbose_name = "метрики"
        verbose_name_plural = "метрики"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["site", "-checked_at"], name="idx_metrics_site"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.checked_at:%d.%m.%Y}"


class SiteCountryMetric(models.Model):
    """Трафик и ключи площадки в одной стране — снимок (ADR-045).

    Пишет выгрузка Ahrefs Batch Analysis под страну, позже — Ahrefs API
    (E2-07). Топ-регион выгрузки «все страны» сюда не попадает: он в
    `SiteMetric`, по нему трафик других стран не узнать. Замер всегда наш —
    продавцы трафик по странам не присылают.
    """

    site = models.ForeignKey(
        Site,
        models.PROTECT,
        verbose_name="площадка",
        related_name="country_metrics",
        db_index=False,
    )
    # Код страны строчными, как его пишет Ahrefs: us, gb, in.
    country = models.CharField("страна", max_length=2)
    organic_traffic = models.IntegerField("органический трафик", null=True, blank=True)
    total_keywords = models.IntegerField("ключей в органике", null=True, blank=True)
    source = PgEnumField(
        "источник",
        enum_type="metric_source",
        choices=MetricSource.choices,
        default=MetricSource.MANUAL,
        db_default=MetricSource.MANUAL,
    )
    raw = models.JSONField("сырой ответ", null=True, blank=True)
    checked_at = models.DateTimeField("дата замера", db_default=PgNow())

    class Meta:
        db_table = "site_country_metrics"
        verbose_name = "метрики по стране"
        verbose_name_plural = "метрики по странам"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["site", "country", "-checked_at"], name="idx_country_metrics_site"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.country} · {self.checked_at:%d.%m.%Y}"


class SitePrice(models.Model):
    """Предложение продавца на дату: одна услуга — одна цена, в центах (ADR-043).

    Снимок: новая цена — новая строка. Сравнивается только цена услуги
    (`placement_cents`); написание, анонс и серая цена — справочно.
    `reviewed_at` пусто — по предложению ещё не решили (разбор). Итог не
    хранится: его считают представления от рабочей цены площадки.
    """

    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="prices", db_index=False
    )
    seller = models.ForeignKey(
        Seller, models.PROTECT, verbose_name="продавец", related_name="prices", db_index=False
    )
    placement_type = PgEnumField(
        "услуга",
        enum_type="placement_type",
        choices=PlacementType.choices,
        default=PlacementType.GUEST_POST,
        db_default=PlacementType.GUEST_POST,
    )
    placement_cents = models.IntegerField("цена услуги, центы", null=True, blank=True)
    announce_cents = models.IntegerField("анонс, центы", null=True, blank=True)
    writing_cents = models.IntegerField("написание, центы", null=True, blank=True)
    gray_cents = models.IntegerField("серая цена, центы", null=True, blank=True)
    currency = models.CharField("валюта", max_length=3, default="EUR", db_default="EUR")
    # «Прочие данные» строки файла: колонки без своего поля, заголовок → значение.
    extra = models.JSONField("прочие данные", null=True, blank=True)
    source = PgEnumField(
        "источник",
        enum_type="metric_source",
        choices=MetricSource.choices,
        default=MetricSource.MANUAL,
        db_default=MetricSource.MANUAL,
    )
    reviewed_at = models.DateTimeField("разобрано", null=True, blank=True)
    checked_at = models.DateTimeField("дата цены", db_default=PgNow())

    class Meta:
        db_table = "site_prices"
        verbose_name = "предложение продавца"
        verbose_name_plural = "предложения продавцов"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["site", "seller", "placement_type", "-checked_at"], name="idx_prices_site"
            ),
            models.Index(
                fields=["site"],
                name="idx_prices_pending",
                condition=models.Q(reviewed_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.seller} · {self.checked_at:%d.%m.%Y}"


class GrayScan(models.Model):
    """Снапшот серости: доля серых тем в индексе площадки."""

    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="gray_scans", db_index=False
    )
    total_indexed = models.IntegerField("страниц в индексе", null=True, blank=True)
    gray_hits = models.IntegerField("серых страниц", null=True, blank=True)
    ratio = models.DecimalField(
        "доля серых, %", max_digits=5, decimal_places=2, null=True, blank=True
    )
    breakdown = models.JSONField("по категориям", null=True, blank=True)
    sample_urls = models.JSONField("примеры адресов", null=True, blank=True)
    method = PgEnumField(
        "способ",
        enum_type="metric_source",
        choices=MetricSource.choices,
        default=MetricSource.SERP_API,
        db_default=MetricSource.SERP_API,
    )
    checked_at = models.DateTimeField("дата замера", db_default=PgNow())

    class Meta:
        db_table = "gray_scans"
        verbose_name = "проверка серости"
        verbose_name_plural = "проверки серости"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["site", "-checked_at"], name="idx_gray_site"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.checked_at:%d.%m.%Y}"


class SiteAudit(models.Model):
    """Вердикт по площадке под конкретный продукт. Повторный аудит — новая строка."""

    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="audits", db_index=False
    )
    product = models.ForeignKey(
        Product, models.PROTECT, verbose_name="продукт", related_name="audits", db_index=False
    )
    verdict = PgEnumField("вердикт", enum_type="audit_verdict", choices=AuditVerdict.choices)
    score = models.SmallIntegerField("оценка", null=True, blank=True)
    blockers = models.JSONField("стоп-факторы", null=True, blank=True)
    strengths = models.JSONField("сильные стороны", null=True, blank=True)
    weaknesses = models.JSONField("слабые стороны", null=True, blank=True)
    summary = models.TextField("вывод", null=True, blank=True)
    missing_data = models.JSONField("не хватило данных", null=True, blank=True)
    price_flag = models.JSONField("превышение ориентира", null=True, blank=True)
    dossier = models.JSONField("досье", null=True, blank=True)
    author = PgEnumField("автор", enum_type="audit_author", choices=AuditAuthor.choices)
    model = models.TextField("модель", null=True, blank=True)
    run_id = models.UUIDField("run_id", null=True, blank=True)
    created_at = models.DateTimeField("дата", db_default=PgNow())

    class Meta:
        db_table = "site_audits"
        verbose_name = "аудит"
        verbose_name_plural = "аудиты"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(score__gte=0, score__lte=100),
                name="site_audits_score_check",
                violation_error_message="Оценка — от 0 до 100.",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["site", "product", "-created_at"], name="idx_audits_site"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.product} · {self.get_verdict_display()}"


class SiteList(models.Model):
    """Рабочий список площадок: одна загрузка таблицы или каталога (ADR-033).

    Площадка в базе одна и входит в списки со всей историей — статусом
    по продуктам, аудитами, размещениями. Список общий для всех продуктов.
    """

    name = models.TextField("название")
    source = models.TextField("откуда", null=True, blank=True)
    created_at = models.DateTimeField("создан", db_default=PgNow())

    class Meta:
        db_table = "site_lists"
        verbose_name = "рабочий список"
        verbose_name_plural = "рабочие списки"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["name"], name="site_lists_name_key"),
        ]

    def __str__(self) -> str:
        return self.name


class SiteListItem(models.Model):
    """Площадка в рабочем списке.

    `first_seen` — площадки не было в базе до этого списка: замена
    колонке «Новая?» из таблицы.
    """

    site_list = models.ForeignKey(
        SiteList,
        models.PROTECT,
        verbose_name="список",
        related_name="items",
        db_column="list_id",
        db_index=False,
    )
    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="list_items", db_index=False
    )
    first_seen = models.BooleanField("впервые в базе", default=False, db_default=False)
    added_at = models.DateTimeField("добавлена", db_default=PgNow())

    class Meta:
        db_table = "site_list_items"
        verbose_name = "строка рабочего списка"
        verbose_name_plural = "строки рабочих списков"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["site_list", "site"], name="site_list_items_list_id_site_id_key"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["site"], name="idx_list_items_site"),
        ]

    def __str__(self) -> str:
        return f"{self.site_list} · {self.site}"


class SiteNote(models.Model):
    """Заметка о площадке — история: не правится и не удаляется (ADR-043).

    Автор — пользователь, или источник — файл («таблица линкбилдинга»).
    Продукт — если заметка про решение под него (причина отказа).
    """

    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="notes", db_index=False
    )
    product = models.ForeignKey(
        Product,
        models.PROTECT,
        verbose_name="продукт",
        related_name="site_notes",
        null=True,
        blank=True,
        db_index=False,
    )
    body = models.TextField("заметка")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.PROTECT,
        verbose_name="автор",
        related_name="site_notes",
        null=True,
        blank=True,
        db_index=False,
    )
    source = models.TextField("источник", null=True, blank=True)
    created_at = models.DateTimeField("дата", db_default=PgNow())

    class Meta:
        db_table = "site_notes"
        verbose_name = "заметка"
        verbose_name_plural = "заметки"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["site", "-created_at"], name="idx_site_notes_site"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.body[:40]}"


class ExchangeRate(models.Model):
    """Курс ЕЦБ на дату: сколько единиц валюты за 1 евро (ADR-043).

    Только для сравнения цен в разных валютах — деньги в разных валютах
    не складываются. Нет курса на сегодня — берётся последний.
    """

    currency = models.CharField("валюта", max_length=3)
    rate_date = models.DateField("дата курса")
    rate = models.DecimalField("за 1 евро", max_digits=14, decimal_places=6)
    created_at = models.DateTimeField("получен", db_default=PgNow())

    class Meta:
        db_table = "exchange_rates"
        verbose_name = "курс валюты"
        verbose_name_plural = "курсы валют"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["currency", "rate_date"], name="exchange_rates_currency_rate_date_key"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.currency} {self.rate} · {self.rate_date:%d.%m.%Y}"


class Upload(models.Model):
    """Загрузка файла: прайс продавца, каталог Collaborator, выгрузки Ahrefs, размещения.

    Путь: файл и разметка колонок (`new`) → проверка в фоне (`checking`) →
    сводка до записи (`checked`) → запись в фоне (`writing`) → разбор
    (`done`). Результат — рабочий список. Файл лежит в папке загрузок
    (`UPLOADS_DIR`), путь в `file_path` — от неё (ADR-044).

    - Выгрузка Ahrefs Batch Analysis (ADR-045) — без продавца и рабочего
      списка, `prices_date` — дата замера, `country` — страна выгрузки
      (пусто — все страны).
    - Размещения продукта (E1-09) — `product`; продавец и сотрудник — «от
      кого файл», подставляются в строки без своих колонок; `prices_date` —
      дата файла. Разбора нет, результат — рабочий список.
    - Ссылающиеся домены Ahrefs (E1-09) — `product`, без продавца и рабочего
      списка, `prices_date` — дата выгрузки.
    """

    kind = PgEnumField("что загружаем", enum_type="upload_kind", choices=UploadKind.choices)
    seller = models.ForeignKey(
        Seller,
        models.PROTECT,
        verbose_name="продавец",
        related_name="uploads",
        null=True,
        blank=True,
        db_index=False,
    )
    product = models.ForeignKey(
        Product,
        models.PROTECT,
        verbose_name="продукт",
        related_name="uploads",
        null=True,
        blank=True,
        db_index=False,
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.PROTECT,
        verbose_name="сотрудник",
        related_name="+",
        null=True,
        blank=True,
        db_index=False,
    )
    prices_date = models.DateField("дата цен")
    country = models.CharField("страна выгрузки", max_length=2, null=True, blank=True)
    file_name = models.TextField("файл")
    file_path = models.TextField("путь к файлу")
    file_sha256 = models.CharField("отпечаток файла", max_length=64)
    header_row = models.IntegerField("строка заголовков", null=True, blank=True)
    columns = models.JSONField("колонки файла", null=True, blank=True)
    mapping = models.JSONField("разметка", null=True, blank=True)
    currency = models.CharField("валюта цен", max_length=3, null=True, blank=True)
    status = PgEnumField(
        "состояние",
        enum_type="upload_status",
        choices=UploadStatus.choices,
        default=UploadStatus.NEW,
        db_default=UploadStatus.NEW,
    )
    summary = models.JSONField("сводка до записи", null=True, blank=True)
    result = models.JSONField("итог записи", null=True, blank=True)
    error = models.TextField("ошибка", null=True, blank=True)
    site_list = models.ForeignKey(
        SiteList,
        models.PROTECT,
        verbose_name="рабочий список",
        related_name="uploads",
        null=True,
        blank=True,
        db_index=False,
    )
    run_id = models.UUIDField("run_id", null=True, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.PROTECT,
        verbose_name="загрузил",
        related_name="uploads",
        null=True,
        blank=True,
        db_index=False,
    )
    created_at = models.DateTimeField("загружен", db_default=PgNow())
    written_at = models.DateTimeField("записан", null=True, blank=True)
    # Запись шла с журналом изменений (ADR-060): загрузку можно отменить целиком.
    journaled = models.BooleanField("с журналом", default=False, db_default=False)

    class Meta:
        db_table = "uploads"
        verbose_name = "загрузка"
        verbose_name_plural = "загрузки"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Продавец обязателен у прайса и каталога. Выгрузка Ahrefs — наш замер
            # (ADR-045); у размещений продавец — в строках файла (E1-09).
            models.CheckConstraint(
                condition=models.Q(seller__isnull=False)
                | models.Q(kind__in=[kind.value for kind in UPLOADS_WITHOUT_SELLER]),
                name="uploads_seller_check",
                violation_error_message="У прайса и каталога должен быть продавец.",
            ),
            # Размещения и ссылающиеся домены — всегда чьего-то продукта (E1-09).
            models.CheckConstraint(
                condition=models.Q(product__isnull=False)
                | models.Q(
                    kind__in=[kind.value for kind in UploadKind if kind not in UPLOADS_FOR_PRODUCT]
                ),
                name="uploads_product_check",
                violation_error_message="У размещений и ссылающихся доменов должен быть продукт.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_name} · {self.prices_date:%d.%m.%Y} · {self.file_name}"

    @property
    def source_name(self) -> str:
        """Чей файл — для экранов: продавец, «Ahrefs · страна» или продукт размещений."""
        if self.kind == UploadKind.PLACEMENTS:
            source = self.seller.name if self.seller is not None else ""
            if self.employee is not None:
                person = self.employee.first_name or self.employee.username
                source = f"{source}, {person}" if source else person
            name = f"Размещения {self.product}"
            return f"{name} · {source}" if source else name
        if self.kind == UploadKind.REF_DOMAINS:
            return f"Ahrefs · ссылаются на {self.product}"
        if self.seller is not None:
            return self.seller.name
        return f"Ahrefs · {self.country.upper()}" if self.country else "Ahrefs · все страны"

    def get_seller(self) -> Seller:
        """Продавец прайса или каталога. У выгрузок Ahrefs его нет — значит, ошибка в коде."""
        if self.seller is None:
            raise ValueError(f"У загрузки {self.pk} нет продавца: это не прайс и не каталог")
        return self.seller

    def get_product(self) -> "Product":
        """Продукт размещений или ссылающихся доменов. У прайса его нет — ошибка в коде."""
        if self.product is None:
            raise ValueError(f"У загрузки {self.pk} нет продукта: это не размещения")
        return self.product


class UploadItem(models.Model):
    """Строка разбора: предложение из файла и с чем его сравнили при записи.

    Вкладка (`review_group`) — по положению на момент записи. Решено ли —
    по `reviewed_at` предложения: решение принимают и в «Площадках», и в
    карточке, разбор это видит.
    """

    upload = models.ForeignKey(
        Upload, models.PROTECT, verbose_name="загрузка", related_name="items", db_index=False
    )
    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="+", db_index=False
    )
    price = models.ForeignKey(
        SitePrice,
        models.PROTECT,
        verbose_name="предложение из файла",
        related_name="+",
        db_index=False,
    )
    ref_price = models.ForeignKey(
        SitePrice,
        models.PROTECT,
        verbose_name="рабочая цена до загрузки",
        related_name="+",
        null=True,
        blank=True,
        db_index=False,
    )
    review_group = PgEnumField("вкладка", enum_type="review_group", choices=ReviewGroup.choices)
    needs_decision = models.BooleanField("ждало решения", default=False, db_default=False)
    auto_applied = models.BooleanField("стало рабочей само", default=False, db_default=False)
    site_created = models.BooleanField("новая в базе", default=False, db_default=False)
    line = models.IntegerField("строка файла", null=True, blank=True)
    source_value = models.TextField("адрес в файле", null=True, blank=True)

    class Meta:
        db_table = "upload_items"
        verbose_name = "строка разбора"
        verbose_name_plural = "строки разбора"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["upload", "review_group"], name="idx_upload_items_upload"),
        ]

    def __str__(self) -> str:
        return f"{self.upload_id} · {self.site_id} · {self.get_review_group_display()}"


class UploadChange(models.Model):
    """Журнал загрузки: строка, которую запись вставила, поменяла или удалила (ADR-060).

    Пишет триггер `log_upload_change` на рабочих таблицах, пока в транзакции
    стоит номер загрузки (`seo.upload_id`, его ставит `service.write`). У
    вставки прежней строки нет, у правки и удаления — строка целиком до
    изменения. Отмена загрузки (`uploads/undo.py`) по журналу возвращает
    прежние строки и удаляет вставленные.
    """

    upload = models.ForeignKey(
        Upload, models.PROTECT, verbose_name="загрузка", related_name="changes", db_index=False
    )
    table_name = models.TextField("таблица")
    row_id = models.BigIntegerField("строка")
    op = models.TextField("действие")  # I — вставка, U — правка, D — удаление
    before = models.JSONField("было", null=True, blank=True)

    class Meta:
        db_table = "upload_changes"
        verbose_name = "изменение загрузки"
        verbose_name_plural = "изменения загрузки"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["upload", "table_name", "row_id"], name="idx_upload_changes_upload"
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(op__in=["I", "U", "D"]), name="upload_changes_op_check"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.upload_id} · {self.op} {self.table_name}#{self.row_id}"


class ProductRefDomain(models.Model):
    """Домен, который ссылается на продукт, — по выгрузке Ahrefs «Referring domains» (E1-09).

    Отдельно от площадок: в выгрузке google.com и тысячи случайных доменов, в
    «Площадки» они не попадают. Площадка с тем же доменом в «Площадках» этого
    продукта по умолчанию скрыта — второй ссылкой с того же домена не выиграть;
    у других продуктов видна. Совпадение — только точное: поддомен из выгрузки
    (`blog.example.com`) площадку `example.com` не прячет.

    Ссылка пропала — у Ahrefs есть дата Lost (`lost_at`) или домена нет в
    новой выгрузке продукта (`missing_since` — дата той выгрузки). Строка не
    удаляется; домен вернулся в выгрузку — пометки снимаются.
    """

    product = models.ForeignKey(
        Product, models.PROTECT, verbose_name="продукт", related_name="ref_domains", db_index=False
    )
    domain = models.TextField("домен")
    first_seen_at = models.DateTimeField(
        "ссылается с", null=True, blank=True, help_text="First seen у Ahrefs."
    )
    lost_at = models.DateTimeField(
        "ссылка пропала", null=True, blank=True, help_text="Lost у Ahrefs."
    )
    seen_on = models.DateField("в выгрузке от")
    missing_since = models.DateField("нет в выгрузках с", null=True, blank=True)
    upload = models.ForeignKey(
        Upload,
        models.PROTECT,
        verbose_name="выгрузка",
        related_name="+",
        null=True,
        blank=True,
        db_index=False,
    )
    created_at = models.DateTimeField("добавлен", db_default=PgNow())
    updated_at = models.DateTimeField("изменён", auto_now=True, db_default=PgNow())

    class Meta:
        db_table = "product_ref_domains"
        verbose_name = "ссылающийся домен"
        verbose_name_plural = "ссылающиеся домены"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Один домен у продукта — одна строка; индекс заодно ищет «ссылается ли».
            models.UniqueConstraint(
                fields=["product", "domain"], name="product_ref_domains_product_id_domain_key"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.domain} → {self.product}"

    @property
    def is_linking(self) -> bool:
        """Ссылается сейчас: Ahrefs не отметил пропажу, и домен есть в последней выгрузке."""
        return self.lost_at is None and self.missing_since is None


def ensure_product_sites(
    *, product_ids: Iterable[int] | None = None, site_ids: Iterable[int] | None = None
) -> None:
    """Создаёт недостающие строки `ProductSite` со статусом «Новая».

    Без аргументов — для всех пар продукт × площадка, с аргументами —
    только для этих продуктов или площадок. Удалённые площадки и
    неактивные продукты тоже получают строки: строка есть для каждой
    пары. Повторный вызов ничего не дублирует.
    """
    products = Product.objects.all()
    sites = Site.all_objects.all()
    if product_ids is not None:
        products = products.filter(pk__in=list(product_ids))
    if site_ids is not None:
        sites = sites.filter(pk__in=list(site_ids))
    # list() выполняет запрос сразу: иначе QuerySet площадок заново
    # уходил бы в базу на каждой итерации внешнего цикла.
    product_pks = list(products.values_list("pk", flat=True))
    site_pks = list(sites.values_list("pk", flat=True))
    pairs = [
        ProductSite(product_id=product_id, site_id=site_id)
        for product_id in product_pks
        for site_id in site_pks
    ]
    # ignore_conflicts — это ON CONFLICT DO NOTHING: существующие пары пропускаются.
    ProductSite.objects.bulk_create(pairs, ignore_conflicts=True)


def _clean_domain(value: str) -> str:
    try:
        return normalize_domain(value)
    except ValueError as error:
        raise ValidationError({"domain": str(error)}) from error


class ProductSiteLatest(models.Model):
    """Площадка в работе продукта «на сегодня» — строка `v_product_site_latest`.

    Только чтение: это представление, а не таблица (`managed = False` —
    Django не создаёт и не меняет его, представление делает миграция
    `0003_views`). Одна строка на пару продукт × площадка, `id` — строки
    `product_sites`. «Пишем мы» и плановые расходы считает представление
    и больше никто.
    """

    product = models.ForeignKey(
        Product, models.DO_NOTHING, verbose_name="продукт", related_name="+", db_constraint=False
    )
    site = models.ForeignKey(
        Site, models.DO_NOTHING, verbose_name="площадка", related_name="+", db_constraint=False
    )
    status = PgEnumField("статус", enum_type="site_status", choices=SiteStatus.choices)
    reject_reason = models.TextField("причина отказа", null=True)
    imported_undecided = models.BooleanField("импортирована без решения")
    domain = models.TextField("домен")
    language = models.TextField("язык", null=True)
    topics = ArrayField(models.TextField(), verbose_name="тематики", null=True)
    declared_topics = ArrayField(models.TextField(), verbose_name="особые тематики", null=True)
    links_allowed = models.SmallIntegerField("ссылок разрешено", null=True)
    link_type = models.TextField("тип ссылки", null=True)
    marks_as_ad = models.BooleanField("пометка «реклама»", null=True)
    dr = models.SmallIntegerField("DR", null=True)
    organic_traffic = models.IntegerField("трафик", null=True)
    total_keywords = models.IntegerField("ключей в органике", null=True)
    # Топ-регион — из последнего замера, где он есть (ADR-045): каталог без гео не стирает.
    top_geo = models.TextField("основное гео", null=True)
    top_geo_traffic = models.IntegerField("трафик основного гео", null=True)
    top_geo_at = models.DateTimeField("дата замера гео", null=True)
    metrics_at = models.DateTimeField("дата метрик", null=True)
    metrics_trusted = models.BooleanField("метрики доверенного источника", null=True)
    metrics_seller = models.TextField("метрики со слов продавца", null=True)
    # Рабочая цена (ADR-043): исходная сумма и валюта, в евро — по курсу ЕЦБ.
    price_id = models.BigIntegerField("рабочая цена, id", null=True)
    price_seller = models.TextField("продавец рабочей цены", null=True)
    price_type = PgEnumField(
        "услуга", enum_type="placement_type", choices=PlacementType.choices, null=True
    )
    placement_cents = models.IntegerField("цена услуги, центы", null=True)
    announce_cents = models.IntegerField("анонс, центы", null=True)
    writing_cents = models.IntegerField("написание, центы", null=True)
    price_currency = models.TextField("валюта", null=True)
    prices_at = models.DateTimeField("дата цены", null=True)
    placement_eur_cents = models.IntegerField("цена услуги, евроценты", null=True)
    writing_eur_cents = models.IntegerField("написание, евроценты", null=True)
    reference_total_cents = models.IntegerField("к ориентиру, евроценты", null=True)
    we_write = models.BooleanField("пишем мы", null=True)
    expected_spend_cents = models.IntegerField("плановые расходы, евроценты", null=True)
    # Пометки разбора: новая цена того же продавца и предложение дешевле рабочей.
    new_price_id = models.BigIntegerField("новая цена, id", null=True)
    new_price_cents = models.IntegerField("новая цена, центы", null=True)
    new_price_currency = models.TextField("валюта новой цены", null=True)
    new_price_pending = models.BooleanField("новая цена не разобрана", null=True)
    cheaper_id = models.BigIntegerField("дешевле, id", null=True)
    cheaper_seller = models.TextField("дешевле у продавца", null=True)
    cheaper_cents = models.IntegerField("дешевле, центы", null=True)
    cheaper_currency = models.TextField("валюта дешёвого", null=True)
    cheaper_eur_cents = models.IntegerField("дешевле, евроценты", null=True)
    cheaper_pending = models.BooleanField("дешёвое не разобрано", null=True)
    offers_pending = models.BooleanField("есть неразобранные предложения")
    gray_ratio = models.DecimalField("доля серых, %", max_digits=5, decimal_places=2, null=True)
    notes_count = models.BigIntegerField("заметок")
    last_note = models.TextField("последняя заметка", null=True)
    last_note_at = models.DateTimeField("дата последней заметки", null=True)
    last_verdict = PgEnumField(
        "вердикт", enum_type="audit_verdict", choices=AuditVerdict.choices, null=True
    )
    last_score = models.SmallIntegerField("оценка", null=True)
    audited_at = models.DateTimeField("дата аудита", null=True)
    placements_published = models.BigIntegerField("опубликовано")
    other_products_placed = ArrayField(
        models.TextField(), verbose_name="размещались другие продукты", null=True
    )

    class Meta:
        managed = False
        db_table = "v_product_site_latest"
        verbose_name = "площадка продукта на сегодня"
        # Главный рабочий экран — в меню первым пунктом «Работы» (E9-08).
        verbose_name_plural = "площадки"

    def __str__(self) -> str:
        return f"{self.domain} · {self.product_id}"


class SiteOffer(models.Model):
    """Текущее предложение площадки — строка `v_site_offers` (ADR-043).

    Последнее предложение каждого продавца за каждую услугу, цена услуги —
    ещё и в евро по последнему курсу ЕЦБ. Только чтение: это представление,
    `id` — строки `site_prices`. Отсюда список предложений в «Площадках» и
    в карточке площадки.
    """

    site = models.ForeignKey(
        Site, models.DO_NOTHING, verbose_name="площадка", related_name="+", db_constraint=False
    )
    seller = models.ForeignKey(
        Seller, models.DO_NOTHING, verbose_name="продавец", related_name="+", db_constraint=False
    )
    seller_name = models.TextField("продавец", db_column="seller")
    placement_type = PgEnumField(
        "услуга", enum_type="placement_type", choices=PlacementType.choices
    )
    placement_cents = models.IntegerField("цена услуги, центы", null=True)
    currency = models.CharField("валюта", max_length=3)
    placement_eur_cents = models.IntegerField("цена услуги, евроценты", null=True)
    announce_cents = models.IntegerField("анонс, центы", null=True)
    writing_cents = models.IntegerField("написание, центы", null=True)
    gray_cents = models.IntegerField("серая цена, центы", null=True)
    extra = models.JSONField("прочие данные", null=True)
    reviewed_at = models.DateTimeField("разобрано", null=True)
    checked_at = models.DateTimeField("дата цены")

    class Meta:
        managed = False
        db_table = "v_site_offers"
        verbose_name = "текущее предложение"
        verbose_name_plural = "текущие предложения"

    def __str__(self) -> str:
        return f"{self.site_id} · {self.seller_name} · {self.get_placement_type_display()}"


class SiteCountryLatest(models.Model):
    """Последний замер площадки по стране — строка `v_site_country_latest` (ADR-045).

    Отсюда колонки «трафик» и «ключи» выбранного региона в «Площадках» и
    список регионов: страна в нём есть, только если под неё грузили
    выгрузку страны. Только чтение, `id` — строки `site_country_metrics`.
    """

    site = models.ForeignKey(
        Site, models.DO_NOTHING, verbose_name="площадка", related_name="+", db_constraint=False
    )
    country = models.CharField("страна", max_length=2)
    organic_traffic = models.IntegerField("органический трафик", null=True)
    total_keywords = models.IntegerField("ключей в органике", null=True)
    source = PgEnumField("источник", enum_type="metric_source", choices=MetricSource.choices)
    checked_at = models.DateTimeField("дата замера")

    class Meta:
        managed = False
        db_table = "v_site_country_latest"
        verbose_name = "последний замер по стране"
        verbose_name_plural = "последние замеры по странам"

    def __str__(self) -> str:
        return f"{self.site_id} · {self.country}"
