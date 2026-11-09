"""Shared kit for the statusline tests (test_statusline.py, test_statusline_layout.py).

Design exception for the tests that use it: they run `sh statusline-command.sh`
as a subprocess (run_statusline, shims), because the object under test is the
shell script itself. No network, no real ~/.claude data: HOME is redirected to
a fresh temp directory for every invocation.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline-command.sh")


def _find_sh():
    """Resolve `sh` once: PATH first, else sh.exe bundled with Git for Windows
    (<git>/../../usr/bin or <git>/../usr/bin relative to the git executable).
    """
    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    if git:
        gitdir = os.path.dirname(os.path.abspath(git))
        for rel in (("..", "..", "usr", "bin"), ("..", "usr", "bin")):
            cand = os.path.normpath(os.path.join(gitdir, *rel, "sh.exe"))
            if os.path.isfile(cand):
                return cand
    return None


SH = _find_sh()


def _sh_env(env):
    """Append sh's own directory to PATH so coreutils (date, awk, basename)
    resolve even when sh was found outside PATH (Git for Windows usr/bin).
    Appended, so shim directories still win the lookup.
    """
    if SH:
        env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.dirname(SH)
    return env


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

GREEN = "\x1b[0;38;5;115m"
YELLOW = "\x1b[0;38;5;222m"
RED = "\x1b[0;38;5;210m"
DIM = "\x1b[0;38;5;152m"
WHITE = "[0;38;5;255m"
CYAN = "[0;38;5;117m"
RESET = "\x1b[0m"


def iso_ts(dt):
    """Format a UTC datetime like the transcripts Claude Code writes:
    2026-09-11T00:00:01.000Z
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_sh_path(path):
    """Convert a native filesystem path to the form `sh` can exec on this host.

    No-op on POSIX hosts. On Windows the shims run under Git Bash's sh, where
    the canonical exec-able form of `C:\\foo\\bar.exe` is `/c/foo/bar.exe`;
    `cygpath -u` is preferred when it is on PATH, with a manual drive-letter
    rewrite as fallback.

    The date shims embed sys.executable through this instead of resolving
    `command -v python3` inside the shim: on Windows that lookup hits the
    Microsoft Store exec-alias stub (WindowsApps\\python3.exe) whenever
    WindowsApps sits on PATH -- and `python3.exe` exists nowhere else on a
    typical C:\\PythonXXX install. The stub prints "Python " to stderr and
    exits 49 instead of running the code, so the result depended on the
    launching shell's PATH composition. Measured 2026-09-13: `command -v
    python3` inside sh -> /c/Users/.../WindowsApps/python3, stderr "Python ",
    rc=49.
    """
    if not re.match(r"^[A-Za-z]:[\\/]", path):
        return path
    cygpath = shutil.which("cygpath")
    if cygpath:
        proc = subprocess.run(["cygpath", "-u", path], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    m = re.match(r"^([A-Za-z]):[\\/]+(.*)$", path)
    if m:
        return "/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return path.replace("\\", "/")


def full_payload(now, cwd=None):
    """Statusline input with every field the layout reads (Claude Code 2.1.296
    shape). `now` is epoch seconds; the resets and the cache expiry hang off it.
    `cwd` defaults to the process working directory; tests pass a temp dir so
    the output carries no git branch.
    """
    return {
        "workspace": {"current_dir": cwd or os.getcwd()},
        "model": {"display_name": "Opus 5.5"},
        "effort": {"level": "high"},
        "thinking": {"enabled": True},
        "context_window": {"used_percentage": 37},
        "rate_limits": {
            "five_hour": {"used_percentage": 62, "resets_at": now + 9000},
            "seven_day": {"used_percentage": 41, "resets_at": now + 300600},
        },
        "cost": {
            "total_api_duration_ms": 780000,
            "total_cost_usd": 4.12,
            "total_lines_added": 214,
            "total_lines_removed": 37,
        },
        "pr": {"number": 41, "review_state": "approved"},
        "prompt_cache": {
            "hit_ratio": 0.93,
            "misses": 2,
            "warm": True,
            "expires_at": now + 2430,
            "last_miss_cause": {"causes": ["tools_changed"]},
        },
    }


def missing(text, fragments):
    """Fragments that do NOT occur in text (empty list = all present)."""
    return [f for f in fragments if f not in text]


def present(text, fragments):
    """Fragments that DO occur in text (empty list = none present)."""
    return [f for f in fragments if f in text]


@unittest.skipIf(SH is None, "sh not found on PATH or in Git for Windows usr/bin")
class StatuslineCase(unittest.TestCase):
    """Base case: temp HOME and work dirs, script runner, shim helpers."""

    def setUp(self):
        self.home_dir = tempfile.mkdtemp(prefix="statusline_home_")
        os.makedirs(os.path.join(self.home_dir, ".claude"), exist_ok=True)
        self.addCleanup(shutil.rmtree, self.home_dir, ignore_errors=True)

        self.work_dir = tempfile.mkdtemp(prefix="statusline_work_")
        self.addCleanup(shutil.rmtree, self.work_dir, ignore_errors=True)

    def make_clone(self):
        """A fake clone: <root>/claude/statusline-command.sh (a copy of the real
        script) with <root>/harness/runtime/state next to it -- the script finds
        the harness state relative to its own path. Returns (script, state_dir).
        """
        root = tempfile.mkdtemp(prefix="statusline_clone_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "claude"))
        script = os.path.join(root, "claude", "statusline-command.sh")
        shutil.copyfile(SCRIPT_PATH, script)
        turns = "statusline-turns.jq"  # модуль раскладки (include через jq -L) лежит рядом
        shutil.copyfile(os.path.join(os.path.dirname(SCRIPT_PATH), turns), os.path.join(root, "claude", turns))
        state = os.path.join(root, "harness", "runtime", "state")
        os.makedirs(state)
        return script, state

    def write_state(self, state_dir, session, name, text):
        """Write state/<session>/<name>.json with raw `text` (may be broken JSON)."""
        folder = os.path.join(state_dir, session)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as f:
            f.write(text)

    def run_statusline(self, payload, extra_path=None, columns=None, script=None):
        """Run the script on `payload`; returns (rc, stdout, stderr, stdout
        without ANSI). `columns=None` removes COLUMNS from the environment
        (pytest may inherit one from the terminal); a value sets it as a
        string. Output is read as bytes, so a stray CR stays visible.
        """
        env = os.environ.copy()
        env["HOME"] = self.home_dir
        env.pop("COLUMNS", None)
        if columns is not None:
            env["COLUMNS"] = str(columns)
        _sh_env(env)
        if extra_path:
            # Prepended so a shim directory wins the PATH lookup over the
            # real binaries later in PATH.
            env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            [SH, script or SCRIPT_PATH],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=30,
            env=env,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        return proc.returncode, out, err, ANSI_RE.sub("", out)

    def real_bin(self, name):
        """POSIX-style path to the real `name` binary, as `sh` itself would
        resolve it -- used inside PATH-shim scripts so they can still do the
        real computation via `exec`/subshell after validating the call shape.
        """
        proc = subprocess.run(
            [SH, "-c", f"command -v {name}"], capture_output=True, text=True, timeout=30, env=_sh_env(os.environ.copy())
        )
        path = proc.stdout.strip()
        self.assertTrue(path, msg=f"could not locate real `{name}` binary via sh; stderr={proc.stderr!r}")
        return path

    def write_shim(self, dirpath, name, content):
        path = os.path.join(dirpath, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.chmod(path, 0o755)
        return path

    def make_transcript(self, records, name="transcript.jsonl"):
        path = os.path.join(self.work_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return path

    def assert_clean_lines(self, rc, stdout, stderr, clean, count=4):
        """Common contract: rc==0, empty stderr, no CR, exactly `count` lines.

        Returns the ANSI-stripped lines (what callers pattern-match on); the
        raw `stdout` with colour codes stays available to the caller for
        colour assertions.
        """
        self.assertEqual(rc, 0, msg=f"non-zero exit; stderr={stderr!r} stdout={stdout!r}")
        self.assertEqual(stderr, "", msg=f"unexpected stderr: {stderr!r}")
        self.assertNotIn("\r", stdout, msg=f"CR in output: {stdout!r}")
        lines = clean.split("\n")
        self.assertEqual(len(lines), count, msg=f"expected exactly {count} lines, got: {stdout!r}")
        return lines


def foreign_os_violations(script_path=SCRIPT_PATH):
    """OS-specific forms of the script used outside the `case "$OS_KIND"` arm
    that guards them: `date -v` (Darwin), `date -d` (Windows/*), LOCALAPPDATA
    and WinGet (Windows). Returns [(lineno, form, arm, line)].
    """
    arm_re = re.compile(r"^\s*(Darwin|Windows|\*)\)\s*(#.*)?$")
    guarded = (
        ("date -v", ("Darwin",)),
        ("date -d", ("Windows", "*")),
        ("LOCALAPPDATA", ("Windows",)),
        ("WinGet", ("Windows",)),
    )
    with open(script_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current_arm = None
    found = []
    for lineno, raw in enumerate(lines, start=1):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if re.match(r"^\s*case\b", line) or stripped in ("esac", ";;"):
            current_arm = None
            continue
        m = arm_re.match(line)
        if m:
            current_arm = m.group(1)
            continue
        found += [(lineno, form, current_arm, line) for form, arms in guarded if form in line and current_arm not in arms]
    return found
