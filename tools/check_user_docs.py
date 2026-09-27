#!/usr/bin/env python3
"""Проверка пользовательской документации (docs/15-USER-DOCS.md).

Запуск из корня репозитория (в контейнере — `make docs-check`):
    python tools/check_user_docs.py
    python tools/check_user_docs.py --screens-out site/screens.json
    python tools/check_user_docs.py --root /путь/к/копии   # для тестов

Что проверяет:
  1. У каждой страницы в user-docs/ есть YAML-заголовок с обязательными
     полями и допустимыми значениями.
  2. Раздел в заголовке совпадает с папкой.
  3. Все ID задач из поля tasks существуют в docs/06-BACKLOG.md.
  4. Каждая страница есть в nav в mkdocs.yml, и каждый пункт nav — это файл.
  5. Относительные ссылки на .md-файлы ведут на существующие страницы.
  6. В журнале изменений есть раздел «[Не выпущено]», группы названы по
     правилам, у записей есть ID задачи.
  7. Идентификаторы в поле screens есть в списке экранов
     (docs/15-USER-DOCS.md §3.4).

Якоря в ссылках не проверяет — это делает `mkdocs build --strict`.

Код выхода 1 при любой ошибке — так проверку можно ставить в pre-commit и CI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED = ("title", "description", "section", "status", "tasks", "updated")
SECTIONS = {"root", "getting-started", "how-to", "reference", "explanation"}
STATUSES = {"draft", "current", "deprecated"}
CHANGELOG_GROUPS = {"Добавлено", "Изменено", "Исправлено", "Удалено"}
TASK_RE = re.compile(r"\bE\d+-\d+[a-z]?\b")
VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
LINK_RE = re.compile(r"\]\(([^)#\s]+\.md)(#[^)]*)?\)")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


class Report:
    """Ошибки и предупреждения одного прогона; пути в них — от корня репозитория."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, path: Path, msg: str) -> None:
        self.errors.append(f"{self.rel(path)}: {msg}")

    def warn(self, path: Path, msg: str) -> None:
        self.warnings.append(f"{self.rel(path)}: {msg}")

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if path.is_absolute() else str(path)


def known_tasks(backlog: Path) -> set[str]:
    text = backlog.read_text(encoding="utf-8")
    return set(re.findall(r"^\| (E\d+-\d+[a-z]?) \|", text, re.M))


def known_screens(screens_doc: Path) -> set[str]:
    text = screens_doc.read_text(encoding="utf-8")
    section = text.split("### 3.4.", 1)[-1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))


def nav_files(node: object) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for item in node:
            out += nav_files(item)
    elif isinstance(node, dict):
        for value in node.values():
            out += nav_files(value)
    return out


def check_page(
    report: Report, docs: Path, path: Path, tasks_ok: set[str], screens_ok: set[str]
) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    m = FRONT_RE.match(text)
    if not m:
        report.err(path, "нет YAML-заголовка между строками ---")
        return None
    try:
        meta: dict[str, Any] = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        report.err(path, f"заголовок не разбирается: {e}")
        return None

    for key in REQUIRED:
        empty = key not in meta or meta[key] in (None, "")
        if empty and not (key == "tasks" and meta.get(key) == []):
            report.err(path, f"нет обязательного поля `{key}`")

    rel_dir = path.parent.relative_to(docs).as_posix()
    expected = "root" if rel_dir == "." else rel_dir.split("/")[0]
    section = meta.get("section")
    if section not in SECTIONS:
        report.err(path, f"section `{section}` — допустимо: {sorted(SECTIONS)}")
    elif section != expected:
        report.err(path, f"section `{section}`, а файл лежит в разделе `{expected}`")

    status = meta.get("status")
    if status not in STATUSES:
        report.err(path, f"status `{status}` — допустимо: {sorted(STATUSES)}")
    if status == "current":
        since = str(meta.get("since", ""))
        if not VERSION_RE.match(since):
            report.err(path, "у страницы со статусом current нужно поле since вида ГГГГ.ММ.ДД")

    updated = meta.get("updated")
    if not isinstance(updated, dt.date):
        report.err(path, "updated должен быть датой ГГГГ-ММ-ДД")
    elif updated > dt.date.today():
        report.err(path, f"updated {updated} — дата из будущего")

    tasks = meta.get("tasks", [])
    if not isinstance(tasks, list):
        report.err(path, "tasks должен быть списком, например [E2-03] или []")
    else:
        for t in tasks:
            if str(t) not in tasks_ok:
                report.err(path, f"задачи {t} нет в docs/06-BACKLOG.md")

    screens = meta.get("screens", [])
    if screens and not isinstance(screens, list):
        report.err(path, "screens должен быть списком")
    elif screens:
        for sc in screens:
            if str(sc) not in screens_ok:
                report.err(path, f"экрана `{sc}` нет в docs/15-USER-DOCS.md §3.4")

    for link, _anchor in LINK_RE.findall(text):
        if link.startswith(("http://", "https://")):
            continue
        target = (path.parent / link).resolve()
        if not target.exists():
            report.err(path, f"ссылка `{link}` ведёт на несуществующую страницу")
    return meta


