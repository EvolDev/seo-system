from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _sync_roles(sender: AppConfig, **kwargs: Any) -> None:
    """После `migrate` — права групп ролей сверить с кодом (apps/workspace/roles.py, ADR-078).

    Права таблиц Django заводит по приложению за раз, а роли нужны права всех
    приложений — заводим их здесь сразу все; повторно ничего не создаётся.
    """
    from django.apps import apps
    from django.contrib.auth.management import create_permissions

    from apps.workspace import roles

    for app_config in apps.get_app_configs():
        create_permissions(app_config, verbosity=0, using=kwargs.get("using", "default"))
    roles.sync()


class WorkspaceConfig(AppConfig):
    name = "apps.workspace"
    verbose_name = "Рабочее место"

    def ready(self) -> None:
        post_migrate.connect(_sync_roles, sender=self, dispatch_uid="apps.workspace.sync_roles")
