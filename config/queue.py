"""Базовый класс фоновых задач (E2-01, ADR-039).

Задача объявляется так:

    @shared_task(base=QueueTask, name="check_indexation")
    def check_indexation(placement_id: int) -> None: ...

и ни о чём ниже не помнит — это делает базовый класс.

**run_id** (ADR-025). Кто ставит задачу в очередь внутри `bind_run_id`,
передаёт ей свой `run_id`: класс дописывает его в аргументы задачи.
Воркер выполняет задачу внутри `bind_run_id`. Нет `run_id` (задачу
поставил beat) — задача сама точка входа цепочки, `run_id` новый.
Следующую задачу ставят из тела предыдущей, и `run_id` переходит сам.
Цепочки Celery (`chain`, `group`) его не передают: следующее звено Celery
отправляет уже после выхода из задачи.

**Журнал `task_runs`** — одна строка на задачу, а не на попытку. Первая
попытка создаёт строку «Выполняется», повторы её дописывают, в конце —
«Успешно» или «Ошибка» с текстом. В `payload` — id задачи Celery,
аргументы (секреты замаскированы), номер попытки, ошибки прежних попыток
и пометка, если задача возвращалась в очередь после остановки воркера.

**Повторы.** Любое исключение — повтор через `FIRST_RETRY_DELAY` секунд,
каждый следующий — вдвое позже, всего `MAX_ATTEMPTS` попыток. После
последней — «Ошибка» и запись ERROR в лог; интеграция Sentry делает из неё
событие — это и есть алерт (ADR-011). Промежуточные неудачи — WARNING.
Свои паузы и число попыток задача задаёт параметрами `retry_backoff` и
`max_retries` декоратора.

**Ограничение скорости** — атрибут `throttle` (`config/throttle.py`).
Ключ занят — задача откладывается на остаток интервала: попытка не
тратится, строки в журнале нет, пока задача не начала работу.

**Пауза** — задача бросает `Postpone(until, reason)`, когда продолжать
пока нельзя: например, исчерпан дневной лимит платного API (E2-02).
Попытка не тратится, строка журнала остаётся «Выполняется», в `payload`
— `waiting`: до какого времени и почему. Задача уходит в очередь заново,
но не дольше чем на `MAX_POSTPONE`: Redis отдаёт снова сообщение, не
подтверждённое за `visibility_timeout`, и задача, отложенная до утра,
выполнилась бы по разу в час. Проснувшись раньше срока, задача сама
проверит условие и при необходимости снова встанет на паузу.

Задачи обязаны быть идемпотентными: сообщение подтверждается после
выполнения (`acks_late`), и задачу, прерванную остановкой воркера, Redis
отдаст снова — она выполнится ещё раз с начала.
"""

import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from numbers import Real
from typing import TYPE_CHECKING, Any, NoReturn

from celery.contrib.django.task import DjangoTask
from celery.exceptions import Ignore, Retry
from celery.result import AsyncResult
from django.utils import timezone

from config.logs import mask_secrets, redact
from config.run_id import bind_run_id, current_run_id, new_run_id
from config.throttle import Throttle

if TYPE_CHECKING:
    from apps.observability.models import TaskRun

# В стабах celery-types класс задачи параметризуется аргументами и результатом:
# DjangoTask[P, R]. Сам Celery так писать не даёт — при запуске класс обычный.
# TYPE_CHECKING истинно только для mypy (тот же приём — в config/admin.py).
if TYPE_CHECKING:
    _DjangoTask = DjangoTask[Any, Any]
else:
    _DjangoTask = DjangoTask

logger = logging.getLogger(__name__)

# Имя аргумента, в котором run_id едет через очередь.
RUN_ID_ARG = "run_id"
MAX_ATTEMPTS = 3
# Пауза перед первым повтором, секунд; перед каждым следующим — вдвое больше.
FIRST_RETRY_DELAY = 60
MAX_RETRY_DELAY = 10 * 60
# Самая длинная пауза за раз, секунд: меньше visibility_timeout (settings), см.
# описание модуля.
MAX_POSTPONE = 30 * 60
# Строку прежней попытки ищем среди запусков за это время: повтор приходит
# через минуты, задача жёстко убитого воркера — через час (visibility_timeout).
_LOOKBACK = timedelta(days=2)


