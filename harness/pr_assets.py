"""Снимки и видео описаний PR: загрузка в приватный репо снимков и проверка тела PR или MR.

python -X utf8 -B ~/.harness/pr_assets.py upload <проект> <номер PR> <файл>...
python -X utf8 -B ~/.harness/pr_assets.py check <ссылка на PR или MR>
upload кладёт файлы в приватный репо снимков REPO по пути <проект>/<номер PR>/<имя файла>, повтор заменяет файл,
и печатает по строке на файл: картинка `![имя](ссылка)`, видео - ссылка (GitHub не встраивает видео из репозитория).
Страж: репо снимков больше LIMIT_KB - отказ, решает владелец. Проекты на GitLab сюда не грузят: вложение MR через glab.
check печатает нарушения тела: нет разделов, ссылка на снимок мимо репо снимков, raw.githubusercontent,
снимок не открывается; MR GitLab - снимки только вложениями /uploads/.
Коды: 0 готово или нарушений нет, 1 есть нарушения, 2 нет файла, 3 ошибка gh/glab, 4 репо снимков переполнен.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote

# Свой приватный репозиторий снимков: <владелец>/<репо> на GitHub - замени под себя.
REPO = "my-user/pr-assets"
BRANCH = "main"
ASSET_SOURCES = frozenset({(REPO, BRANCH)})
LIMIT_KB = 1_000_000
VIDEO = frozenset({".mp4", ".webm", ".mov"})
SECTIONS = ("Было", "Стало", "Почему", "Проверка", "Ревью")

_GITHUB_PR = re.compile(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/pull/(?P<n>\d+)")
_GITLAB_MR = re.compile(r"https://(?P<host>[^/\s]+)/(?P<path>[^\s]+?)/-/merge_requests/(?P<n>\d+)")
_IMAGES = re.compile(r"!\[[^\]]*\]\((?P<md>[^)\s]+)[^)]*\)|<img[^>]*\ssrc=\"(?P<html>[^\"]+)\"")
_BLOB = re.compile(r"https://github\.com/(?P<repo>[^/]+/[^/]+)/blob/(?P<ref>[^/]+)/(?P<path>[^?#]+)")

Gh = Callable[..., str]
Glab = Callable[[str, str], str]


class GhError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(str(exc)) from exc
    if done.returncode != 0:
        raise GhError((done.stderr or done.stdout).strip()[-500:])
    return done.stdout


def _gh(*args: str, body: dict | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["gh", "api", *args]
        if body is not None:
            payload = Path(tmp) / "body.json"
            payload.write_text(json.dumps(body), encoding="utf-8")
            cmd += ["--input", str(payload)]
        return _run(cmd)


def _glab(host: str, endpoint: str) -> str:
    return _run(["glab", "api", "--hostname", host, endpoint])


def _missing(exc: GhError) -> bool:
    return "404" in str(exc) or "Not Found" in str(exc)


def asset_path(project: str, pr: int, file: Path) -> str:
    return f"{project}/{pr}/{file.name}"


def markdown(path: str) -> str:
    url = f"https://github.com/{REPO}/blob/{BRANCH}/{path}?raw=true"
    return url if Path(path).suffix.lower() in VIDEO else f"![{Path(path).stem}]({url})"


def existing_sha(gh: Gh, path: str) -> str | None:
    try:
        return json.loads(gh(f"repos/{REPO}/contents/{quote(path)}?ref={BRANCH}"))["sha"]
    except GhError as exc:
        if _missing(exc):
            return None
        raise


def upload(gh: Gh, project: str, pr: int, file: Path) -> str:
    path = asset_path(project, pr, file)
    body = {
        "message": f"{project} #{pr}: {file.name}",
        "content": base64.b64encode(file.read_bytes()).decode("ascii"),
        "branch": BRANCH,
    }
    sha = existing_sha(gh, path)
    if sha:
        body["sha"] = sha
    gh("-X", "PUT", f"repos/{REPO}/contents/{quote(path)}", body=body)
    return markdown(path)


def image_urls(body: str) -> list[str]:
    return [m.group("md") or m.group("html") for m in _IMAGES.finditer(body)]


def body_violations(body: str) -> list[str]:
    return [f"нет раздела «{name}»" for name in SECTIONS if name not in body]


def github_violations(body: str, gh: Gh) -> list[str]:
    found = body_violations(body)
    for url in image_urls(body):
        if "raw.githubusercontent.com" in url:
            found.append(f"raw.githubusercontent без токена не открывается: {url}")
            continue
        if url.startswith("https://github.com/user-attachments/"):
            continue
        blob = _BLOB.match(url)
        if blob is None or (blob["repo"], blob["ref"]) not in ASSET_SOURCES:
            found.append(f"снимок мимо репо снимков, грузи через pr_assets.py upload: {url}")
            continue
        try:
            gh(f"repos/{blob['repo']}/contents/{quote(unquote(blob['path']))}?ref={blob['ref']}")
        except GhError as exc:
            if not _missing(exc):
                raise
            found.append(f"снимок не найден: {url}")
    return found


def gitlab_violations(body: str, host: str) -> list[str]:
    found = body_violations(body)
    for url in image_urls(body):
        if not (url.startswith("/uploads/") or (host in url and "/uploads/" in url)):
            found.append(f"снимок MR не вложением /uploads/, грузи через glab api .../uploads: {url}")
    return found


def check(url: str, gh: Gh, glab: Glab) -> list[str]:
    pr = _GITHUB_PR.match(url)
    if pr:
        return github_violations(json.loads(gh(f"repos/{pr['repo']}/pulls/{pr['n']}"))["body"] or "", gh)
    mr = _GITLAB_MR.match(url)
    if mr:
        data = json.loads(glab(mr["host"], f"projects/{quote(mr['path'], safe='')}/merge_requests/{mr['n']}"))
        return gitlab_violations(data["description"] or "", mr["host"])
    raise GhError(f"не ссылка на PR GitHub или MR GitLab: {url}")


def main(argv: list[str] | None = None, gh: Gh = _gh, glab: Glab = _glab) -> int:
    parser = argparse.ArgumentParser(prog="pr_assets.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload")
    up.add_argument("project")
    up.add_argument("pr", type=int)
    up.add_argument("files", nargs="+", type=Path)
    sub.add_parser("check").add_argument("url")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "check":
            found = check(args.url, gh, glab)
            print("\n".join(found) if found else f"PR без нарушений: {args.url}")
            return 1 if found else 0
        missing = [str(f) for f in args.files if not f.is_file()]
        if missing:
            print(f"нет файла: {', '.join(missing)}", file=sys.stderr)
            return 2
        size = int(gh(f"repos/{REPO}", "--jq", ".size"))
        if size > LIMIT_KB:
            print(f"репо снимков {REPO} занимает {size} КБ, предел {LIMIT_KB}: спроси владельца", file=sys.stderr)
            return 4
        for file in args.files:
            print(upload(gh, args.project, args.pr, file))
    except GhError as exc:
        print(f"gh: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
