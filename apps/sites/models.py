"""Блок 1 модели данных: продукты, площадки, решения по ним, снапшоты.

Схема — `schema.sql` 1.2, как модели её повторяют — ADR-029: имена
таблиц, индексов и ограничений из схемы, значения по умолчанию в базе
(`db_default`), `on_delete=PROTECT`. Несколько продуктов — ADR-030:
площадка хранит только факты о себе, решение по ней — в `ProductSite`.
"""

from collections.abc import Iterable
from typing import Any, ClassVar

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models, transaction

from apps.sites.domains import normalize_domain
from config.db import PgEnumField, PgNow


class SiteStatus(models.TextChoices):
    NEW = "new", "Новая"
    AUDITING = "auditing", "На аудите"
    APPROVED = "approved", "Одобрена"
    REJECTED = "rejected", "Отклонена"
    PLACED = "placed", "Размещались"
    BLACKLISTED = "blacklisted", "Чёрный список"


class MetricSource(models.TextChoices):
    AHREFS_API = "ahrefs_api", "Ahrefs API"
    SERP_API = "serp_api", "SERP API"
    MANUAL = "manual", "Вручную"
    CSV_IMPORT = "csv_import", "Импорт из файла"
    COLLABORATOR_API = "collaborator_api", "Collaborator API"


class AuditVerdict(models.TextChoices):
    YES = "yes", "Да"
    NO = "no", "Нет"
    BORDERLINE = "borderline", "Пограничная"


class AuditAuthor(models.TextChoices):
    HUMAN = "human", "Человек"
    LLM = "llm", "LLM"
    SYSTEM = "system", "Система"


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


class ActiveSiteManager(models.Manager["Site"]):
    """Площадки без пометки «удалена» — менеджер по умолчанию."""

    def get_queryset(self) -> models.QuerySet["Site"]:
        return super().get_queryset().filter(is_deleted=False)


class Site(models.Model):
    """Площадка-донор: факты, которые не зависят от продукта.

    Удалённая (`is_deleted`) площадка не видна через `Site.objects`;
    через `Site.all_objects` — видна. Домен уникален и среди удалённых:
    импорт и поиск дублей ищут через `all_objects`.
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
    notes = models.TextField("заметки", null=True, blank=True)
    content_selector = models.TextField("CSS-селектор статьи", null=True, blank=True)
    is_deleted = models.BooleanField("удалена", default=False, db_default=False)
    created_at = models.DateTimeField("создана", db_default=PgNow())
    updated_at = models.DateTimeField("изменена", auto_now=True, db_default=PgNow())

    objects = ActiveSiteManager()
    all_objects = models.Manager["Site"]()

    class Meta:
        db_table = "sites"
        verbose_name = "площадка"
        verbose_name_plural = "площадки"
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
        verbose_name = "площадка продукта"
        verbose_name_plural = "площадки продуктов"
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


class SiteMetric(models.Model):
    """Снапшот метрик площадки. Новый замер — новая строка (ADR-008)."""

    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="metrics", db_index=False
    )
    dr = models.SmallIntegerField("DR", null=True, blank=True)
    organic_traffic = models.IntegerField("органический трафик", null=True, blank=True)
    us_traffic = models.IntegerField("трафик США", null=True, blank=True)
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


class SitePrice(models.Model):
    """Снапшот цен площадки в центах (ADR-009). Итог не хранится."""

    site = models.ForeignKey(
        Site, models.PROTECT, verbose_name="площадка", related_name="prices", db_index=False
    )
    placement_cents = models.IntegerField("размещение, центы", null=True, blank=True)
    announce_cents = models.IntegerField("анонс, центы", null=True, blank=True)
    writing_cents = models.IntegerField("написание, центы", null=True, blank=True)
    currency = models.CharField("валюта", max_length=3, default="EUR", db_default="EUR")
    source = PgEnumField(
        "источник",
        enum_type="metric_source",
        choices=MetricSource.choices,
        default=MetricSource.MANUAL,
        db_default=MetricSource.MANUAL,
    )
    checked_at = models.DateTimeField("дата замера", db_default=PgNow())

    class Meta:
        db_table = "site_prices"
        verbose_name = "цены"
        verbose_name_plural = "цены"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["site", "-checked_at"], name="idx_prices_site"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.checked_at:%d.%m.%Y}"


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