class Postpone(Exception):
    """Задаче рано продолжать: запуск ждёт до `until`, попытка не тратится.

    `reason` — по-человечески, его видно в журнале запусков.
    """

    def __init__(self, until: datetime, reason: str) -> None:
        super().__init__(reason)
        self.until = until
        self.reason = reason

    def __reduce__(self) -> tuple[type["Postpone"], tuple[datetime, str]]:
        # Celery сохраняет ошибку задачи через pickle, а тот по умолчанию
        # пересоздаёт исключение из args — здесь в них только reason.
        return (type(self), (self.until, self.reason))


class QueueTask(_DjangoTask):
    """Базовый класс наших задач; что он делает — в описании модуля."""

    # Повторы делает Celery (autoretry): исключение из тела задачи → retry().
    autoretry_for: tuple[type[BaseException], ...] = (Exception,)
    # Пауза — не ошибка: её не повторяем, а откладываем (_pause).
    dont_autoretry_for: tuple[type[BaseException], ...] = (Postpone,)
    max_retries = MAX_ATTEMPTS - 1
    retry_backoff: bool | int = FIRST_RETRY_DELAY
    retry_backoff_max = MAX_RETRY_DELAY
    # Случайная пауза от нуля до расчётной могла бы повторить задачу сразу.
    retry_jitter = False
    # run_id нет в аргументах функции задачи, и проверка аргументов Celery при
    # постановке в очередь на нём падала бы — проверяем сами (_with_run_id).
    typing = False
    # Ограничение скорости (config/throttle.py); None — без ограничения.
    throttle: Throttle | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        run_id = str(kwargs.pop(RUN_ID_ARG, None) or current_run_id() or new_run_id())
        self._remember_run_id(run_id)
        with bind_run_id(run_id):
            self._pass_throttle(args, kwargs)
            run = _Run.start(self, args, kwargs)
            try:
                # Тело задачи — напрямую: Task.__call__ подменил бы контекст
                # запроса пустым, и повторы перестали бы работать.
                result = self.run(*args, **kwargs)
            except Retry as retry:
                run.attempt_failed(retry)
                raise
            except Postpone as pause:
                self._pause(run, pause)
            except Ignore:
                # Задача сама решила остановиться — это не ошибка.
                run.succeeded()
                raise
            except Exception as error:
                run.failed(error)
                raise
            run.succeeded()
            return result

    def apply_async(
        self,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        *more: Any,
        **options: Any,
    ) -> "AsyncResult[Any]":
        """Ставит задачу в очередь; `run_id` текущей цепочки уходит вместе с ней.

        Аннотация в кавычках: AsyncResult параметризуется только в стабах.
        """
        return super().apply_async(args, self._with_run_id(args, kwargs), *more, **options)

    def apply_async_on_commit(  # type: ignore[override]
        self,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        *more: Any,
        **options: Any,
    ) -> None:
        """Ставит задачу в очередь после фиксации транзакции (`DjangoTask` Celery).

        `run_id` берём сейчас: к фиксации блок `bind_run_id` может уже
        закончиться. В стабах celery-types метод возвращает AsyncResult,
        на деле — None.
        """
        super().apply_async_on_commit(args, self._with_run_id(args, kwargs), *more, **options)

    def delay_on_commit(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        """`delay()` после фиксации транзакции; `run_id` — в момент вызова."""
        self.apply_async_on_commit(args, kwargs)

    def _with_run_id(
        self, args: Sequence[Any] | None, kwargs: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Аргументы задачи для очереди: с `run_id` цепочки, если он есть.

        Явно переданный `run_id` главнее текущего. Аргументы сверяются с
        функцией задачи, как у Celery с typing=True: ошибка — при постановке
        в очередь, а не в воркере.
        """
        result = dict(kwargs or {})
        run_id = result.pop(RUN_ID_ARG, None) or current_run_id()
        check = getattr(self, "__header__", None)
        if check is not None:
            check(*(args or ()), **result)
        if run_id is not None:
            result[RUN_ID_ARG] = str(run_id)
        return result

    def _remember_run_id(self, run_id: str) -> None:
        """Кладёт `run_id` в аргументы запроса, из которых Celery строит повтор.

        Задача от beat приходит без `run_id` и получает новый. Повтор и
        перенос (`signature_from_request`) берут аргументы из запроса — без
        этой записи вторая попытка получила бы ещё один новый `run_id`.
        Вызов функцией напрямую, без очереди, идёт с общим пустым запросом —
        его не трогаем.
        """
        request = self.request
        if request.called_directly:
            return
        if (request.kwargs or {}).get(RUN_ID_ARG) != run_id:
            request.kwargs = {**(request.kwargs or {}), RUN_ID_ARG: run_id}

    def _pass_throttle(self, args: Sequence[Any], kwargs: Mapping[str, Any]) -> None:
        """Занимает ключ ограничения скорости или откладывает задачу."""
        if self.throttle is None:
            return
        key = self.throttle.key_for(args, kwargs)
        if key is None:
            return
        wait = self.throttle.acquire(key)
        if not wait:
            return
        if self.request.is_eager or self.request.called_directly:
            # Без очереди задачу отложить некуда — ждём на месте.
            while wait:
                time.sleep(wait)
                wait = self.throttle.acquire(key)
            return
        self._postpone(key, wait)

    def _postpone(self, key: str, seconds: float) -> NoReturn:
        logger.info(
            "задача отложена ограничением скорости",
            extra={"task": self.name, "key": key, "seconds": round(seconds, 3)},
        )
        self._send_later(seconds)

    def _pause(self, run: "_Run", pause: Postpone) -> NoReturn:
        """Задача попросила подождать: откладываем запуск, строка журнала ждёт."""
        if self.request.is_eager or self.request.called_directly:
            # Без очереди отложить некуда, а ждать до утра на месте нельзя:
            # запуск заканчивается ошибкой с причиной паузы.
            run.failed(pause)
            raise pause
        run.waiting(pause)
        seconds = min(max((pause.until - timezone.now()).total_seconds(), 1), MAX_POSTPONE)
        logger.info(
            "задача на паузе: %s",
            pause.reason,
            extra={"task": self.name, "until": pause.until.isoformat(), "seconds": seconds},
        )
        self._send_later(seconds)

    def _send_later(self, seconds: float) -> NoReturn:
        # Та же задача — тот же id, аргументы с run_id, номер попытки, — но позже.
        # Ignore подтверждает текущее сообщение без записи об ошибке.
        self.signature_from_request(countdown=seconds).apply_async()
        raise Ignore()


class _Run:
    """Строка `task_runs` одной задачи: её создаёт первая попытка, дописывают повторы."""

    def __init__(self, task: QueueTask, row: "TaskRun") -> None:
        self.task = task
        self.row = row

    @classmethod
    def start(cls, task: QueueTask, args: Sequence[Any], kwargs: Mapping[str, Any]) -> "_Run":
        # Модели — внутри функций: config загружается вместе с Django, раньше,
        # чем модели готовы (как в config/admin_site.py).
        from apps.observability.models import TaskRun, TaskStatus

        request = task.request
        row = _running_row(task.name, request.id) if request.id else None
        if row is None:
            row = TaskRun(
                task_name=task.name,
                status=TaskStatus.RUNNING,
                # Время из Python, как и конец: now() базы внутри транзакции —
                # время её начала, длительность вышла бы неверной.
                started_at=timezone.now(),
                payload={
                    "task_id": request.id,
                    "args": _jsonable(redact(list(args))),
                    "kwargs": _jsonable(redact(dict(kwargs))),
                },
            )
        payload = dict(row.payload or {})
        payload["attempt"] = request.retries + 1
        # Пауза кончилась: задача снова работает.
        payload.pop("waiting", None)
        if (request.delivery_info or {}).get("redelivered"):
            # Сообщение вернулось в очередь после остановки воркера.
            payload["redelivered"] = True
        row.payload = payload
        row.save()
        return cls(task, row)

    @property
    def attempt(self) -> int:
        return int((self.row.payload or {}).get("attempt", 1))

    @property
    def attempts_total(self) -> int:
        return (self.task.max_retries or 0) + 1

    def attempt_failed(self, retry: Retry) -> None:
        """Попытка не удалась, Celery уже поставил повтор."""
        error = _error_text(retry.exc) if retry.exc is not None else "повтор по запросу задачи"
        payload = dict(self.row.payload or {})
        payload["errors"] = [*payload.get("errors", []), {"attempt": self.attempt, "error": error}]
        self.row.payload = payload
        self.row.save(update_fields=["payload"])
        logger.warning(
            "попытка %d из %d не удалась, повтор через %s",
            self.attempt,
            self.attempts_total,
            _delay_text(retry.when),
            extra={"task": self.task.name, "task_id": self.task.request.id, "error": error},
        )

    def waiting(self, pause: Postpone) -> None:
        """Запуск на паузе: строка остаётся «Выполняется», в payload — до когда и почему."""
        payload = dict(self.row.payload or {})
        payload["waiting"] = {"until": pause.until.isoformat(), "reason": pause.reason}
        self.row.payload = payload
        self.row.save(update_fields=["payload"])

    def succeeded(self) -> None:
        from apps.observability.models import TaskStatus

        self._finish(TaskStatus.SUCCESS, error=None)

    def failed(self, error: Exception) -> None:
        from apps.observability.models import TaskStatus

        self._finish(TaskStatus.FAILED, error=_error_text(error))
        # ERROR с исключением → событие в Sentry: это алерт «задача упала».
        # Интеграция Sentry с Celery ловит то же исключение ещё раз, дубль
        # отбрасывает её же дедупликация — событие одно, с нашим run_id.
        logger.error(
            "задача упала: %s, попыток %d",
            self.task.name,
            self.attempt,
            exc_info=error,
            extra={"task": self.task.name, "task_id": self.task.request.id},
        )

    def _finish(self, status: str, error: str | None) -> None:
        finished = timezone.now()
        self.row.status = status
        self.row.finished_at = finished
        self.row.duration_ms = int((finished - self.row.started_at).total_seconds() * 1000)
        self.row.error = error
        self.row.save(update_fields=["status", "finished_at", "duration_ms", "error"])


def _running_row(task_name: str, task_id: str) -> "TaskRun | None":
    """Незаконченная строка этой задачи: её прежняя попытка или прерванный запуск."""
    from apps.observability.models import TaskRun, TaskStatus

    return (
        TaskRun.objects.filter(
            task_name=task_name,
            status=TaskStatus.RUNNING,
            # Индекс idx_taskruns_name (task_name, started_at): смотрим только
            # недавние строки задачи, а не весь журнал.
            started_at__gte=timezone.now() - _LOOKBACK,
            payload__task_id=task_id,
        )
        .order_by("-started_at")
        .first()
    )


def _error_text(error: BaseException) -> str:
    """«ConnectionError: сообщение» — без трассировки: она в логе и в Sentry."""
    message = str(error)
    text = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return mask_secrets(text)


def _delay_text(when: Real | datetime | None) -> str:
    """Пауза до повтора: Celery даёт секунды или время запуска."""
    if isinstance(when, datetime):
        return f"{(when - timezone.now()).total_seconds():.0f} с"
    return f"{float(when or 0):g} с"


def _jsonable(value: Any) -> Any:
    """Аргументы для JSON-колонки: то, чего JSON не знает (дата, объект), — строкой.

    Через очередь аргументы и так идут JSON-ом; другое бывает только при
    запуске без очереди — в тестах и в режиме eager.
    """
    return json.loads(json.dumps(value, default=str))
