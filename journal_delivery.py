"""Confirm the exact released articles after the public site deploys."""
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time

import requests

from marketing_journal import normalized_title, verify_publication


@dataclass(frozen=True)
class ExpectedArticle:
    slug: str
    title: str


def confirm_release(expected, verify=verify_publication, wait=time.sleep, *, attempts=10):
    publications = {}
    for attempt in range(attempts):
        for article in expected:
            if article.slug in publications:
                continue
            try:
                result = verify(article.slug)
            except (requests.RequestException, ValueError):
                continue
            if normalized_title(result["title"]) == normalized_title(article.title):
                publications[article.slug] = result
        if len(publications) == len(expected):
            return list(publications.values()), True
        if attempt + 1 < attempts:
            wait(15)
    return list(publications.values()), False


def main():
    before, after, output = (Path(arg) for arg in sys.argv[1:])
    known = {row["slug"] for row in json.loads(before.read_text(encoding="utf-8"))["articles"]}
    expected = [ExpectedArticle(row["slug"], row["title"])
                for row in json.loads(after.read_text(encoding="utf-8"))["articles"]
                if row["slug"] not in known]
    receipts, confirmed = confirm_release(expected)
    output.write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
    if not confirmed:
        print("::error title=Проверка выпуска::Не все ожидаемые страницы подтверждены на публичном сайте.")
    return 0 if confirmed else 1


if __name__ == "__main__":
    raise SystemExit(main())
