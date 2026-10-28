"""Блокировка правки: площадку правит один человек (пользователь, 28.10.2026; ADR-077).

Что блокируется — ключ строки `edit_locks`:
- `site:<площадка>` — карточка площадки: цены и рабочая цена, серость, факты
  каталога, решения продуктов. Одна на площадку, для всех продуктов;
- `placements:<площадка>:<продукт>` — размещения продукта на площадке: правка,
  новое размещение, удаление. Только внутри продукта: размещения другого
  продукта и так открываются только для просмотра (ADR-073).

Блокировку берёт тот, кто открыл запись с правом её менять (`claim` в админке:
пользователь 28.10.2026 — «если он зашёл в эту карточку, которую трогает в
данный момент другой пользователь»), а ещё правка в форме, если своя
блокировка простояла (`seo/edit-lock.js`). Работа в записи (`touch`) продлевает
её, закрытие панели снимает (`release`). Простой дольше настройки `EDIT_LOCK` —
блокировка не считается: следующее открытие или правка забирает её.
Заметка и своя звёздочка блокировку не берут и под ней разрешены — они ничего
не перетирают. Загрузки файлов и задачи очереди блокировку не смотрят: это не
человек, который правит рядом.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils import timezone

from apps.content.domain_settings import edit_lock_minutes
from apps.sites.models import Product
from config.admin import is_partial

SITE = "site"
PLACEMENTS = "placements"
# Что уже узнали о блокировках за этот запрос: права проверяются по нескольку
# раз на страницу — база спрашивается один.
_CACHE = "_seo_edit_locks"


@dataclass(frozen=True)
class Holder:
    """Кто держит чужую блокировку: имя для окошка и с какого времени правит."""

    user_id: int
    name: str
    since: datetime

    def since_text(self) -> str:
        """«14:05» — сегодня, «27.10 14:05» — раньше."""
        local = timezone.localtime(self.since)
        if local.date() == timezone.localdate():
            return f"{local:%H:%M}"
        return f"{local:%d.%m %H:%M}"


def site_key(site_id: int) -> str:
    return f"{SITE}:{site_id}"


def placements_key(site_id: int, product_id: int) -> str:
    return f"{PLACEMENTS}:{site_id}:{product_id}"


def parse_key(key: str) -> tuple[str, int, int | None] | None:
    """Ключ → (вид, площадка, продукт); чужая строка — None."""
    parts = key.split(":")
    if len(parts) == 2 and parts[0] == SITE and parts[1].isdigit():
        return SITE, int(parts[1]), None
    if len(parts) == 3 and parts[0] == PLACEMENTS and parts[1].isdigit() and parts[2].isdigit():
        return PLACEMENTS, int(parts[1]), int(parts[2])
    return None


def what(key: str) -> str:
    """Что правят — началом фразы: «Площадку», «Размещения Convertio на этой площадке»."""
    parsed = parse_key(key)
    product_id = parsed[2] if parsed is not None else None
    if product_id is None:
        return "Площадку"
    name = Product.objects.filter(pk=product_id).values_list("name", flat=True).first()
    return f"Размещения {name or 'продукта'} на этой площадке"


def busy_text(key: str, holder: Holder) -> str:
    """«Площадку правит Анна с 14:05» — без точки: дальше — что это значит."""
    return f"{what(key)} правит {holder.name} с {holder.since_text()}"


# Взять или продлить одним запросом: строка ключа либо наша, либо простаивает
# дольше настройки — тогда переходит к нам с новым «правит с». Иначе условие
# WHERE не даёт её обновить, RETURNING пуст: блокировка чужая. Проверка и захват
# в одном операторе — двое, нажавшие одновременно, не получат её оба. ORM такой
# «вставь или обнови при условии» не умеет — отсюда SQL.
_TAKE = """
INSERT INTO edit_locks AS l (key, user_id, started_at, seen_at)
VALUES (%(key)s, %(user)s, now(), now())
ON CONFLICT (key) DO UPDATE SET
    user_id = EXCLUDED.user_id,
    started_at = CASE
        WHEN l.user_id = EXCLUDED.user_id
         AND l.seen_at >= now() - make_interval(mins => %(idle)s)
        THEN l.started_at ELSE now() END,
    seen_at = now()
WHERE l.user_id = EXCLUDED.user_id
   OR l.seen_at < now() - make_interval(mins => %(idle)s)
RETURNING l.user_id
"""

# Живые блокировки ключей: простой не дольше настройки.
_ALIVE = """
SELECT key, user_id, started_at FROM edit_locks
WHERE key = ANY(%(keys)s) AND seen_at >= now() - make_interval(mins => %(idle)s)
"""

_TOUCH = """
UPDATE edit_locks SET seen_at = now()
WHERE key = %(key)s AND user_id = %(user)s
  AND seen_at >= now() - make_interval(mins => %(idle)s)
