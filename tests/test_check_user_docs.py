"""Проверщик пользовательской документации `tools/check_user_docs.py`.

Каждый тест работает на копии настоящих `user-docs/`, `mkdocs.yml` и
документов, с которыми сверяется проверщик, и портит в ней одно место.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from django.conf import settings

from tools.check_user_docs import latest_changes, main, page_url

ROOT: Path = settings.BASE_DIR


@pytest.fixture
def docs_root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "user-docs", tmp_path / "user-docs")
    shutil.copy(ROOT / "mkdocs.yml", tmp_path / "mkdocs.yml")
    (tmp_path / "docs").mkdir()
    for name in ("06-BACKLOG.md", "15-USER-DOCS.md"):
        shutil.copy(ROOT / "docs" / name, tmp_path / "docs" / name)
    return tmp_path


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(["--root", str(root)])
    return code, capsys.readouterr().out


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"в {path.name} нет «{old}» — тест устарел"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_repository_docs_pass(docs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(docs_root, capsys)
    assert code == 0, out
    assert "ошибок: 0" in out


def test_broken_link_fails(docs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _replace(docs_root / "user-docs/reference/index.md", "(statuses.md)", "(no-such-page.md)")
    code, out = _run(docs_root, capsys)
    assert code == 1
    assert "reference/index.md: ссылка `no-such-page.md` ведёт на несуществующую" in out


def test_page_missing_from_nav_fails(docs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _replace(docs_root / "mkdocs.yml", "      - reference/glossary.md\n", "")
    code, out = _run(docs_root, capsys)
    assert code == 1
    assert "страница `reference/glossary.md` не добавлена в nav" in out


def test_nav_entry_without_file_fails(docs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (docs_root / "user-docs/reference/glossary.md").unlink()
    code, out = _run(docs_root, capsys)
    assert code == 1
    assert "в nav есть `reference/glossary.md`, а файла нет" in out


def test_unknown_task_fails(docs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    page = docs_root / "user-docs/reference/index.md"
    # Список задач страницы — любой: он растёт с каждой задачей.
    text = re.sub(r"^tasks: .*$", "tasks: [E99-99]", page.read_text(encoding="utf-8"), flags=re.M)
    page.write_text(text, encoding="utf-8")
    code, out = _run(docs_root, capsys)
    assert code == 1
    assert "задачи E99-99 нет в docs/06-BACKLOG.md" in out


def test_section_must_match_folder(docs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _replace(docs_root / "user-docs/reference/index.md", "section: reference", "section: how-to")
    code, out = _run(docs_root, capsys)
    assert code == 1
    assert "section `how-to`, а файл лежит в разделе `reference`" in out


def test_missing_front_matter_fails(docs_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (docs_root / "user-docs/reference/index.md").write_text("# Справочник\n", encoding="utf-8")
    code, out = _run(docs_root, capsys)
    assert code == 1
    assert "reference/index.md: нет YAML-заголовка" in out


def test_changelog_without_unreleased_fails(
    docs_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _replace(docs_root / "user-docs/changelog.md", "## [Не выпущено]", "## [Скоро]")
    code, out = _run(docs_root, capsys)
    assert code == 1
    assert "нет раздела `## [Не выпущено]`" in out


# ---------- Карты для раздела «Документация» в интерфейсе (E9-07) ----------


def test_page_url() -> None:
    assert page_url("index.md") == "/docs/"
    assert page_url("reference/index.md") == "/docs/reference/"
    assert page_url("how-to/find-sites.md") == "/docs/how-to/find-sites/"
    # «index» в конце имени — не страница раздела.
    assert page_url("how-to/reindex.md") == "/docs/how-to/reindex/"


def test_screens_map_most_specific_first(
    docs_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "site" / "screens.json"
    assert main(["--root", str(docs_root), "--screens-out", str(out)]) == 0, capsys.readouterr()
    screens = json.loads(out.read_text(encoding="utf-8"))
    site_list = screens["site_list"]
    # Только про «Площадки» — первой, общая инструкция про панель — последней.
    assert site_list[0] == {
        "url": "/docs/how-to/find-sites/",
        "title": "Как найти площадку и отфильтровать список",
    }
    assert site_list[-1]["url"] == "/docs/how-to/edit-in-panel/"
    # Только про главную — первой; рабочий продукт — про главную и списки.
    assert screens["home"] == [
        {"url": "/docs/how-to/home-stats/", "title": "Как смотреть статистику на главной"},
        {"url": "/docs/how-to/working-product/", "title": "Как выбрать рабочий продукт"},
    ]


def test_screens_map_ties_follow_nav(
    docs_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Две страницы с одним экраном — в порядке навигации, а не по алфавиту.
    _replace(docs_root / "mkdocs.yml", "      - reference/api-spend.md\n", "")
    _replace(
        docs_root / "mkdocs.yml",
        "      - reference/index.md\n",
        "      - reference/index.md\n      - reference/api-spend.md\n",
    )
    page = docs_root / "user-docs/reference/background-tasks.md"
    _replace(page, "screens: [task_runs]", "screens: [api_usage]")
    out = tmp_path / "screens.json"
    assert main(["--root", str(docs_root), "--screens-out", str(out)]) == 0, capsys.readouterr()
    urls = [p["url"] for p in json.loads(out.read_text(encoding="utf-8"))["api_usage"]]
    assert urls == ["/docs/reference/api-spend/", "/docs/reference/background-tasks/"]


CHANGELOG = """---
title: Журнал изменений
---

# Журнал изменений

## [Не выпущено]

### Добавлено

## [2026.10.05]

### Добавлено

- Проверка индексации **по расписанию** (E2-03,
  [инструкция](how-to/check-indexation.md#schedule)).
- Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/) (E0-04).

### Исправлено

- Список не моргает (E9-08, [Статусы](reference/index.md)).

## [2026.09.30]

### Добавлено

- Старое (E1-01).
"""


def test_whatsnew_top_section_with_entries(tmp_path: Path) -> None:
    changelog = tmp_path / "changelog.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    news = latest_changes(changelog)
    # Пустой «Не выпущено» сразу после выкатки пропускается.
    assert news["version"] == "2026.10.05"
    assert news["released"] is True
    html = news["html"]
    assert "<h3>Добавлено</h3>" in html and "<h3>Исправлено</h3>" in html
    assert "<strong>по расписанию</strong>" in html
    assert '<a href="/docs/how-to/check-indexation/#schedule">инструкция</a>' in html
    assert '<a href="/docs/reference/">Статусы</a>' in html
    assert '<a href="https://keepachangelog.com/ru/1.1.0/">' in html
    assert "Старое" not in html


def test_whatsnew_unreleased_before_first_release(tmp_path: Path) -> None:
    changelog = tmp_path / "changelog.md"
    changelog.write_text(
        CHANGELOG.replace(
            "## [Не выпущено]\n\n### Добавлено\n",
            "## [Не выпущено]\n\n### Добавлено\n\n- Раздел «Документация» (E9-07).\n",
        ),
        encoding="utf-8",
    )
    news = latest_changes(changelog)
    assert news["version"] == "Не выпущено"
    assert news["released"] is False
    assert "Раздел «Документация»" in news["html"]
    assert "по расписанию" not in news["html"]


def test_whatsnew_file_written(
    docs_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "whatsnew.json"
    assert main(["--root", str(docs_root), "--whatsnew-out", str(out)]) == 0, capsys.readouterr()
    news = json.loads(out.read_text(encoding="utf-8"))
    assert news["version"] == "Не выпущено"
    assert news["html"].startswith("<h3>")
