"""Проект собирается: настройки читаются, системные проверки Django проходят."""

from django.core.management import call_command


def test_django_system_check_passes() -> None:
    # call_command — то же, что `python manage.py check`; при ошибках бросает исключение.
    call_command("check", fail_level="WARNING")