"""


def _names(user_ids: Iterable[int]) -> dict[int, str]:
    users = get_user_model().objects.filter(pk__in=set(user_ids))
    return {user.pk: user.get_full_name() or user.get_username() for user in users}


def _alive(keys: list[str]) -> dict[str, tuple[int, datetime]]:
    if not keys:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(_ALIVE, {"keys": keys, "idle": edit_lock_minutes()})
        return {key: (user_id, started) for key, user_id, started in cursor.fetchall()}


def holders(user: Any, keys: Iterable[str]) -> dict[str, Holder]:
    """Чужие живые блокировки ключей; своих и свободных в ответе нет."""
    alive = {
        key: row for key, row in _alive(list(dict.fromkeys(keys))).items() if row[0] != user.pk
    }
    names = _names(user_id for user_id, _ in alive.values())
    return {
        key: Holder(user_id, names.get(user_id, "другой пользователь"), started)
        for key, (user_id, started) in alive.items()
    }


def state(user: Any, key: str) -> tuple[Holder | None, bool]:
    """Чужая блокировка ключа (или None) и держит ли его сам пользователь."""
    row = _alive([key]).get(key)
    if row is None:
        return None, False
    if row[0] == user.pk:
        return None, True
    return holders(user, [key]).get(key), False


def take(user: Any, key: str) -> Holder | None:
    """Взять или продлить блокировку. None — она наша; иначе — кто держит."""
    found: Holder | None = None
    # Вторая попытка — если чужую успели снять между захватом и вопросом «чья».
    for _ in range(2):
        with connection.cursor() as cursor:
            cursor.execute(_TAKE, {"key": key, "user": user.pk, "idle": edit_lock_minutes()})
            if cursor.fetchone() is not None:
                return None
        found = holders(user, [key]).get(key)
        if found is not None:
            return found
    return found


def touch(user: Any, key: str) -> bool:
    """Продлить свою живую блокировку; False — её уже нет (простой, снята, чужая)."""
    with connection.cursor() as cursor:
        cursor.execute(_TOUCH, {"key": key, "user": user.pk, "idle": edit_lock_minutes()})
        return bool(cursor.rowcount)


def release(user: Any, key: str) -> None:
    """Снять свою блокировку; чужую не трогает."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM edit_locks WHERE key = %(key)s AND user_id = %(user)s",
            {"key": key, "user": user.pk},
        )


# ---------- В запросе админки ----------


def _cache(request: HttpRequest) -> dict[str, Holder | None]:
    cache: dict[str, Holder | None] = request.__dict__.setdefault(_CACHE, {})
    return cache


def blocker(request: HttpRequest, key: str) -> Holder | None:
    """Чужая блокировка ключа — для прав на запись (`has_change_permission`)."""
    cache = _cache(request)
    if key not in cache:
        cache[key] = holders(request.user, [key]).get(key)
    return cache[key]


def claim(request: HttpRequest, key: str) -> Holder | None:
    """Запись идёт: блокировка — за этим человеком. Чужая — её держатель, писать нельзя."""
    found = take(request.user, key)
    _cache(request)[key] = found
    return found


def refused(request: HttpRequest, key: str, holder: Holder, back_url: str) -> HttpResponse:
    """Ответ на запись поверх чужой блокировки: панели — JSON с текстом, странице — назад.

    Код 200, как у формы с ошибками: это не ошибка запроса, а ответ человеку, а
    ответ 4xx браузер пишет в консоль ошибкой.
    """
    text = f"{busy_text(key, holder)} — изменения не записаны."
    if is_partial(request):
        return JsonResponse({"locked": True, "message": text})
    messages.error(request, text)
    return HttpResponseRedirect(back_url)


def marker(request: HttpRequest, key: str) -> dict[str, Any]:
    """Для разметки (`admin/seo_lock.html`): ключ, чья блокировка и куда спрашивать."""
    found, mine = state(request.user, key)
    _cache(request)[key] = found
    return {
        "key": key,
        "url": reverse("admin:edit_lock"),
        "mine": mine,
        "text": f"{busy_text(key, found)} — пока только просмотр." if found else "",
    }


def split(
    request: HttpRequest, items: Iterable[Any], key_of: Callable[[Any], str]
) -> tuple[list[Any], list[tuple[Any, Holder]]]:
    """Действие по отмеченным: что можно менять, а что сейчас правят другие."""
    items = list(items)
    found = holders(request.user, (key_of(item) for item in items))
    free = [item for item in items if key_of(item) not in found]
    busy = [(item, found[key_of(item)]) for item in items if key_of(item) in found]
    return free, busy


def skipped_text(busy: list[tuple[str, Holder]]) -> str:
    """«Пропущено, сейчас правят другие: 2 — a.com (Анна), b.org (Борис).»"""
    names = ", ".join(f"{domain} ({holder.name})" for domain, holder in busy)
    return f"Пропущено, сейчас правят другие: {len(busy)} — {names}."
