"""Hermetic tests for claude/statusline-command.sh (labels, line order, legacy fields).

Runs the shell script itself (via /bin/sh) against prepared JSON payloads and
JSONL transcripts, and asserts on its stdout/stderr/returncode. The shared
helpers live in statusline_testkit.py; the 4-line layout, width trimming and
the new input fields are covered by test_statusline_layout.py.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from statusline_testkit import (
    GREEN,
    RED,
    SCRIPT_PATH,
    SH,
    YELLOW,
    StatuslineCase,
    _sh_env,
    foreign_os_violations,
    iso_ts,
    missing,
    present,
    to_sh_path,
)


class StatuslineTestCase(StatuslineCase):
    # ---- 1. syntax ------------------------------------------------------

    def test_shell_syntax_is_valid(self):
        proc = subprocess.run([SH, "-n", SCRIPT_PATH], capture_output=True, text=True, check=False, env=_sh_env(os.environ.copy()))
        self.assertEqual(proc.returncode, 0, msg=f"sh -n failed: {proc.stderr}")

    # ---- 2. turn count ----------------------------------------------

    def test_turn_count_ignores_noise_records(self):
        now = datetime.now(timezone.utc)
        records = [
            {"type": "user", "timestamp": iso_ts(now - timedelta(seconds=10 * i)), "message": {"role": "user", "content": "hi"}}
            for i in range(7)
        ]
        # noise that must NOT be counted
        records.append({"type": "user", "timestamp": iso_ts(now), "toolUseResult": {"ok": True}})
        records.append({"type": "user", "isMeta": True, "timestamp": iso_ts(now)})
        records.append({"type": "assistant", "timestamp": iso_ts(now), "message": {"role": "assistant", "content": "hi"}})

        path = self.make_transcript(records)
        rc, out, err, clean = self.run_statusline({"transcript_path": path})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("ход 7", lines[1], msg=f"model line was: {lines[1]!r}")

    # ---- 3. turn count colour thresholds -----------------------------

    def turn_count_stdout(self, n):
        now_iso = iso_ts(datetime.now(timezone.utc))
        records = [{"type": "user", "timestamp": now_iso} for _ in range(n)]
        path = self.make_transcript(records, name=f"transcript_{n}.jsonl")
        rc, out, err, clean = self.run_statusline({"transcript_path": path})
        self.assert_clean_lines(rc, out, err, clean)
        return out

    def test_turn_count_below_60_is_green(self):
        out = self.turn_count_stdout(5)
        self.assertIn(f"{GREEN}5", out, msg=f"expected green immediately before turn count 5: {out!r}")

    def test_turn_count_60_is_yellow(self):
        out = self.turn_count_stdout(60)
        self.assertIn(f"{YELLOW}60", out, msg=f"expected yellow immediately before turn count 60: {out!r}")

    def test_turn_count_120_is_red(self):
        out = self.turn_count_stdout(120)
        self.assertIn(f"{RED}120", out, msg=f"expected red immediately before turn count 120: {out!r}")

    # ---- 4. no transcript_path key -------------------------------------

    def test_missing_transcript_path_key(self):
        rc, out, err, clean = self.run_statusline({"model": {"display_name": "Test"}})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("ход —", lines[1], msg=f"model line was: {lines[1]!r}")

    # ---- 5. transcript_path pointing at a nonexistent file --------------

    def test_transcript_path_nonexistent_file(self):
        missing_path = os.path.join(self.work_dir, "does_not_exist.jsonl")
        self.assertFalse(os.path.exists(missing_path))
        rc, out, err, clean = self.run_statusline({"transcript_path": missing_path})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("ход —", lines[1], msg=f"model line was: {lines[1]!r}")

    # ---- 6. unparsable timestamp -----------------------------------------

    def test_unparsable_timestamp_still_counts_turn(self):
        records = [{"type": "user", "timestamp": "garbage"}]
        path = self.make_transcript(records)
        rc, out, err, clean = self.run_statusline({"transcript_path": path})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("ход 1", lines[1], msg=f"model line was: {lines[1]!r}")

    # ---- 6a. tok/s of the last turn -------------------------------------------

    def test_tok_s_counts_last_turn_without_tool_time(self):
        base = datetime(2026, 10, 16, 6, 0, 0, tzinfo=timezone.utc)
        at = lambda sec: iso_ts(base + timedelta(seconds=sec))
        reply = lambda sec, mid, out: {"type": "assistant", "timestamp": at(sec),
                                       "message": {"id": mid, "usage": {"output_tokens": out}}}
        records = [
            {"type": "user", "timestamp": at(0)},
            {"type": "api-request", "timestamp": at(1), "querySource": "repl_main_thread"},
            reply(3, "a", 100), reply(5, "a", 100),              # окно 1..5, блоки одного ответа
            {"type": "user", "timestamp": at(8), "toolUseResult": {}},
            {"type": "api-request", "timestamp": at(9), "querySource": "repl_main_thread"},
            {"type": "api-request", "timestamp": at(10), "querySource": "agent:custom"},
            reply(11, "b", 60),                                   # окно 9..11, инструмент 5..8 не в счёт
            {"type": "user", "timestamp": at(20)},                # новый ход без ответа - виден прошлый
            {"type": "api-request", "timestamp": at(21), "querySource": "repl_main_thread"},
        ]
        rc, out, err, clean = self.run_statusline({"transcript_path": self.make_transcript(records)})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("27 tok/s", lines[1], msg=f"model line was: {lines[1]!r}")
        records.append(reply(23, "c", 40))
        rc, out, err, clean = self.run_statusline({"transcript_path": self.make_transcript(records, name="t2.jsonl")})
        self.assertIn("20 tok/s", self.assert_clean_lines(rc, out, err, clean)[1])

    def test_tok_s_hidden_without_transcript(self):
        rc, out, err, clean = self.run_statusline({"cost": {"total_api_duration_ms": 60000}})
        self.assertNotIn("tok/s", "\n".join(self.assert_clean_lines(rc, out, err, clean)))

    # ---- 7. cache segment -----------------------------------------------

    def test_cache_segment_typical_hit_ratio(self):
        rc, out, err, clean = self.run_statusline({"prompt_cache": {"hit_ratio": 0.8712, "misses": 4}})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("кэш 87% ✗4", lines[3], msg=f"session line was: {lines[3]!r}")

    def test_cache_segment_zero_hit_ratio_is_red(self):
        rc, out, err, clean = self.run_statusline({"prompt_cache": {"hit_ratio": 0, "misses": 9}})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("кэш 0% ✗9", lines[3], msg=f"session line was: {lines[3]!r}")
        self.assertIn(f"{RED}0%", out, msg=f"expected red colour before '0%' in raw stdout: {out!r}")

    def test_cache_segment_perfect_hit_ratio_no_miss_marker_and_green(self):
        rc, out, err, clean = self.run_statusline({"prompt_cache": {"hit_ratio": 0.995, "misses": 0}})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("кэш 100%", lines[3], msg=f"session line was: {lines[3]!r}")
        self.assertNotIn("✗", lines[3], msg=f"unexpected miss marker: {lines[3]!r}")
        self.assertIn(f"{GREEN}100%", out, msg=f"expected green colour before '100%' in raw stdout: {out!r}")

    def test_cache_segment_null_hit_ratio(self):
        rc, out, err, clean = self.run_statusline({"prompt_cache": {"hit_ratio": None}})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("кэш —", lines[3], msg=f"session line was: {lines[3]!r}")

    # ---- 8. no prompt_cache block at all ---------------------------------

    def test_cache_segment_missing_block(self):
        rc, out, err, clean = self.run_statusline({"model": {"display_name": "Test"}})
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("кэш —", lines[3], msg=f"session line was: {lines[3]!r}")

    # ---- 9. no rate_limits.seven_day ------------------------------------

    def test_missing_seven_day_rate_limit(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 42}}}
        rc, out, err, clean = self.run_statusline(payload)
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("сутки —", lines[2], msg=f"limits line was: {lines[2]!r}")

    # ---- 10. empty payload -------------------------------------------

    def test_empty_payload_all_placeholders(self):
        rc, out, err, clean = self.run_statusline({})
        self.assert_clean_lines(rc, out, err, clean)
        placeholders = ["~", "ctx —", "ход —", "работа —", "5h —", "7d —", "кэш —"]
        self.assertEqual(missing(clean, placeholders), [], msg=f"output was: {clean!r}")
        self.assertEqual(present(clean, ["$", "PR", "тёплый", "остыл"]), [], msg=f"output was: {clean!r}")

    # ---- 11. OS-specific forms must live inside their own case arm ------

    def test_foreign_os_forms_are_branch_guarded(self):
        """statusline-command.sh branches on OS_KIND via `case "$OS_KIND" in
        Darwin) ... ;; Windows) ... ;; *) ... ;; esac` (see today_midnight_epoch_and_dow
        and the PATH setup near the top). This does not forbid
        OS-specific date forms or Windows paths outright -- the script needs
        both `date -v...` (BSD/macOS) and `date -d...`/LOCALAPPDATA/WinGet
        (Linux/Windows) -- it only forbids using one OS's form outside the
        case arm that guards it, e.g. a bare `date -v` sitting outside a
        `Darwin)` arm, which would break on Linux/Windows.
        """
        violations = foreign_os_violations()
        self.assertEqual(
            violations,
            [],
            msg=f"OS-specific form used outside its guarding case arm: {violations!r}",
        )

    def test_statusline_has_no_harness_state_dependency(self):
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            self.assertNotIn("harness/runtime/state", f.read())

    # ---- 12. Darwin branch exercised via PATH shim -----------------------

    def test_darwin_branch_via_path_shim(self):
        """Force OS_KIND=Darwin with a shimmed `uname`, and shim `date` to
        answer only the exact BSD shapes the Darwin branch is expected to
        call (`-v0H -v0M -v0S +%s`, `-j -u -f '%Y-%m-%d %H:%M:%S' TS +%s`,
        bare `+%s`/`+%u`), computing the real answer via Python rather than
        the real `date` binary -- the real `date` on the machine running
        this test may itself be BSD (no `-d`) or GNU (no `-v`/`-j`), so it
        cannot be trusted to answer either shape portably. Any GNU `-d` form
        reaching the shim -- meaning the Darwin branch leaked a foreign-OS
        form -- fails loudly (nonzero exit, stderr) instead of silently
        producing a wrong or empty answer.
        """
        shim_dir = tempfile.mkdtemp(prefix="statusline_shim_darwin_")
        self.addCleanup(shutil.rmtree, shim_dir, ignore_errors=True)

        py = to_sh_path(sys.executable)
        self.write_shim(shim_dir, "uname", "#!/bin/sh\necho Darwin\n")
        self.write_shim(
            shim_dir,
            "date",
            f"""#!/bin/sh
