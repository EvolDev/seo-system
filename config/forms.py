"""Поля и виджеты форм под удобство (E9-11, ADR-048): для панели записи и полных форм.

- `ChoiceButtons` — выбор кнопками в ряд по порядку списка: статусы;
- `DayField` — день без времени для поля-момента модели, с кнопкой «Сегодня»;
- `MoneyField` — сумма в валюте («120,50»), в базе — целые центы.

Вид — `seo/widgets.css`, кнопки «Сегодня» и подстановка даты по статусу —
`seo/widgets.js`; оба файла приходят с формой (Media виджета), и на полной
странице, и в панели.
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar

from django import forms
from django.forms.utils import flatatt
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

from config.assets import Css, Js

CENT = Decimal("0.01")


class _WidgetMedia:
    js = (Js("seo/widgets.js"),)
    css: ClassVar[dict[str, tuple[Css, ...]]] = {"all": (Css("seo/widgets.css"),)}


class ChoiceButtons(forms.RadioSelect):
    """Выбор из списка — кнопками в ряд, в порядке списка.

    Это обычные радиокнопки: отправка формы, Tab и стрелки внутри ряда,
    подсказки ошибок — штатные. rows — значения, с которых начинается новый
    ряд (отказы у статусов площадки). fills — какое поле даты заполнить
    сегодняшним днём, если оно пустое, при выборе значения: у размещения
    «Заявка отправлена» → дата заявки.
    """

    Media = _WidgetMedia

    def __init__(
        self,
        attrs: dict[str, Any] | None = None,
        choices: Iterable[tuple[Any, Any]] = (),
        rows: Iterable[str] = (),
        fills: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(attrs, choices)
        self.rows = frozenset(rows)
        self.fills = dict(fills or {})

    def render(
        self, name: str, value: Any, attrs: dict[str, Any] | None = None, renderer: Any = None
    ) -> SafeString:
        widget = self.get_context(name, value, attrs)["widget"]
        rows: list[list[SafeString]] = [[]]
        for _group, options, _index in widget["optgroups"]:
            for option in options:
                if str(option["value"]) in self.rows and rows[-1]:
                    rows.append([])
                rows[-1].append(self._button(option))
        return format_html(
            '<div class="seo-choice" role="radiogroup"{}>{}</div>',
            flatatt({"id": widget["attrs"].get("id")} if widget["attrs"].get("id") else {}),
            format_html_join(
                "",
                '<div class="seo-choice-row">{}</div>',
                ((SafeString("".join(row)),) for row in rows),
            ),
        )

    def _button(self, option: dict[str, Any]) -> SafeString:
        attrs = dict(option["attrs"])
        target = self.fills.get(str(option["value"]))
        if target:
            attrs["data-fills"] = target
        return format_html(
            '<label class="seo-choice-item"><input type="radio" name="{}" value="{}"{}>'
            "<span>{}</span></label>",
            option["name"],
            option["value"],
            flatatt(attrs),
            option["label"],
        )


class DayInput(forms.DateInput):
    """Поле даты браузера (календарь и ввод с клавиатуры) и кнопка «Сегодня»."""

    input_type = "date"
    Media = _WidgetMedia

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        # Поле даты браузера принимает только ГГГГ-ММ-ДД, показывает — как в системе.
        super().__init__(attrs, format="%Y-%m-%d")

    def render(
        self, name: str, value: Any, attrs: dict[str, Any] | None = None, renderer: Any = None
    ) -> SafeString:
        return format_html(
            '<span class="seo-day">{}<button type="button" class="seo-btn" data-today>'
            "Сегодня</button></span>",
            super().render(name, value, attrs, renderer),
        )


def local_day(value: Any) -> Any:
    """Момент в базе — день по времени проекта; прочее — как есть."""
    if isinstance(value, dt.datetime):
        return timezone.localdate(value) if timezone.is_aware(value) else value.date()
    return value


def start_of_day(day: dt.date) -> dt.datetime:
    """Начало дня по времени проекта — как дату пишет импорт таблицы."""
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


class DayField(forms.DateField):
    """День без времени для поля-момента модели (DateTimeField).

    Время и предупреждение о часовом поясе в форме не нужны: важен день.
    Новый день пишется началом дня по времени проекта; день не меняли —
    момент в базе остаётся прежним вместе со временем (`DayFieldsForm`).
    """

    widget = DayInput

    def prepare_value(self, value: Any) -> Any:
        return local_day(value)

    def has_changed(self, initial: Any, data: Any) -> bool:
        try:
            return bool(local_day(initial) != self.to_python(data))
        except forms.ValidationError:
            return True


class DayFieldsForm(forms.ModelForm):  # type: ignore[type-arg]
    """Форма модели, где у полей `DayField` день превращается в момент для базы."""

    def clean(self) -> dict[str, Any] | None:
        cleaned = super().clean()
        if cleaned is None:
            return None
        for name, field in self.fields.items():
            if isinstance(field, DayField) and name in cleaned:
                cleaned[name] = self._moment(cleaned[name], getattr(self.instance, name, None))
        return cleaned

    @staticmethod
    def _moment(day: dt.date | None, before: dt.datetime | None) -> dt.datetime | None:
        if day is None:
            return None
        if before is not None and local_day(before) == day:
            return before
        return start_of_day(day)


class MoneyField(forms.DecimalField):
    """Сумма в валюте: человек пишет «120,50» или «120.50», в базе — 12050 центов."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("min_value", 0)
        kwargs.setdefault("localize", True)
        super().__init__(**kwargs)

    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, int):
            return (Decimal(value) / 100).quantize(CENT)
        return value

    def clean(self, value: Any) -> int | None:
        amount = super().clean(value)
        if amount is None:
            return None
        return int((Decimal(amount) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def has_changed(self, initial: Any, data: Any) -> bool:
        try:
            return bool(initial != self.clean(data))
        except forms.ValidationError:
            return True
