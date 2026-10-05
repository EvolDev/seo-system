"""«Мои фильтры»: сохранить, удалить и вернуть набор (E9-10, ADR-050);
рабочий продукт из шапки (E9-12, ADR-057); свёрнутые разделы карточки
площадки (E9-13, ADR-058).

Блок над колонкой фильтров (`seo/saved-filters.js`) шлёт POST и получает JSON:
подпись для сообщения и наборы списка заново — селектор перерисовывается без
перехода. Чужой набор не виден и не удаляется: ищем только среди своих.
Ответ на отказ (название занято) — тоже 200: это не ошибка запроса, а вопрос
человеку, а ответ 4xx браузер пишет в консоль ошибкой.
"""

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.exceptions import NotRegistered
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.http import (
    Http404,
    HttpRequest,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
    QueryDict,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from apps.sites.models import Product
from apps.workspace import card
from apps.workspace.models import SavedFilter
from apps.workspace.products import choose_product, without_product
from apps.workspace.saved_filters import NAME_MAX, as_json, clean_query, sets_of


def _user_id(request: HttpRequest) -> int:
    # Сюда пускает только admin_view — пользователь вошёл, номер у него есть.
    user_id = request.user.pk
    if user_id is None:
        raise PermissionDenied
    return user_id


def _check_screen(request: HttpRequest, screen: str) -> None:
    """Список есть в админке, и пользователю можно его смотреть."""
    app_label, _, model_name = screen.partition(".")
    try:
        model = apps.get_model(app_label, model_name)
        model_admin = admin.site.get_model_admin(model)
    except (LookupError, ValueError, NotRegistered) as error:
        raise Http404("Нет такого списка.") from error
    if not model_admin.has_view_permission(request):
        raise PermissionDenied


def _answer(request: HttpRequest, screen: str, **data: object) -> JsonResponse:
    # Список — чтобы «Отменить», нажатое уже на другом экране, не перерисовало его селектор.
    sets = as_json(sets_of(_user_id(request), screen))
    return JsonResponse({**data, "screen": screen, "sets": sets})


def save_view(request: HttpRequest) -> JsonResponse:
    """Сохранить текущие фильтры списка под названием.

    Название занято — сначала вопрос (`exists`), с `replace=1` — фильтры
    набора заменяются текущими.
    """
    screen = request.POST.get("screen", "")
    _check_screen(request, screen)
    name = " ".join(request.POST.get("name", "").split())
    if not name:
        return JsonResponse({"saved": False, "message": "Назовите набор."})
    if len(name) > NAME_MAX:
        return JsonResponse(
            {"saved": False, "message": f"Название длиннее {NAME_MAX} знаков — сократите."}
        )
    query = clean_query(request.POST.get("query", ""))
    existing = (
        sets_of(_user_id(request), screen)
        .annotate(lower_name=Lower("name"))
        .filter(lower_name=name.lower())
        .first()
    )
    if existing is not None:
        if request.POST.get("replace") != "1":
            return JsonResponse(
                {
                    "saved": False,
                    "exists": True,
                    "message": f"Набор «{existing.name}» уже есть. Заменить его фильтры текущими?",
                }
            )
        existing.name = name
        existing.query = query
        existing.save(update_fields=["name", "query"])
        return _answer(
            request,
            screen,
            saved=True,
            id=existing.pk,
            message=f"Набор «{name}» — фильтры заменены.",
        )
    try:
        # Свой атомарный блок: при гонке двух вкладок упадёт только он.
        with transaction.atomic():
            item = SavedFilter.objects.create(
                user_id=_user_id(request), screen=screen, name=name, query=query
            )
    except IntegrityError:
        return JsonResponse(
            {
                "saved": False,
                "exists": True,
                "message": f"Набор «{name}» уже есть. Заменить его фильтры текущими?",
            }
        )
    return _answer(request, screen, saved=True, id=item.pk, message=f"Набор «{name}» сохранён.")


def delete_view(request: HttpRequest, pk: int) -> JsonResponse:
    """Удалить набор — пометкой: «Отменить» в сообщении его вернёт."""
    item = get_object_or_404(SavedFilter, pk=pk, user_id=_user_id(request), deleted_at__isnull=True)
    item.deleted_at = timezone.now()
    item.save(update_fields=["deleted_at"])
    return _answer(
        request, item.screen, deleted=True, id=item.pk, message=f"Набор «{item.name}» удалён."
    )


def restore_view(request: HttpRequest, pk: int) -> JsonResponse:
    """«Отменить» удаление. Название за это время заняли — вернуть нельзя."""
    item = get_object_or_404(
        SavedFilter, pk=pk, user_id=_user_id(request), deleted_at__isnull=False
    )
    item.deleted_at = None
    try:
        with transaction.atomic():
            item.save(update_fields=["deleted_at"])
    except IntegrityError:
        return _answer(
            request,
            item.screen,
            restored=False,
            message=f"Вернуть нельзя: набор «{item.name}» уже сохранён заново.",
        )
    return _answer(
        request, item.screen, restored=True, id=item.pk, message=f"Набор «{item.name}» возвращён."
    )


def working_product_view(request: HttpRequest) -> HttpResponseRedirect:
    """Выбор рабочего продукта в шапке: запомнить и вернуть на тот же экран.

    Экран — без выбора продукта и номера страницы в адресе: список сразу
    показывает новый рабочий продукт (seo/soft-nav.js — на месте, без
    перезагрузки), а не продукт, выбранный в колонке раньше.
    """
    product = Product.objects.filter(pk=_number(request.POST.get("working_product"))).first()
    if product is None:
        raise Http404("Нет такого продукта.")
    choose_product(request.user, product)  # type: ignore[arg-type]
    target = request.POST.get("next", "")
    if not target.startswith("/") or not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return HttpResponseRedirect(reverse("admin:index"))
    path, _, query = target.partition("?")
    rest = without_product(QueryDict(query))
    return HttpResponseRedirect(f"{path}?{rest}" if rest else path)


def card_sections_view(request: HttpRequest) -> JsonResponse | HttpResponseBadRequest:
    """Свернуть или развернуть раздел карточки площадки — на все карточки.

    `section` — ключ раздела или `all` («Свернуть все»), `closed` — 1 или 0.
    Неизвестный раздел — ошибка разметки, а не вопрос человеку: 400.
    """
    section = request.POST.get("section", "")
    sections = card.SECTIONS if section == "all" else (section,)
    if not set(sections) <= set(card.SECTIONS):
        return HttpResponseBadRequest("Нет такого раздела карточки.")
    closed = request.POST.get("closed") == "1"
    keys = card.set_closed(request.user, sections, closed)  # type: ignore[arg-type]
    return JsonResponse({"closed": keys})


def _number(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0
