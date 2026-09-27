"""Проверщик пользовательской документации `tools/check_user_docs.py`.

Каждый тест работает на копии настоящих `user-docs/`, `mkdocs.yml` и
документов, с которыми сверяется проверщик, и портит в ней одно место.
"""

import shutil
from pathlib import Path

import pytest
from django.conf import settings

from tools.check_user_docs import main

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
    _replace(docs_root / "user-docs/reference/index.md", "tasks: []", "tasks: [E99-99]")
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