PY='{py}'
for a in "$@"; do
  if [ "$a" = "-d" ]; then
    echo "fake-date(darwin shim): GNU -d form reached the Darwin branch: $*" >&2
    exit 1
  fi
done
case "$1" in
  -v0H)
    exec "$PY" -c "import time, datetime; print(int(time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))), datetime.date.today().isoweekday())"
    ;;
  -j)
    ts="$5"
    exec "$PY" -c "import calendar, time, sys; print(calendar.timegm(time.strptime(sys.argv[1], '%Y-%m-%d %H:%M:%S')))" "$ts"
    ;;
  +%s)
    exec "$PY" -c "import time; print(int(time.time()))"
    ;;
  +%u)
    exec "$PY" -c "import datetime; print(datetime.date.today().isoweekday())"
    ;;
  *)
    echo "fake-date(darwin shim): unexpected args: $*" >&2
    exit 1
    ;;
esac
""",
        )

        now = datetime.now(timezone.utc)
        last_ts = now - timedelta(seconds=90)
        records = [{"type": "user", "timestamp": iso_ts(last_ts)}]
        path = self.make_transcript(records)

        # No seven_day.resets_at -> forces the today_midnight_epoch() /
        # `date +%u` path (the "else" branch) rather than the wk_reset-anchored
        # one, so the Darwin `-v0H` shape actually gets exercised.
        payload = {
            "transcript_path": path,
            "rate_limits": {"seven_day": {"used_percentage": 17}},
        }
        rc, out, err, clean = self.run_statusline(payload, extra_path=shim_dir)
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("ход 1", lines[1], msg=f"model line was: {lines[1]!r}")
        # The shim answers `-v0H ... +%s %u` with "<epoch> <weekday>": the
        # allowance proves the weekday was read from that answer.
        self.assertIn(f"сутки 17/{self.weekday_allowance()}%", lines[2], msg=f"limits line was: {lines[2]!r}")

    def weekday_allowance(self):
        """Weekly allowance (percent) at the end of today: ISO weekday * 100/7."""
        return int(datetime.now().astimezone().isoweekday() * 100 / 7 + 0.5)

    # ---- 13. current (non-Darwin) branch rejects BSD date forms ----------

    def run_non_darwin(self, uname_output):
        """Symmetric to the Darwin shim above: force OS_KIND away from
        Darwin with a shimmed `uname` (tried as both a Windows-shaped and a
        Linux-shaped value, since the script's OS_KIND detection treats them
        the same past that point but real CI runs on both), and shim `date`
        to fail loudly if the script ever calls a BSD-only form (`-v*`/`-j`)
        while still answering the GNU forms it does expect -- computed via
        Python rather than the real `date` binary, since the real `date` on
        the machine running this test may itself be BSD (macOS) and unable
        to answer the GNU `-d` shape at all. Explicitly shimming `uname`
        means this exercises the actual `*)` arm regardless of which real OS
        runs the test.
        """
        py = to_sh_path(sys.executable)
        shim_dir = tempfile.mkdtemp(prefix="statusline_shim_nondarwin_")
        self.addCleanup(shutil.rmtree, shim_dir, ignore_errors=True)

        self.write_shim(shim_dir, "uname", f"#!/bin/sh\necho {uname_output}\n")
        self.write_shim(
            shim_dir,
            "date",
            f"""#!/bin/sh
