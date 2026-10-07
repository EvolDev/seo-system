"""Оценка площадки звёздочкой, 1–5 (E1-21, ADR-064).

Оценка общая для площадки: оценивается сама площадка как партнёр, а не
пара с продуктом. Одна строка на человека — передумал, строка
перезаписывается; история оценок не ведётся (решение пользователя
07.10.2026).

Среднее здесь не считается: его даёт `v_site_latest` одной группировкой
на все площадки. Тут только запись и своя оценка строки.
"""

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, OuterRef, QuerySet, Subquery
from django.db.models.functions import Round
from django.utils import timezone

from apps.sites.models import Seller, SellerRating, Site, SiteRating

MIN_VALUE = 1
MAX_VALUE = 5

#: Имя аннотации со своей оценкой — одно на все списки.
MINE = "my_rating"


def clean_value(raw: str | None) -> int | None:
    """Оценка из формы: 1–5, или None — «снять оценку».

    Снимает только пустое поле. Число вне 1–5 и нечисло — ошибка
    (`ValueError`), а не снятие: иначе опечатка молча стёрла бы оценку.
    """
    text = (raw or "").strip()
    if not text:
        return None
    value = int(text)
    if not MIN_VALUE <= value <= MAX_VALUE:
        raise ValueError(f"оценка вне {MIN_VALUE}–{MAX_VALUE}: {value}")
    return value


def set_rating(site: Site, user: Any, value: int | None) -> None:
    """Поставить оценку или снять её. Повторная оценка заменяет прежнюю."""
    if value is None:
        SiteRating.objects.filter(site=site, user=user).delete()
        return
    SiteRating.objects.update_or_create(
        site=site, user=user, defaults={"value": value, "updated_at": timezone.now()}
    )


def summary(site: Site) -> tuple[Decimal | None, int]:
    """Среднее и сколько оценок — после записи, чтобы ответить странице."""
    rows = SiteRating.objects.filter(site=site)
    count = rows.count()
    if not count:
        return None, 0
    average = rows.aggregate(value=Round(Avg("value"), 1))["value"]
    return average, count


def with_summary(queryset: QuerySet[Any], site_field: str = "site_id") -> QuerySet[Any]:
    """Среднее и счёт оценок для списка, где представления нет («Размещения»).

    Два скоррелированных подзапроса на строку. В «Площадках» так делать
    нельзя — там 45 000 строк каталога и своё представление, — а здесь на
    странице полсотни размещений рабочего продукта.
    """
    rows = SiteRating.objects.filter(site_id=OuterRef(site_field)).values("site_id")
    annotated: QuerySet[Any] = queryset.annotate(
        rating_avg=Subquery(rows.annotate(v=Round(Avg("value"), 1)).values("v")[:1]),
        rating_count=Subquery(rows.annotate(n=Count("*")).values("n")[:1]),
    )
    return annotated


def with_mine(queryset: QuerySet[Any], user: Any, site_field: str = "site_id") -> QuerySet[Any]:
    """Своя оценка строки — подзапросом: в представлении её нет и быть не может."""
    if not user.is_authenticated:
        return queryset
    mine = SiteRating.objects.filter(site_id=OuterRef(site_field), user=user)
    annotated: QuerySet[Any] = queryset.annotate(**{MINE: Subquery(mine.values("value")[:1])})
    return annotated


# --- Продавцы (E1-24) ---
#
# Устроено как у площадок, но проще: продавцов полсотни, представления у них
# нет, поэтому среднее и своя оценка считаются подзапросами прямо в списке.


def set_seller_rating(seller: Seller, user: Any, value: int | None) -> None:
    """Поставить оценку продавцу или снять её."""
    if value is None:
        SellerRating.objects.filter(seller=seller, user=user).delete()
        return
    SellerRating.objects.update_or_create(
        seller=seller, user=user, defaults={"value": value, "updated_at": timezone.now()}
    )


def seller_summary(seller: Seller) -> tuple[Decimal | None, int]:
    """Среднее и сколько оценок у продавца — ответить странице после записи."""
    rows = SellerRating.objects.filter(seller=seller)
    count = rows.count()
    if not count:
        return None, 0
    average = rows.aggregate(value=Round(Avg("value"), 1))["value"]
    return average, count


def with_seller_rating(queryset: QuerySet[Any], user: Any) -> QuerySet[Any]:
    """Среднее, число оценок и своя оценка — для списка продавцов."""
    rows = SellerRating.objects.filter(seller_id=OuterRef("pk")).values("seller_id")
    annotated: QuerySet[Any] = queryset.annotate(
        rating_avg=Subquery(rows.annotate(v=Round(Avg("value"), 1)).values("v")[:1]),
        rating_count=Subquery(rows.annotate(n=Count("*")).values("n")[:1]),
    )
    if not user.is_authenticated:
        return annotated
    mine = SellerRating.objects.filter(seller_id=OuterRef("pk"), user=user)
    result: QuerySet[Any] = annotated.annotate(**{MINE: Subquery(mine.values("value")[:1])})
    return result


#: Оценка продавца для показа: среднее, сколько оценок, моя.
type Mark = tuple[Any, int, int | None]


def seller_marks(seller_ids: Iterable[int], user: Any = None) -> dict[int, Mark]:
    """Оценки нескольких продавцов разом: id → (среднее, сколько, моя).

    Для таблицы предложений в карточке площадки: продавцов там единицы, но
    запрос на строку — всё равно запрос на строку.
    """
    ids = list(dict.fromkeys(seller_ids))
    if not ids:
        return {}
    rows = (
        SellerRating.objects.filter(seller_id__in=ids)
        .values("seller_id")
        .annotate(average=Round(Avg("value"), 1), total=Count("*"))
    )
    found = {row["seller_id"]: (row["average"], row["total"]) for row in rows}
    own: dict[int, int] = {}
    if user is not None and getattr(user, "is_authenticated", False):
        own = dict(
            SellerRating.objects.filter(seller_id__in=ids, user=user).values_list(
                "seller_id", "value"
            )
        )
    return {seller_id: (*found.get(seller_id, (None, 0)), own.get(seller_id)) for seller_id in ids}
