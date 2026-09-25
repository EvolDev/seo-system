"""Доменные приложения подключены и не конфликтуют метками (ADR-024)."""

import pytest
from django.apps import apps
from django.core.management import call_command

DOMAIN_APPS = ["sites", "placements", "keywords", "content", "observability", "integrations"]


@pytest.mark.parametrize("label", DOMAIN_APPS)
def test_domain_app_installed(label: str) -> None:
    # Метка приложения (label) — последняя часть пути: apps.sites → sites.
    assert apps.get_app_config(label).name == f"apps.{label}"


@pytest.mark.django_db
def test_no_pending_migrations() -> None:
    # Модели и миграции совпадают: makemigrations --check падает, если нет.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
