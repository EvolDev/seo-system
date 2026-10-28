"""Роли пользователей (пользователь, 28.10.2026; ADR-078).

Роль — набор прав, который человеку выбирают одним щелчком на его карточке, вместо
двухсот отдельных галочек:
- «Администратор» — полный доступ: флажок суперпользователя, все права, и будущие тоже;
- «Линкбилдер» — вся работа, справочники и данные под ними: площадки, размещения,
  загрузки, счета, продавцы, цены, решения, анкоры, копирайтеры; общие настройки,
  запуски задач и расход API — только смотреть; пользователей не видит;
- «Только просмотр» — смотреть то же, что линкбилдер, ничего не менять.

Права ролей «Линкбилдер» и «Только просмотр» задаёт код: группы в базе с теми же
названиями сверяются с ним после каждой `migrate` (`apps.WorkspaceConfig`) и перед
тем, как роль назначить. Исключения человеку — отдельными правами на его карточке.
Своё рабочее место — наборы фильтров, рабочий продукт, шаблоны выгрузки — у всех
ролей.
"""

from typing import Any

from django.contrib.auth.models import Group, Permission
from django.db import transaction
from django.db.models import Q

ADMIN = "admin"
LINKBUILDER = "linkbuilder"
VIEWER = "viewer"
NONE = ""

# Код роли → название группы в базе (у администратора группы нет — флажок).
GROUPS = {LINKBUILDER: "Линкбилдер", VIEWER: "Только просмотр"}

CHOICES = [
    (ADMIN, "Администратор"),
    (LINKBUILDER, "Линкбилдер"),
    (VIEWER, "Только просмотр"),
    (NONE, "Без роли"),
]

HINTS = {
    ADMIN: "Полный доступ ко всему, включая пользователей и общие настройки.",
    LINKBUILDER: (
        "Работа, справочники и данные под ними — смотреть, заводить, менять, удалять. Общие"
        " настройки, запуски задач и расход API — только смотреть. Пользователей не видит."
    ),
    VIEWER: "Смотреть работу, справочники и служебное, ничего не менять.",
    NONE: "Только особые права ниже. Без них после входа экраны пусты.",
}

# Приложения линкбилдинга: работа, справочники и данные под ними.
_WORK_APPS = ("sites", "placements", "keywords")
# Своё рабочее место — у каждого, кто входит.
_OWN_APPS = ("workspace",)
# Только смотреть линкбилдеру: общие настройки, промпты, журналы и расход API.
_READ_ONLY = Q(content_type__app_label="observability") | Q(
    content_type__app_label="content",
    content_type__model__in=("domainsetting", "prompttemplate", "promptvariant"),
)


def _role_filter(role: str) -> Q:
    work = Q(content_type__app_label__in=_WORK_APPS) | Q(
        content_type__app_label="content", content_type__model="copywriter"
    )
    own = Q(content_type__app_label__in=_OWN_APPS)
    view = Q(codename__startswith="view_")
    if role == LINKBUILDER:
        return work | own | (_READ_ONLY & view)
    if role == VIEWER:
        return ((work | _READ_ONLY) & view) | own
    return Q(pk__in=[])


def permissions(role: str) -> "list[Permission]":
    """Права роли: что получает человек с этой ролью (у администратора — все)."""
    if role == ADMIN:
        return list(Permission.objects.all())
    return list(Permission.objects.filter(_role_filter(role)))


@transaction.atomic
def sync() -> None:
    """Группы ролей — ровно с теми правами, что задаёт код: заводит и сверяет."""
    for role, name in GROUPS.items():
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set(permissions(role))


def role_of(user: Any) -> str:
    """Роль человека: флажок полного доступа, иначе группа роли, иначе — без роли."""
    if user.pk is None:
        return NONE
    if user.is_superuser:
        return ADMIN
    names = set(user.groups.values_list("name", flat=True))
    for role, name in GROUPS.items():
        if name in names:
            return role
    return NONE


def set_role(user: Any, role: str) -> None:
    """Назначить роль: флажок полного доступа и одна группа роли. Пользователь записан."""
    user.is_superuser = role == ADMIN
    user.save(update_fields=["is_superuser"])
    others = Group.objects.filter(name__in=GROUPS.values())
    user.groups.remove(*others)
    if role in GROUPS:
        sync()
        user.groups.add(Group.objects.get(name=GROUPS[role]))


def label(role: str) -> str:
    return dict(CHOICES)[role]
