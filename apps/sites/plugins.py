"""«Работа → Плагины» (ADR-084): расширения браузера, которые ставят с сайта.

Сейчас расширение одно — серость из Google (`browser-extension/`, ADR-054). Архив
собирается при скачивании из папки в образе, поэтому версия на сайте всегда та, что
в коде. В manifest архива дописывается адрес, с которого его скачали: переехала
система на другой адрес — достаточно скачать архив заново, а не править manifest.

Магазин Chrome не используется: Chrome и Edge ставят расширение не из магазина
только распакованной папкой в режиме разработчика. Поэтому архив — с одной папкой
внутри, и обновление — распаковать новый архив поверх неё.
"""

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

# Скрипт расширения на страницах системы: передаёт числа в карточку площадки и
# сообщает странице «Плагины» свою версию.
RELAY = "relay.js"
# Дата в архиве фиксирована: один и тот же код — один и тот же архив, байт в байт.
_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class Plugin:
    code: str
    name: str
    folder: Path
    # Имя папки в архиве: её выбирают в «Загрузить распакованное».
    archive_folder: str

    def manifest(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads((self.folder / "manifest.json").read_text("utf-8"))
        return result

    @property
    def version(self) -> str:
        return str(self.manifest()["version"])

    @property
    def filename(self) -> str:
        return f"{self.archive_folder}-{self.version}.zip"


GRAY = Plugin(
    code="gray",
    name="Серость из Google",
    folder=Path(settings.BASE_DIR) / "browser-extension",
    archive_folder="seo-gray-extension",
)
PLUGINS: dict[str, Plugin] = {plugin.code: plugin for plugin in (GRAY,)}


def site_pattern(scheme: str, host: str) -> str:
    """Шаблон адреса для manifest: `https://2.29.192.198/*`.

    Порт не пишем: шаблон без порта совпадает с любым портом, а сервер за
    прокси видит порт не всегда тот, что в адресной строке.
    """
    hostname = urlsplit(f"//{host}").hostname or host
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{scheme}://{hostname}/*"


def with_site(manifest: dict[str, Any], pattern: str) -> dict[str, Any]:
    """Manifest, в котором скрипт страниц системы работает и на этом адресе."""
    result = json.loads(json.dumps(manifest))
    for script in result.get("content_scripts", []):
        if RELAY in script.get("js", []) and pattern not in script["matches"]:
            script["matches"].append(pattern)
    return dict(result)


def archive(plugin: Plugin, pattern: str) -> bytes:
    """Zip с одной папкой расширения; manifest — с адресом системы."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(plugin.folder.rglob("*")):
            inside = path.relative_to(plugin.folder)
            if not path.is_file() or any(part.startswith(".") for part in inside.parts):
                continue
            relative = inside.as_posix()
            data = path.read_bytes()
            if relative == "manifest.json":
                manifest = with_site(plugin.manifest(), pattern)
                data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
            entry = zipfile.ZipInfo(f"{plugin.archive_folder}/{relative}", _ZIP_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            bundle.writestr(entry, data)
    return buffer.getvalue()
