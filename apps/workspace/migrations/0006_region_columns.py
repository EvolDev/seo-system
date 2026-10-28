# Регион в выгрузке размещений (ADR-076): колонка «US Traff» сохранённых
# раскладок становится «Region Traff» — трафиком страны региона каждого
# размещения, а сразу после неё встаёт «Region» — код этой страны (пользователь,
# 26.10.2026: «добавить поле Region, а это переименовать в Region Traff — тогда
# все будущие регионы норм будут подставляться под эту таблицу»). Колонку со
# своим заголовком («Трафик США») не трогаем: так назвали именно США. «Region»
# не добавляем, если она уже есть. Ключ «region» прошлой настройки региона
# выгрузки из черновика убирается.

from typing import Any

from django.db import migrations

SCREEN = "placements.placement"
US, REGION_TRAFFIC, REGION = "us_traffic", "region_traffic", "region"
# Колонку «Region», которую добавила миграция, откат узнаёт по id.
ADDED = "region-0006"


def _forward(config: dict[str, Any]) -> bool:
    columns = config.get("columns")
    if not isinstance(columns, list):
        return False
    changed = config.pop("region", None) is not None
    result: list[Any] = []
    has_region = any(isinstance(c, dict) and c.get("source") == REGION for c in columns)
    for column in columns:
        result.append(column)
        if (
            not isinstance(column, dict)
            or column.get("source") != US
            or column.get("header") != "US Traff"
        ):
            continue
        column["source"] = REGION_TRAFFIC
        column["header"] = "Region Traff"
        changed = True
        if not has_region and config.get("mode") == "custom":
            result.append(
                {
                    "id": ADDED,
                    "index": None,
                    "header": "Region",
                    "source": REGION,
                    "enabled": bool(column.get("enabled", True)),
                    "constant": "",
                }
            )
            has_region = True
    config["columns"] = result
    return changed


def _backward(config: dict[str, Any]) -> bool:
    columns = config.get("columns")
    if not isinstance(columns, list):
        return False
    changed = False
    result: list[Any] = []
    for column in columns:
        if isinstance(column, dict) and column.get("id") == ADDED:
            changed = True
            continue
        if (
            isinstance(column, dict)
            and column.get("source") == REGION_TRAFFIC
            and column.get("header") == "Region Traff"
        ):
            column["source"] = US
            column["header"] = "US Traff"
            changed = True
        result.append(column)
    config["columns"] = result
    return changed


def rename(apps: Any, schema_editor: Any) -> None:
    ExportProfile = apps.get_model("workspace", "ExportProfile")
    for profile in ExportProfile.objects.filter(screen=SCREEN):
        if isinstance(profile.config, dict) and _forward(profile.config):
            # Открытая вкладка со старой раскладкой не перезапишет новую молча.
            profile.revision += 1
            profile.save(update_fields=["config", "revision"])


def restore(apps: Any, schema_editor: Any) -> None:
    ExportProfile = apps.get_model("workspace", "ExportProfile")
    for profile in ExportProfile.objects.filter(screen=SCREEN):
        if isinstance(profile.config, dict) and _backward(profile.config):
            profile.revision += 1
            profile.save(update_fields=["config", "revision"])


class Migration(migrations.Migration):
    dependencies = [
        ("workspace", "0005_export_profiles"),
    ]

    operations = [
        migrations.RunPython(rename, restore),
    ]
