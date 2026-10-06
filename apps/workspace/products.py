"""Рабочий продукт пользователя (E9-12, ADR-057).

Продукт выбирают в шапке, рядом с «SEO-система»; выбор у каждого свой и
хранится в базе (`user_settings`). С ним открываются все экраны с фильтром
продукта и главная, он подставлен в новые записи. Не выбран — первый активный
продукт.

Фильтр продукта в колонке справа — не рамка, а лупа (E1-19, ADR-063):
открывается на «Все», дальше помнит последний выбор, шапку не меняет. Где
рабочий продукт решает, чьи строки в списке («Площадки», «Размещения»), стоит
`FrameProductFilter`: строки всегда рабочего продукта, а выбор в фильтре
только сужает их до площадок, где работал и выбранный продукт.
"""

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

from django.contrib import admin
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.db.models import Exists, OuterRef, QuerySet
from django.http import HttpRequest, QueryDict
from django.utils import timezone

from apps.placements.models import Placement
from apps.sites.models import Product
from apps.workspace.filters import Remembering, settings_of
from apps.workspace.models import UserSettings

ALL = "all"
_CACHE = "_seo_working_product"
_PRODUCTS = "_seo_products"


def products_of(request: HttpRequest) -> list[tuple[int, str, bool]]:
    """Все продукты — (id, название, активен) по порядку заведения.

    Один запрос на страницу: его берут шапка, фильтр продукта и выбор рабочего.
    """
    if not hasattr(request, _PRODUCTS):
        rows = Product.objects.order_by("pk").values_list("pk", "name", "is_active")
        setattr(request, _PRODUCTS, list(rows))
    products: list[tuple[int, str, bool]] = getattr(request, _PRODUCTS)
    return products


def working_product_id(request: HttpRequest) -> int | None:
    """Рабочий продукт того, кто смотрит; не выбран — первый активный.

    Один раз на запрос: фильтры и шапка спрашивают его по нескольку раз.
    """
    if hasattr(request, _CACHE):
        cached: int | None = getattr(request, _CACHE)
        return cached
    found = settings_of(request)
    chosen = found.product_id if found is not None else None
    if chosen is None:
        chosen = next((pk for pk, _, active in products_of(request) if active), None)
    setattr(request, _CACHE, chosen)
    return chosen


def chosen_product_id(user: AbstractBaseUser | AnonymousUser) -> int | None:
    """Продукт, выбранный в шапке; не выбирал — None."""
    if not user.is_authenticated:
        return None
    return (
        UserSettings.objects.filter(user_id=user.pk, product__isnull=False)
        .values_list("product_id", flat=True)
        .first()
    )


def choose_product(user: AbstractBaseUser, product: Product) -> None:
    UserSettings.objects.update_or_create(
        user_id=user.pk, defaults={"product": product, "updated_at": timezone.now()}
    )


def without_product(query: QueryDict) -> str:
    """Строка адреса без выбора продукта и номера страницы: экран — под рабочий продукт.

    Продукт в адресах — `product` (у «Площадок», «Счетов», главной) и
    `…product__id__exact` (штатные фильтры по связи).
    """
    kept = [
        (key, value)
        for key, values in query.lists()
        if key not in ("product", "p") and not key.endswith("product__id__exact")
        for value in values
    ]
    return urlencode(kept)


class WorkingProductFilter(Remembering, admin.SimpleListFilter):
    """Фильтр «продукт»: по умолчанию «Все», дальше — последний выбор (E1-19).

    `field` — путь к продукту от строки списка; параметр адреса — как у
    штатного фильтра по связи (`product__id__exact`): старые ссылки и наборы
    «Моих фильтров» работают как раньше.
    """

    title = "продукт"
    parameter_name = "product__id__exact"
    field = "product"

    def __init__(
        self, request: HttpRequest, params: dict[str, Any], model: Any, model_admin: Any
    ) -> None:
        self.working = working_product_id(request)
        super().__init__(request, params, model, model_admin)

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [(str(pk), name) for pk, name, _ in products_of(request)]

    def value(self) -> str:
        value = self.last_choice()
        known = {lookup for lookup, _ in self.lookup_choices} | {ALL}
        if value is None or (self.url_value is None and value not in known):
            # Запомненный продукт могли удалить (ADR-036) — тогда снова «Все».
            # Мусор в адресе остаётся мусором: список на него покажет пусто.
            return ALL
        return value

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        value = self.value()
        self.keep(value)
        if value == ALL:
            return self.all_products(queryset)
        if not value.isdigit():
            return queryset.none()
        return self.of_product(queryset, int(value))

    def all_products(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        """Что показывать на «Все»: здесь — всё подряд, у рамочного — рабочий продукт."""
        return queryset

    def of_product(self, queryset: QuerySet[Any], product_id: int) -> QuerySet[Any]:
        return queryset.filter(**{f"{self.field}_id": product_id})

    def choices(self, changelist: Any) -> Iterator[Any]:
        # Как у штатного фильтра, только «Все» — своим значением, а не пустым.
        value = self.value()
        counts = self.get_facet_queryset(changelist) if changelist.add_facets else None
        yield {
            "selected": value == ALL,
            "query_string": changelist.get_query_string({self.parameter_name: ALL}),
            "display": "Все",
        }
        for i, (lookup, title) in enumerate(self.lookup_choices):
            if counts is not None:
                count = counts.get(f"{i}__c", -1)
                title = f"{title} ({count})" if count != -1 else f"{title} (-)"
            yield {
                "selected": value == str(lookup),
                "query_string": changelist.get_query_string({self.parameter_name: lookup}),
                "display": title,
            }


class FrameProductFilter(WorkingProductFilter):
    """Тот же фильтр там, где рабочий продукт — рамка работы (E1-19, ADR-063).

    Строки списка всегда под продуктом из шапки: «Площадки» и «Размещения»
    показывают, что с площадкой делаем мы под этот продукт. Выбор в фильтре
    рамку не меняет, а сужает список до площадок, где работал и выбранный
    продукт, — то есть ищет по колонке «другие продукты».

    `site_field` — путь к площадке от строки списка: по ней и ищем чужие
    размещения.
    """

    site_field = "site_id"

    def all_products(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        return self.framed(queryset)

    def of_product(self, queryset: QuerySet[Any], product_id: int) -> QuerySet[Any]:
        other = Placement.objects.filter(site_id=OuterRef(self.site_field), product_id=product_id)
        return self.framed(queryset).filter(Exists(other))

    def framed(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        if self.working is None:
            return queryset.none()
        return queryset.filter(**{f"{self.field}_id": self.working})


def product_filter(field: str) -> type[WorkingProductFilter]:
    """Фильтр продукта по пути `field` (`keyword__product`) — параметр `<путь>__id__exact`."""
    return type(
        "WorkingProductFilter",
        (WorkingProductFilter,),
        {"field": field, "parameter_name": f"{field}__id__exact"},
    )
