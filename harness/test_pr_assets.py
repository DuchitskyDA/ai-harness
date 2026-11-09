"""pr_assets: загрузка со стражем размера и замена файла, проверка тела PR и MR. gh и glab подменены записью вызовов."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("harness_pr_assets", Path(__file__).resolve().parent / "pr_assets.py")
assert _spec is not None and _spec.loader is not None
pa = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pa
_spec.loader.exec_module(pa)

SECTIONS = "**Было:** x\n**Стало:** y\n**Почему:** z\n**Проверка:** w\n**Ревью:** v\n"
ASSET = f"https://github.com/{pa.REPO}/blob/{pa.BRANCH}"


class FakeGh:
    def __init__(self, replies: dict[str, str] | None = None, missing: set[str] | None = None, size: int = 10):
        self.replies = replies or {}
        self.missing = missing or set()
        self.size = size
        self.calls: list[tuple[tuple[str, ...], dict | None]] = []

    def __call__(self, *args: str, body: dict | None = None) -> str:
        self.calls.append((args, body))
        endpoint = next(a for a in args if a.startswith("repos/"))
        if endpoint in self.missing:
            raise pa.GhError("HTTP 404: Not Found")
        if endpoint == f"repos/{pa.REPO}":
            return str(self.size)
        return self.replies.get(endpoint, json.dumps({"sha": "s1"}))


def no_glab(host, endpoint):
    raise AssertionError("glab не нужен")


def pr_reply(body: str) -> dict[str, str]:
    return {"repos/my-user/my-app/pulls/15": json.dumps({"body": body})}


def test_upload_prints_image_and_video_and_new_file_has_no_sha(tmp_path, capsys):
    shot, clip = tmp_path / "after.png", tmp_path / "flow.mp4"
    shot.write_bytes(b"png")
    clip.write_bytes(b"mp4")
    gh = FakeGh(missing={f"repos/{pa.REPO}/contents/my-app/7/{n}?ref={pa.BRANCH}" for n in ("after.png", "flow.mp4")})

    assert pa.main(["upload", "my-app", "7", str(shot), str(clip)], gh=gh) == 0

    out = capsys.readouterr().out.splitlines()
    assert out == [f"![after]({ASSET}/my-app/7/after.png?raw=true)", f"{ASSET}/my-app/7/flow.mp4?raw=true"]
    puts = [b for a, b in gh.calls if "PUT" in a]
    assert len(puts) == 2 and all(b is not None and "sha" not in b and b["branch"] == pa.BRANCH for b in puts)


def test_reupload_passes_existing_sha(tmp_path):
    shot = tmp_path / "before.png"
    shot.write_bytes(b"png")
    gh = FakeGh()

    assert pa.main(["upload", "harness", "3", str(shot)], gh=gh) == 0

    put = next(b for a, b in gh.calls if "PUT" in a)
    assert put is not None and put["sha"] == "s1"


def test_full_assets_repo_refuses_upload(tmp_path):
    shot = tmp_path / "before.png"
    shot.write_bytes(b"png")
    gh = FakeGh(size=pa.LIMIT_KB + 1)

    assert pa.main(["upload", "harness", "3", str(shot)], gh=gh) == 4
    assert not [a for a, _ in gh.calls if "PUT" in a]


def test_missing_file_exits_2(tmp_path):
    assert pa.main(["upload", "harness", "3", str(tmp_path / "nope.png")], gh=FakeGh()) == 2


def test_check_clean_pr(capsys):
    body = SECTIONS + f"| ![до]({ASSET}/my-app/15/before.png?raw=true) |"
    gh = FakeGh(pr_reply(body))
    assert pa.main(["check", "https://github.com/my-user/my-app/pull/15"], gh=gh, glab=no_glab) == 0
    assert "без нарушений" in capsys.readouterr().out


def test_check_reports_hand_links_missing_asset_and_sections(capsys):
    lost = f"{ASSET}/my-app/15/lost.png"
    body = (
        "**Было:** x\n"
        "![a](https://raw.githubusercontent.com/my-user/my-app/feat/docs/a.png)\n"
        '<img src="https://github.com/my-user/my-app/blob/3fddded/docs/b.png?raw=true">\n'
        f"![c]({lost}?raw=true)\n"
    )
    gh = FakeGh(pr_reply(body), missing={f"repos/{pa.REPO}/contents/my-app/15/lost.png?ref={pa.BRANCH}"})

    assert pa.main(["check", "https://github.com/my-user/my-app/pull/15"], gh=gh, glab=no_glab) == 1

    out = capsys.readouterr().out
    assert "raw.githubusercontent" in out and "3fddded" in out and "lost.png" in out
    assert all(f"«{s}»" in out for s in ("Стало", "Почему", "Проверка", "Ревью")) and "«Было»" not in out


def test_check_gitlab_mr_wants_uploads(capsys):
    def glab(host, endpoint):
        assert host == "git.example.com" and endpoint == "projects/team%2Fgroup%2Fmy-app/merge_requests/9"
        return json.dumps({"description": SECTIONS + "![ok](/uploads/abc/a.png)\n![bad](https://example.com/b.png)"})

    url = "https://git.example.com/team/group/my-app/-/merge_requests/9"
    assert pa.main(["check", url], gh=FakeGh(), glab=glab) == 1
    out = capsys.readouterr().out
    assert "example.com/b.png" in out and "/uploads/abc" not in out
