"""`.env.example` совпадает с `docs/13-CONFIG.md` §1 и не содержит секретов."""

import re
from pathlib import Path

from django.conf import settings

from config.logs import SECRET_SUFFIXES

ROOT: Path = settings.BASE_DIR


def _parse(text: str) -> dict[str, str]:
    """KEY=value построчно; комментарии — целые строки и хвосты после « #»."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            result[key] = value.strip()
    return result


def _config_doc_block() -> str:
    doc = (ROOT / "docs" / "13-CONFIG.md").read_text(encoding="utf-8")
    match = re.search(r"^## 1\..*?```bash\n(.*?)```", doc, re.S | re.M)
    assert match, "в 13-CONFIG.md не найден блок §1"
    return match.group(1)


def _env_example() -> dict[str, str]:
    return _parse((ROOT / ".env.example").read_text(encoding="utf-8"))


def test_same_keys_and_defaults_as_config_doc() -> None:
    assert _env_example() == _parse(_config_doc_block())


def test_secrets_are_empty() -> None:
    filled = {
        key: value
        for key, value in _env_example().items()
        if key.endswith(SECRET_SUFFIXES) and value
    }
    assert filled == {}
