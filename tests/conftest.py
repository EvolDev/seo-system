from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
from django.core.cache import cache
from django.test import override_settings
from openpyxl import Workbook
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from config.celery import app as celery_app
from config.sentry import disable_sentry, sentry_options

# Адрес не резолвится: даже если транспорт не подменится, в сеть ничего не уйдёт.
FAKE_DSN = "https://public@sentry.invalid/1"

Events = list[dict[str, Any]]


@pytest.fixture(autouse=True, scope="session")
def celery_eager() -> Iterator[None]:
    """Задачи выполняются сразу, в процессе теста, без воркера (E2-01).

    В `.env` запущенного приложения CELERY_TASK_ALWAYS_EAGER=false — тесты
    от него не зависят. Повторы в этом режиме идут сразу, без пауз; ошибку
    задачи после всех попыток поднимает `.get()` у результата.

    Ключи — с префиксом CELERY_, как в настройках Django: Celery ищет
    значение сначала по нему, и ключ без префикса (`task_always_eager`)
    настройку не перекрыл бы. Брокер — в памяти процесса: задача, ушедшая
    мимо eager, не попадёт в Redis запущенного приложения к его воркеру.
    """
    celery_app.conf.update(CELERY_TASK_ALWAYS_EAGER=True, CELERY_BROKER_URL="memory://")
    yield


@pytest.fixture(autouse=True, scope="session")
def memory_cache() -> Iterator[None]:
    """Кеш — в памяти процесса, а не в Redis запущенного приложения (E2-02).

    Иначе тест получил бы из кеша выдачу, сохранённую приложением, или
    оставил бы в нём свою.
    """
    memory = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    with override_settings(CACHES=memory):
        yield


