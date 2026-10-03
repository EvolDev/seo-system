"""Наборы фильтров списков админки (E9-10, ADR-050).

Набор — строка адреса списка: всё, что выбрано в колонке фильтров, поиск,
регион и сортировка уже в ней. Применить набор — перейти по адресу, поэтому
новый фильтр списка попадает в наборы без правки кода.
"""

from urllib.parse import parse_qsl, urlencode

from django.db.models import QuerySet
from django.db.models.functions import Lower

from apps.workspace.models import SavedFilter

# Чего в наборе не бывает: номер страницы списка (набор открывается с первой),
# служебные метки админки — ошибка в параметрах, окно выбора связанной записи.
DROPPED = frozenset({"p", "e", "_popup", "_to_field", "_changelist_filters"})

# Длинное название не помещается в колонку фильтров.
NAME_MAX = 80


def clean_query(raw: str) -> str:
    """Строка адреса без номера страницы, служебных меток и пустых полей.

    Пустое поле — это «ничего не выбрано»: пустой поиск или незаполненное
    «С — До» после «Применить». В наборе оно только мешает узнать набор.
    """
    pairs = parse_qsl(raw.lstrip("?"), keep_blank_values=True)
    return urlencode([(key, value) for key, value in pairs if key not in DROPPED and value])


def same_query(left: str, right: str) -> bool:
    """Одни и те же фильтры, в каком бы порядке они ни стояли в адресе."""
    return sorted(parse_qsl(clean_query(left))) == sorted(parse_qsl(clean_query(right)))


def screen_of(app_label: str, model_name: str) -> str:
    return f"{app_label}.{model_name}"


def sets_of(user_id: int, screen: str) -> QuerySet[SavedFilter]:
    """Наборы пользователя на списке, без удалённых, по алфавиту."""
    return SavedFilter.objects.filter(
        user_id=user_id, screen=screen, deleted_at__isnull=True
    ).order_by(Lower("name"), "pk")


def as_json(sets: QuerySet[SavedFilter]) -> list[dict[str, object]]:
    return [{"id": item.pk, "name": item.name, "query": item.query} for item in sets]
