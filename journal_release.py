"""Prepare a due issue in an isolated checkout using the existing journal editor."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

from journal_schedule import reviewed_release


def run(journal_root, entries, today):
    root = Path(journal_root).resolve()
    feed = root / "journal-public" / "editorial" / "feed.json"
    published = {item["slug"] for item in json.loads(feed.read_text())["articles"]}
    due = [entry for entry in entries if entry["date"] == today.isoformat()]
    slugs = [entry["slug"] for entry in due]
    if len(slugs) != len(set(slugs)):
        raise ValueError("В очереди повторяется идентификатор выпуска.")
    for entry in due:
        slug = entry["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ValueError("Некорректный идентификатор выпуска.")
        if slug in published:
            continue
        drafts = root / "journal_editor" / "drafts"
        path = drafts / f"{slug}.json"
        if drafts.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError("Утверждённый черновик отсутствует в редакции.")
        reviewed_release(entry, path.read_bytes(), today)
        subprocess.run(
            [sys.executable, str(root / "journal_editor" / "editor.py"),
             "--publish", slug, "--review-sha256", entry["sha256"]],
            cwd=root, env={"PATH": os.environ.get("PATH", ""), "JOURNAL_AUTOPUBLISH": "1"},
            check=True, timeout=30,
        )
    return len(due)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    entries = json.loads(args.manifest.read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    try:
        print(f"Заданий на дату {today}: {run(args.journal_root, entries, today)}")
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"::error title=Редакционный выпуск остановлен::{str(error).splitlines()[0]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
