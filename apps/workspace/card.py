"""Свёрнутые разделы карточки площадки (E9-13, ADR-058).

Выбор у каждого свой, в базе (`user_settings.card_closed`), и один на все
карточки: свернул «Данные из файлов» у одной площадки — они свёрнуты у всех,
в любом браузере. Не свёрнут — ключа нет в списке; по умолчанию всё развёрнуто.
"""

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.db import transaction
from django.utils import timezone

from apps.workspace.models import UserSettings

# Разделы карточки в порядке на экране; ключи — те же, что data-section в разметке.
SECTIONS = ("price", "notes", "gray", "data")


def closed_sections(user: AbstractBaseUser | AnonymousUser) -> frozenset[str]:
    if not user.is_authenticated:
        return frozenset()
    keys = (
        UserSettings.objects.filter(user_id=user.pk).values_list("card_closed", flat=True).first()
    )
    return frozenset(keys or ()) & frozenset(SECTIONS)


def set_closed(user: AbstractBaseUser, sections: tuple[str, ...], closed: bool) -> list[str]:
    """Свернуть или развернуть разделы; остальные — как были.

    Меняются только названные разделы: вторая вкладка с карточкой не затрёт
    то, что свернули в первой. Возвращает свёрнутые после записи.
    """
    with transaction.atomic():
        # select_for_update — строка заблокирована до конца транзакции: два
        # щелчка подряд из разных вкладок не перепишут список друг другу.
        row, _ = UserSettings.objects.select_for_update().get_or_create(user_id=user.pk)
        keys = set(row.card_closed)
        if closed:
            keys.update(sections)
        else:
            keys.difference_update(sections)
        row.card_closed = [key for key in SECTIONS if key in keys]
        row.updated_at = timezone.now()
        row.save(update_fields=["card_closed", "updated_at"])
    return row.card_closed
