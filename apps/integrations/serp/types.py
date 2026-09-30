"""Что возвращает поиск по выдаче и чем он может закончиться (E2-02).

Типы общие для всех провайдеров: вызывающий код не знает, чья выдача.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from config.queue import Postpone


@dataclass(frozen=True)
class SerpResult:
    """Один органический результат выдачи."""

    position: int
    url: str
    domain: str
    title: str
    snippet: str


@dataclass(frozen=True)
class SerpPage:
    """Страница выдачи по запросу.

    `total_estimate` — оценка Google «примерно N результатов»; неточная
    (E2-06), и не каждый провайдер её отдаёт — тогда None.
    """

    query: str
    depth: int
    country: str
    results: tuple[SerpResult, ...]
    total_estimate: int | None
    fetched_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Для кеша: только простые типы."""
        data = asdict(self)
        data["results"] = [asdict(result) for result in self.results]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SerpPage":
        return cls(
            **{
                **data,
                "results": tuple(SerpResult(**result) for result in data["results"]),
            }
        )


@dataclass(frozen=True)
class ProviderAnswer:
    """Ответ провайдера: выдача и сколько она стоила."""

    page: SerpPage
    endpoint: str
    # Кредиты или запросы провайдера — то, что он списал.
    units: int
    cost_cents: Decimal


class SerpProvider(Protocol):
    """Провайдер выдачи. Protocol — «утиный» интерфейс: класс подходит, если
    у него есть такие атрибут и метод, наследоваться не нужно."""

    name: str

    def fetch(self, query: str, depth: int, country: str) -> ProviderAnswer: ...


class SerpError(Exception):
    """Провайдер не дал выдачу: неверный ключ, кончились кредиты, сбой сети.

    Задача, которая искала, упадёт и повторится (config/queue.py).
    """


class SerpBudgetExceeded(Postpone):
    """Дневной лимит расходов на выдачу исчерпан: задача ждёт следующих суток."""

    def __init__(self, until: datetime, spent: Decimal, budget: int) -> None:
        super().__init__(
            until,
            f"дневной лимит на выдачу исчерпан: потрачено {spent} ¢ из {budget} ¢",
        )
        self.spent = spent
        self.budget = budget

    def __reduce__(self) -> Any:
        return (type(self), (self.until, self.spent, self.budget))


def next_midnight(now: datetime) -> datetime:
    """Начало следующих суток в часовом поясе `now`."""
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=now.tzinfo)