PY='{py}'
for a in "$@"; do
  case "$a" in
    -v*|-j)
      echo "fake-date(non-darwin shim): BSD form reached the non-Darwin branch: $*" >&2
      exit 1
      ;;
  esac
done
case "$1" in
  -d)
    exec "$PY" -c "import time, datetime; print(int(time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))), datetime.date.today().isoweekday())"
    ;;
  -u)
    ts="$3"
    exec "$PY" -c "import calendar, time, sys; print(calendar.timegm(time.strptime(sys.argv[1], '%Y-%m-%d %H:%M:%S')))" "$ts"
    ;;
  +%s)
    exec "$PY" -c "import time; print(int(time.time()))"
    ;;
  +%u)
    exec "$PY" -c "import datetime; print(datetime.date.today().isoweekday())"
    ;;
  *)
    echo "fake-date(non-darwin shim): unexpected args: $*" >&2
    exit 1
    ;;
esac
""",
        )

        now = datetime.now(timezone.utc)
        last_ts = now - timedelta(seconds=90)
        records = [{"type": "user", "timestamp": iso_ts(last_ts)}]
        path = self.make_transcript(records)

        payload = {
            "transcript_path": path,
            "rate_limits": {"seven_day": {"used_percentage": 17}},
        }
        rc, out, err, clean = self.run_statusline(payload, extra_path=shim_dir)
        lines = self.assert_clean_lines(rc, out, err, clean)
        self.assertIn("ход 1", lines[1], msg=f"model line was: {lines[1]!r}")
        self.assertIn(f"сутки 17/{self.weekday_allowance()}%", lines[2], msg=f"limits line was: {lines[2]!r}")

    def test_non_darwin_branch_rejects_bsd_date_forms_windows_shaped_uname(self):
        self.run_non_darwin("MINGW64_NT-10.0-22631")

    def test_non_darwin_branch_rejects_bsd_date_forms_linux_uname(self):
        self.run_non_darwin("Linux")

    # ---- 14. cumulative weekly budget ("сутки" segment) -----------------

    def day_seg_color(self, out):
        """Colour escape emitted right before the 'used/allow%' value of the
        'сутки' segment in the raw (ANSI) limits line.
        """
        raw_limits = out.split("\n")[2]
        m = re.search(r"(\x1b\[[0-9;]*m)\d+/\d+%", raw_limits)
        self.assertIsNotNone(m, msg=f"'сутки' value not found in raw limits line: {raw_limits!r}")
        return m.group(1)

    def run_day_segment(self, used_pct, resets_at):
        payload = {
            "rate_limits": {"seven_day": {"used_percentage": used_pct, "resets_at": resets_at}},
        }
        rc, out, err, clean = self.run_statusline(payload)
        lines = self.assert_clean_lines(rc, out, err, clean)
        return out, lines

    def test_day_segment_cumulative_pace_yellow(self):
        """resets_at = now + 5d9h puts 'now' 16h into day index 1 of the
        cycle (days_elapsed=1): allowance = 2*100/7 = 28.57 -> '29', and
        left = 28.57 - 24 = 4.57 < base/2 -> yellow.
        """
        now = int(datetime.now(timezone.utc).timestamp())
        resets_at = now + 5 * 86400 + 9 * 3600
        out, lines = self.run_day_segment(24, resets_at)
        self.assertIn("сутки 24/29%", lines[2], msg=f"limits line was: {lines[2]!r}")
        self.assertEqual(self.day_seg_color(out), YELLOW)

    def test_day_segment_cumulative_pace_colors(self):
        """Same anchor; colour follows left = allowance - wk:
        left >= base/2 -> green, left < 0 -> red."""
        now = int(datetime.now(timezone.utc).timestamp())
        resets_at = now + 5 * 86400 + 9 * 3600
        colors = [self.day_seg_color(self.run_day_segment(wk, resets_at)[0]) for wk in (10, 35)]
        self.assertEqual(colors, [GREEN, RED])

    def test_day_segment_day_zero_allowance(self):
        """resets_at = now + 6d1h -> days_elapsed=0, so allowance = 100/7 =
        14.29 -> '14' regardless of weekly spend."""
        now = int(datetime.now(timezone.utc).timestamp())
        resets_at = now + 6 * 86400 + 3600
        _, lines = self.run_day_segment(5, resets_at)
        self.assertIn("сутки 5/14%", lines[2], msg=f"limits line was: {lines[2]!r}")

    def test_day_segment_writes_no_usage_log(self):
        """The segment no longer persists per-period marks: rendering with
        rate_limits must not create ~/.claude/usage_log.json."""
        now = int(datetime.now(timezone.utc).timestamp())
        resets_at = now + 5 * 86400 + 9 * 3600
        self.run_day_segment(24, resets_at)
        log_path = os.path.join(self.home_dir, ".claude", "usage_log.json")
        self.assertFalse(os.path.exists(log_path), msg=f"unexpected file: {log_path}")


if __name__ == "__main__":
    unittest.main()
