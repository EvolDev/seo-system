#!/usr/bin/env python3
"""Проверка пользовательской документации (docs/15-USER-DOCS.md).

Запуск из корня репозитория:
    python tools/check_user_docs.py
    python tools/check_user_docs.py --screens-out site/screens.json

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

Код выхода 1 при любой ошибке — так проверку можно ставить в pre-commit и CI.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "user-docs"
MKDOCS = ROOT / "mkdocs.yml"
BACKLOG = ROOT / "docs" / "06-BACKLOG.md"
SCREENS_DOC = ROOT / "docs" / "15-USER-DOCS.md"

REQUIRED = ("title", "description", "section", "status", "tasks", "updated")
SECTIONS = {"root", "getting-started", "how-to", "reference", "explanation"}
STATUSES = {"draft", "current", "deprecated"}
CHANGELOG_GROUPS = {"Добавлено", "Изменено", "Исправлено", "Удалено"}
TASK_RE = re.compile(r"\bE\d+-\d+[a-z]?\b")
VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
LINK_RE = re.compile(r"\]\(([^)#\s]+\.md)(#[^)]*)?\)")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)

errors: list[str] = []
warnings: list[str] = []


def err(path: Path | str, msg: str) -> None:
    errors.append(f"{rel(path)}: {msg}")


def warn(path: Path | str, msg: str) -> None:
    warnings.append(f"{rel(path)}: {msg}")


def rel(path: Path | str) -> str:
    p = Path(path)
    return str(p.relative_to(ROOT)) if p.is_absolute() else str(p)


def known_tasks() -> set[str]:
    text = BACKLOG.read_text(encoding="utf-8")
    return set(re.findall(r"^\| (E\d+-\d+[a-z]?) \|", text, re.M))


def known_screens() -> set[str]:
    text = SCREENS_DOC.read_text(encoding="utf-8")
    section = text.split("### 3.4.", 1)[-1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))


def nav_files(node) -> list[str]:
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


def check_page(path: Path, tasks_ok: set[str], screens_ok: set[str]) -> dict | None:
    text = path.read_text(encoding="utf-8")
    m = FRONT_RE.match(text)
    if not m:
        err(path, "нет YAML-заголовка между строками ---")
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        err(path, f"заголовок не разбирается: {e}")
        return None

    for key in REQUIRED:
        if key not in meta or meta[key] in (None, ""):
            if not (key == "tasks" and meta.get(key) == []):
                err(path, f"нет обязательного поля `{key}`")

    rel_dir = path.parent.relative_to(DOCS).as_posix()
    expected = "root" if rel_dir == "." else rel_dir.split("/")[0]
    section = meta.get("section")
    if section not in SECTIONS:
        err(path, f"section `{section}` — допустимо: {sorted(SECTIONS)}")
    elif section != expected:
        err(path, f"section `{section}`, а файл лежит в разделе `{expected}`")

    status = meta.get("status")
    if status not in STATUSES:
        err(path, f"status `{status}` — допустимо: {sorted(STATUSES)}")
    if status == "current":
        since = str(meta.get("since", ""))
        if not VERSION_RE.match(since):
            err(path, "у страницы со статусом current нужно поле since вида ГГГГ.ММ.ДД")

    updated = meta.get("updated")
    if not isinstance(updated, dt.date):
        err(path, "updated должен быть датой ГГГГ-ММ-ДД")
    elif updated > dt.date.today():
        err(path, f"updated {updated} — дата из будущего")

    tasks = meta.get("tasks", [])
    if not isinstance(tasks, list):
        err(path, "tasks должен быть списком, например [E2-03] или []")
    else:
        for t in tasks:
            if str(t) not in tasks_ok:
                err(path, f"задачи {t} нет в docs/06-BACKLOG.md")

    screens = meta.get("screens", [])
    if screens and not isinstance(screens, list):
        err(path, "screens должен быть списком")
    elif screens:
        for sc in screens:
            if str(sc) not in screens_ok:
                err(path, f"экрана `{sc}` нет в docs/15-USER-DOCS.md §3.4")

    for link, _anchor in LINK_RE.findall(text):
        if link.startswith(("http://", "https://")):
            continue
        target = (path.parent / link).resolve()
        if not target.exists():
            err(path, f"ссылка `{link}` ведёт на несуществующую страницу")
    return meta


def check_changelog(path: Path) -> None:
    text = FRONT_RE.sub("", path.read_text(encoding="utf-8"), count=1)
    if "## [Не выпущено]" not in text:
        err(path, "нет раздела `## [Не выпущено]`")
    for m in re.finditer(r"^## \[([^\]]+)\]", text, re.M):
        name = m.group(1)
        if name != "Не выпущено" and not VERSION_RE.match(name):
            err(path, f"версия `{name}` — нужен формат ГГГГ.ММ.ДД")
    for m in re.finditer(r"^### (.+)$", text, re.M):
        if m.group(1).strip() not in CHANGELOG_GROUPS:
            err(path, f"группа `{m.group(1)}` — допустимо: {sorted(CHANGELOG_GROUPS)}")
    # Запись может занимать несколько строк: продолжение начинается с отступа.
    entries: list[str] = []
    for line in text.splitlines():
        if line.startswith("- "):
            entries.append(line)
        elif entries and line.startswith((" ", "\t")) and line.strip():
            entries[-1] += " " + line.strip()
    for entry in entries:
        if not TASK_RE.search(entry):
            warn(path, f"запись без ID задачи: {entry[:70]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screens-out", help="записать карту экран → страница в JSON")
    args = ap.parse_args()

    tasks_ok = known_tasks()
    screens_ok = known_screens()
    pages = sorted(DOCS.rglob("*.md"))
    metas: dict[str, dict] = {}
    for p in pages:
        meta = check_page(p, tasks_ok, screens_ok)
        if meta is not None:
            metas[p.relative_to(DOCS).as_posix()] = meta

    changelog = DOCS / "changelog.md"
    if changelog.exists():
        check_changelog(changelog)
    else:
        err(DOCS, "нет changelog.md")

    config = yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))
    in_nav = nav_files(config.get("nav", []))
    on_disk = {p.relative_to(DOCS).as_posix() for p in pages}
    for f in in_nav:
        if f not in on_disk:
            err(MKDOCS, f"в nav есть `{f}`, а файла нет")
    for f in sorted(on_disk - set(in_nav)):
        err(MKDOCS, f"страница `{f}` не добавлена в nav")

    if args.screens_out:
        screens: dict[str, list[str]] = {}
        for f, meta in metas.items():
            url = "/" + f.removesuffix(".md").removesuffix("index").rstrip("/") + "/"
            for s in meta.get("screens") or []:
                screens.setdefault(str(s), []).append(url.replace("//", "/"))
        out = Path(args.screens_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(screens, ensure_ascii=False, indent=2), encoding="utf-8")

    for w in warnings:
        print(f"предупреждение  {w}")
    for e in errors:
        print(f"ошибка          {e}")
    by_status: dict[str, int] = {}
    for m in metas.values():
        by_status[m.get("status", "?")] = by_status.get(m.get("status", "?"), 0) + 1
    print(f"\nстраниц: {len(pages)} ({', '.join(f'{k} {v}' for k, v in sorted(by_status.items()))}), "
          f"ошибок: {len(errors)}, предупреждений: {len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
