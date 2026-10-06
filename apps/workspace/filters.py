"""Память выбора фильтров у пользователя (E1-19, ADR-063).

Фильтр без параметра в адресе открывается тем, что человек выбрал на этом
экране в прошлый раз; не выбирал ни разу — значением по умолчанию самого
фильтра. Выбор лежит в `user_settings.filters`: экран → параметр адреса →
значение. Экран — «приложение.модель», как у наборов «Моих фильтров»
(ADR-050), память у каждого экрана своя.

В базу пишем, только когда значение изменилось: это щелчок по фильтру, а не
каждая страница списка. Этим память и отличается от «Как в прошлый раз»
(ADR-050), которое пишет в браузер всю строку адреса на каждый переход.
"""

from typing import Any

from django.http import HttpRequest
from django.utils import timezone

from apps.workspace.models import UserSettings
from apps.workspace.saved_filters import screen_of

_CACHE = "_seo_user_settings"


def settings_of(request: HttpRequest) -> UserSettings | None:
    """Настройки того, кто смотрит; строки ещё нет — None.

    Один запрос на страницу: его спрашивают рабочий продукт и каждый фильтр
    с памятью.
    """
    if hasattr(request, _CACHE):
        cached: UserSettings | None = getattr(request, _CACHE)
        return cached
    user = getattr(request, "user", None)
    found = None
    if user is not None and user.is_authenticated:
        found = UserSettings.objects.filter(user_id=user.pk).first()
    setattr(request, _CACHE, found)
    return found


def remembered(request: HttpRequest, screen: str, param: str) -> str | None:
    """Что человек выбрал этим фильтром на этом экране в прошлый раз."""
    found = settings_of(request)
    if found is None:
        return None
    chosen: Any = found.filters.get(screen, {})
    value = chosen.get(param) if isinstance(chosen, dict) else None
    return value if isinstance(value, str) else None


def remember(request: HttpRequest, screen: str, param: str, value: str) -> None:
    """Запоминает выбор; то же самое значение второй раз в базу не идёт."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return
    if remembered(request, screen, param) == value:
        return
    found = settings_of(request)
    filters = dict(found.filters) if found is not None else {}
    screen_filters = filters.get(screen)
    chosen = dict(screen_filters) if isinstance(screen_filters, dict) else {}
    chosen[param] = value
    filters[screen] = chosen
    # Строка настроек заводится при первом выборе — как у рабочего продукта.
    saved, _ = UserSettings.objects.update_or_create(
        user_id=user.pk, defaults={"filters": filters, "updated_at": timezone.now()}
    )
    setattr(request, _CACHE, saved)


class Remembering:
    """Примесь к фильтру списка: без параметра в адресе — последний выбор.

    Фильтр с этой примесью сам решает, что считать выбором: `keep()` зовётся
    из его `queryset()` только с тем значением, которое фильтр признал годным.
    """

    parameter_name: str

    def __init__(
        self, request: HttpRequest, params: dict[str, Any], model: Any, model_admin: Any
    ) -> None:
        self.request = request
        self.screen = screen_of(model._meta.app_label, model._meta.model_name)
        super().__init__(request, params, model, model_admin)  # type: ignore[call-arg]
        # Что пришло в адресе — запоминаем сразу: дальше фильтр подставляет в
        # `used_parameters` значение по умолчанию, а счёт в скобках у каждого
        # пункта («фасеты») подставляет туда же своё.
        chosen = self.used_parameters.get(self.parameter_name)  # type: ignore[attr-defined]
        self.url_value = str(chosen) if chosen else None

    def last_choice(self) -> str | None:
        """Выбор из адреса, а без него — запомненный на этом экране."""
        value = super().value()  # type: ignore[misc]
        if value:
            return str(value)
        return remembered(self.request, self.screen, self.parameter_name)

    def keep(self, value: str) -> None:
        """Запомнить выбор — только если он пришёл из адреса, то есть щелчком."""
        if self.url_value == value:
            remember(self.request, self.screen, self.parameter_name, value)