def check_changelog(report: Report, path: Path) -> None:
    text = FRONT_RE.sub("", path.read_text(encoding="utf-8"), count=1)
    if "## [Не выпущено]" not in text:
        report.err(path, "нет раздела `## [Не выпущено]`")
    for m in re.finditer(r"^## \[([^\]]+)\]", text, re.M):
        name = m.group(1)
        if name != "Не выпущено" and not VERSION_RE.match(name):
            report.err(path, f"версия `{name}` — нужен формат ГГГГ.ММ.ДД")
    for m in re.finditer(r"^### (.+)$", text, re.M):
        if m.group(1).strip() not in CHANGELOG_GROUPS:
            report.err(path, f"группа `{m.group(1)}` — допустимо: {sorted(CHANGELOG_GROUPS)}")
    # Запись может занимать несколько строк: продолжение начинается с отступа.
    entries: list[str] = []
    for line in text.splitlines():
        if line.startswith("- "):
            entries.append(line)
        elif entries and line.startswith((" ", "\t")) and line.strip():
            entries[-1] += " " + line.strip()
    for entry in entries:
        if not TASK_RE.search(entry):
            report.warn(path, f"запись без ID задачи: {entry[:70]}")


def write_screens(metas: dict[str, dict[str, Any]], out: Path) -> None:
    screens: dict[str, list[str]] = {}
    for f, meta in metas.items():
        url = "/" + f.removesuffix(".md").removesuffix("index").rstrip("/") + "/"
        for s in meta.get("screens") or []:
            screens.setdefault(str(s), []).append(url.replace("//", "/"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(screens, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screens-out", help="записать карту экран → страница в JSON")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="корень репозитория")
    args = ap.parse_args(argv)

    root: Path = args.root.resolve()
    docs = root / "user-docs"
    mkdocs = root / "mkdocs.yml"
    report = Report(root)

    tasks_ok = known_tasks(root / "docs" / "06-BACKLOG.md")
    screens_ok = known_screens(root / "docs" / "15-USER-DOCS.md")
    pages = sorted(docs.rglob("*.md"))
    metas: dict[str, dict[str, Any]] = {}
    for p in pages:
        meta = check_page(report, docs, p, tasks_ok, screens_ok)
        if meta is not None:
            metas[p.relative_to(docs).as_posix()] = meta

    changelog = docs / "changelog.md"
    if changelog.exists():
        check_changelog(report, changelog)
    else:
        report.err(docs, "нет changelog.md")

    config = yaml.safe_load(mkdocs.read_text(encoding="utf-8"))
    in_nav = nav_files(config.get("nav", []))
    on_disk = {p.relative_to(docs).as_posix() for p in pages}
    for f in in_nav:
        if f not in on_disk:
            report.err(mkdocs, f"в nav есть `{f}`, а файла нет")
    for f in sorted(on_disk - set(in_nav)):
        report.err(mkdocs, f"страница `{f}` не добавлена в nav")

    if args.screens_out:
        write_screens(metas, Path(args.screens_out))

    for w in report.warnings:
        print(f"предупреждение  {w}")
    for e in report.errors:
        print(f"ошибка          {e}")
    by_status: dict[str, int] = {}
    for m in metas.values():
        status = str(m.get("status", "?"))
        by_status[status] = by_status.get(status, 0) + 1
    statuses = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
    print(
        f"\nстраниц: {len(pages)} ({statuses}), "
        f"ошибок: {len(report.errors)}, предупреждений: {len(report.warnings)}"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