@pytest.fixture(autouse=True)
def empty_cache(memory_cache: None) -> Iterator[None]:
    """Каждый тест начинает с пустого кеша."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True, scope="session")
def no_real_sentry() -> Iterator[None]:
    """Тесты не шлют события в настоящий Sentry, даже если в `.env` задан SENTRY_DSN.

    Настройки уже вызвали `init_sentry()`; повторный `init` с пустым DSN
    заменяет клиент на такой, у которого нет транспорта.
    """
    disable_sentry()
    yield


class _CaptureTransport(Transport):
    """Складывает события в список вместо отправки."""

    def __init__(self, events: Events) -> None:
        super().__init__()
        self.events = events

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(dict(event))


@pytest.fixture
def sentry_events() -> Iterator[Events]:
    """Sentry включён с настройками проекта, события попадают в этот список."""
    events: Events = []
    sentry_sdk.init(**sentry_options(FAKE_DSN, "test"), transport=_CaptureTransport(events))
    yield events
    disable_sentry()


# --- Импорт таблицы (E1-04): книга Excel, которую тест собирает сам ---

BASE_HEADERS = [
    "Target",
    "Новая?",
    "Источник",
    "Комментарий к площадке",
    "URL статьи",
    "Индексация",
    "Статус",
    "Дата размещения",
    "Месяц",
    "Комментарий по размещению",
    "Анкор1",
    "Ссылка1",
    "Анкор2",
    "Ссылка2",
    "Пример статьи на Clideo",
    "Organic / Traffic",
    "Top Geo",
    "Top Geo Traff",
    "US Traff",
    "DR",
    "Organic / Total Keywords",
    "Тип ссылки",
    "Цена размещения статья, EUR",
    "Цена анонса статья, EUR",
    "Цена написания статья, EUR",
    "Итог цена",
    "Количество ссылок статья",
    "Пометка о рекламе статья",
    "Особые тематики",
    "Языки сайта",
    "Тип сайта",
    "URL Коллаборатора",
    "Тематика",
    "Тип ссылки статья",
]

KEYWORD_HEADERS = [
    "Keyword",
    "URL",
    "Volume",
    "Global Volume",
    "Links Placed",
    "Links Waiting",
    "Pos 22.09.26",
    "Pos 16.09.26",
    "Pos 09.09.26",
    "Pos 02.09.26",
    "Tool",
    "Type",
]

Row = dict[str, object]
# Строка книги в тесте: («домен» или «ключ», изменения к типовой строке)
# или словарь целиком — например, строка без Target.
RowSpec = tuple[str, Row] | Row


def site_row(domain: str, changes: Row | None = None) -> Row:
    """Строка основной вкладки с правдоподобными значениями, как в файле 27.09."""
    row: Row = {
        "Target": domain,
        "Источник": "Collaborator",
        "Organic / Traffic": 96653,
        "Top Geo": "us",
        "Top Geo Traff": 60990,
        "US Traff": 60990,
        "DR": 55,
        "Organic / Total Keywords": 15707,
        "Цена размещения статья, EUR": 544.57,
        "Цена анонса статья, EUR": "",
        "Цена написания статья, EUR": 40.84,
        "Итог цена": 581.82,
        "Количество ссылок статья": 1,
        "Пометка о рекламе статья": "Нет",
        "Особые тематики": "",
        "Языки сайта": "Английский",
        "Тип сайта": "Персональный блог",
        "URL Коллаборатора": f"https://collaborator.pro/ru/creator/article/view?id={domain}",
        "Тематика": "Культура и искусство",
        "Тип ссылки статья": "dofollow",
    }
    row.update(changes or {})
    return row


def keyword_row(keyword: str, changes: Row | None = None) -> Row:
    row: Row = {
        "Keyword": keyword,
        "URL": "https://convertio.co/",
        "Volume": 42000,
        "Global Volume": 363000,
        "Pos 22.09.26": 7,
        "Pos 16.09.26": 14,
        "Pos 09.09.26": 5,
        "Pos 02.09.26": 7,
        "Tool": "Main",
        "Type": "Главная",
    }
    row.update(changes or {})
    return row


def _rows(specs: list[RowSpec] | None, factory: Callable[[str, Row | None], Row]) -> list[Row]:
    return [factory(*spec) if isinstance(spec, tuple) else spec for spec in specs or []]


@pytest.fixture
def make_workbook(tmp_path: Path) -> Callable[..., Path]:
    """Собирает xlsx с вкладками «База линкбилдинга», «Распределение анкоров»
    и, если передана, «Размещения»; возвращает путь к файлу.

    Строки основной вкладки и копии — `("домен", {колонка: значение})`,
    ключи — `("ключ", {…})`: к типовой строке применяются изменения.
    """

    def build(
        base: list[RowSpec] | None = None,
        keywords: list[RowSpec] | None = None,
        copy: list[RowSpec] | None = None,
        base_headers: list[str] | None = None,
        name: str = "book.xlsx",
    ) -> Path:
        workbook = Workbook()
        workbook.remove(workbook.active)  # type: ignore[arg-type]
        sheets: list[tuple[str, list[str], list[Row]]] = [
            ("База линкбилдинга", base_headers or BASE_HEADERS, _rows(base, site_row)),
            ("Распределение анкоров", KEYWORD_HEADERS, _rows(keywords, keyword_row)),
        ]
        if copy is not None:
            sheets.append(("Размещения", BASE_HEADERS, _rows(copy, site_row)))
        for title, headers, rows in sheets:
            sheet = workbook.create_sheet(title)
            sheet.append(headers)
            for row in rows:
                sheet.append([row.get(header) for header in headers])
        path = tmp_path / name
        workbook.save(path)
        return path

    return build


# Предложение продавца в тестах (E1-07, ADR-043). Продавец по умолчанию —
# Collaborator: его заводит миграция `sites.0007`, в тестовой базе он есть.
OfferFactory = Callable[..., Any]


@pytest.fixture
def offer(db: None) -> OfferFactory:
    """Создаёт предложение: `offer(site, 24000, seller=..., working=True, **поля)`.

    `working` — сразу рабочая цена площадки, без заметки в истории (как
    первая цена при импорте); такое предложение уже разобрано.
    """
    # Модели — внутри: conftest читается до того, как Django готов.
    from django.utils import timezone

    from apps.sites.models import Seller, Site, SitePrice

    def make(
        site: Any,
        cents: int | None = 10000,
        *,
        seller: Any = None,
        working: bool = True,
        **fields: Any,
    ) -> Any:
        if working:
            fields.setdefault("reviewed_at", timezone.now())
        price = SitePrice.objects.create(
            site=site, seller=seller or Seller.collaborator(), placement_cents=cents, **fields
        )
        if working:
            Site.all_objects.filter(pk=site.pk).update(price=price)
            site.price = price
        return price

    return make
