"""Layout tests for claude/statusline-command.sh: the four dashboard lines,
width trimming by COLUMNS and the input fields added in Claude Code 2.1.296
(cost, lines changed, pr.*, prompt_cache warmth and miss cause).

Shared helpers live in statusline_testkit.py. The script runs as a subprocess
(design exception of the bead): the object under test is the shell script.
"""

import json
import os
import re
import time

from statusline_testkit import (
    DIM,
    GREEN,
    RED,
    RESET,
    WHITE,
    YELLOW,
    StatuslineCase,
    full_payload,
    missing,
    present,
)

LABELS = ("место", "модель", "claude", "сессия")
WIDTHS = (160, 120, 100, 90, 80, 70)
# Drop chains per line: the marker that leaves first (lowest step) comes first.
# Steps: cause 1, day budget 2, cost 3, lines +/- 4, resets 5, row labels 6.
CHAINS = (
    (2, ("сутки", "сброс", "claude")),
    (3, ("(tools_changed)", "$", "+214", "сессия")),
)


def order_violations(width, lines):
    """Markers that survive while a marker of a later drop step is gone, plus
    row labels shown on some lines but not on all of them."""
    found = []
    for idx, chain in CHAINS:
        for i, marker in enumerate(chain):
            if marker in lines[idx] and any(later not in lines[idx] for later in chain[i + 1 :]):
                found.append((width, idx, marker))
    shown = {name in line for name, line in zip(LABELS, lines)}
    if len(shown) > 1:
        found.append((width, "labels", sorted(shown)))
    return found


class StatuslineLayoutTest(StatuslineCase):
    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        self.payload = full_payload(self.now, cwd=self.work_dir)

    def render(self, columns=None):
        """Run the script on self.payload, check the output contract, return
        (raw stdout, ANSI-stripped lines)."""
        rc, out, err, clean = self.run_statusline(self.payload, columns=columns)
        return out, self.assert_clean_lines(rc, out, err, clean)

    # ---- S1: everything fits ------------------------------------------

    def test_full_payload_all_details(self):
        _, lines = self.render(columns=160)
        rows = [
            ("место  ", [os.path.basename(self.work_dir), "", "PR #41 ✓ одобрен"]),
            ("модель ", ["Opus 5.5 · high · think ●", "ctx ▰▰▰▰▱▱▱▱▱▱ 37%", "ход —", "работа 13м"]),
            ("claude ", ["5h ▰▰▰▰▰▰▱▱▱▱ 62% ▲темп сброс 2ч30м", "7d ▰▰▰▰▱▱▱▱▱▱ 41% сброс 3д11ч", "сутки 41/57%"]),
            ("сессия ", ["кэш 93% ✗2 · тёплый 41м (tools_changed)", "$4.12", "+214 −37"]),
        ]
        # Строка «место» не выравнивается: группы через три пробела; сетка - по строкам 2-4.
        widths = [max(len(groups[i]) for _, groups in rows[1:] if i < len(groups)) for i in range(3)]
        place = rows[0][0] + "   ".join(g for g in rows[0][1] if g)
        grid = [label + "   ".join(g.ljust(widths[i]) for i, g in enumerate(groups[:3])) + "   ".join([""] + groups[3:]) for label, groups in rows[1:]]
        self.assertEqual(lines, [place] + [line.rstrip() for line in grid])

    # ---- S2: 100 columns -----------------------------------------------

    def test_width_100_fits(self):
        _, lines = self.render(columns=100)
        self.assertLessEqual(max(len(line) for line in lines), 99)
        self.assertEqual(missing("\n".join(lines), ["(tools_changed)", "сутки 41/57%", "$4.12"]), [])

    # ---- S3: 70 columns keep the pace marker and the red ----------------

    def test_width_70_keeps_pace_and_red(self):
        self.payload["context_window"]["used_percentage"] = 95
        out, lines = self.render(columns=70)
        self.assertLessEqual(max(len(line) for line in lines), 69)
        self.assertIn("▲темп", "\n".join(lines))
        self.assertIn(f"{RED}95%", out)
        self.assertNotIn("сутки", "\n".join(lines))

    # ---- S4: drop order -------------------------------------------------

    def test_details_drop_in_priority_order(self):
        runs = [self.render(columns=width)[1] for width in WIDTHS]
        broken = [v for width, lines in zip(WIDTHS, runs) for v in order_violations(width, lines)]
        self.assertEqual(broken, [])
        self.assertEqual([lines[2].startswith("claude") for lines in runs], [True] * 5 + [False])

    def test_session_line_sheds_cause_then_cost_then_lines(self):
        self.payload = {k: self.payload[k] for k in ("cost", "prompt_cache")}
        session = [self.render(columns=width)[1][3] for width in (61, 46, 36)]
        self.assertEqual(session[0], "сессия кэш 93% ✗2 · тёплый 41м   $4.12   +214 −37")
        self.assertEqual(session[1], "сессия кэш 93% ✗2 · тёплый 41м   +214 −37")
        self.assertEqual(session[2], "сессия кэш 93% ✗2 · тёплый 41м")

    def test_limits_line_sheds_day_then_resets_then_labels(self):
        self.payload = {"rate_limits": self.payload["rate_limits"]}
        limits = [self.render(columns=width)[1][2] for width in (80, 56, 49)]
        self.assertEqual(limits[0], "claude 5h ▰▰▰▰▰▰▱▱▱▱ 62% ▲темп сброс 2ч30м   7d ▰▰▰▰▱▱▱▱▱▱ 41% сброс 3д11ч")
        self.assertEqual(limits[1], "claude 5h ▰▰▰▰▰▰▱▱▱▱ 62% ▲темп   7d ▰▰▰▰▱▱▱▱▱▱ 41%")
        self.assertEqual(limits[2], "5h ▰▰▰▰▰▰▱▱▱▱ 62% ▲темп   7d ▰▰▰▰▱▱▱▱▱▱ 41%")

    def test_red_day_budget_survives_the_cut(self):
        self.payload = {"rate_limits": self.payload["rate_limits"]}
        self.payload["rate_limits"]["seven_day"] = {"used_percentage": 90, "resets_at": self.now + 5 * 86400 + 9 * 3600}
        out, lines = self.render(columns=71)
        self.assertEqual(lines[2], "claude 5h ▰▰▰▰▰▰▱▱▱▱ 62% ▲темп   7d ▰▰▰▰▰▰▰▰▰▱ 90%   сутки 90/29%")
        self.assertIn(f"{RED}90/29%", out)

    def test_tail_is_cut_with_ellipsis_on_tiny_width(self):
        out, lines = self.render(columns=30)
        self.assertEqual(lines[1], "Opus 5.5 · high · think ●   …")
        self.assertLessEqual(max(len(line) for line in lines), 29)
        self.assertTrue(out.split("\n")[1].endswith(RESET + "…"))

    # ---- S6: new fields absent ------------------------------------------

    def test_missing_new_fields_keep_output(self):
        del self.payload["cost"], self.payload["pr"]
        self.payload["prompt_cache"] = {"hit_ratio": 0.93, "misses": 2}
        _, lines = self.render(columns=160)
        text = "\n".join(lines)
        self.assertEqual(present(text, ["$", "PR", "тёплый", "остыл", "("]), [])
        self.assertEqual(missing(text, ["кэш 93% ✗2", "работа —", "ctx ▰▰▰▰▱▱▱▱▱▱ 37%"]), [])

    # ---- fields of an unexpected type --------------------------------------

    def shape(self, patch):
        """Output text after `patch` replaces top-level fields of a fresh FULL payload."""
        self.payload = {**full_payload(self.now, cwd=self.work_dir), **patch}
        text = "\n".join(self.render(columns=160)[1])
        return re.sub(" {2,}", " ", text)  # padding between columns is not under test

    def test_fields_of_unexpected_type_only_lose_their_element(self):
        cache = self.payload["prompt_cache"]
        as_text = self.shape({"prompt_cache": {**cache, "last_miss_cause": "tools_changed"}})
        as_object = self.shape({"prompt_cache": {**cache, "last_miss_cause": {"reason": "tools_changed"}}})
        pr_number = self.shape({"pr": 41})
        mixed = self.shape({"rate_limits": 5, "model": "opus", "cost": "free"})
        kept = ["кэш 93% ✗2 · тёплый 41м $4.12", "сутки 41/57%"]
        self.assertEqual(present(as_text + as_object, ["(", "tools_changed"]), [])
        self.assertEqual(missing(as_text, kept) + missing(as_object, kept), [])
        whole = ["кэш 93% ✗2 · тёплый 41м (tools_changed) $4.12","сутки 41/57%"]
        self.assertEqual(present(pr_number, ["PR", "MR"]) + missing(pr_number, whole), [])
        self.assertEqual(missing(mixed, [os.path.basename(self.work_dir), "5h —", "кэш 93%"]), [])

    # ---- S7: pace marker --------------------------------------------------

    def test_five_hour_pace_marker(self):
        ahead = "\n".join(self.render(columns=160)[1])  # 62% used, 50% of the window gone
        self.payload["rate_limits"]["five_hour"]["resets_at"] = self.now + 3600  # 80% gone
        behind = "\n".join(self.render(columns=160)[1])
        self.assertIn("62% ▲темп", ahead)
        self.assertNotIn("▲", behind)

    # ---- S8: PR states -----------------------------------------------------

    def pr_tail(self, pr):
        """Raw text of the last (PR) group on the place line."""
        self.payload["pr"] = pr
        out, _ = self.render(columns=160)
        return out.split("\n")[0].split("   ")[-1]

    def test_pr_states(self):
        cases = [
            {"number": 41, "review_state": "approved"},
            {"number": 41, "review_state": "pending"},
            {"number": 41, "review_state": "changes_requested"},
            {"number": 41, "review_state": "draft"},
            {"number": 41},
            {"number": 41, "kind": "mr"},
        ]
        tails = [self.pr_tail(pr) for pr in cases]
        self.assertEqual(
            tails,
            [
                f"{GREEN}PR #41 ✓ одобрен{RESET}",
                f"{YELLOW}PR #41 ждёт ревью{RESET}",
                f"{RED}PR #41 ✗ правки{RESET}",
                f"{DIM}PR #41 черновик{RESET}",
                f"{WHITE}PR #41{RESET}",
                f"{WHITE}MR !41{RESET}",
            ],
        )

    # ---- S9: cache warmth ----------------------------------------------------

    def warmth(self, cache):
        """Raw session line after the cache patch is applied."""
        self.payload["prompt_cache"].update(cache)
        out, _ = self.render(columns=160)
        return out.split("\n")[3]

    def test_cache_warmth(self):
        cold = self.warmth({"warm": False})
        two = self.warmth({"warm": True, "expires_at": self.now + 120})
        five = self.warmth({"expires_at": self.now + 300})
        six = self.warmth({"expires_at": self.now + 360})
        self.assertIn(f"{YELLOW}остыл", cold)
        self.assertIn(f"{YELLOW}тёплый 2м", two)
        self.assertIn(f"{YELLOW}тёплый 5м", five)
        self.assertIn(f"{GREEN}тёплый 6м", six)
        self.assertNotIn("тёплый", cold)

    # ---- S10: COLUMNS that is not a positive integer ----------------------------

    def test_bad_columns_means_no_trim(self):
        sessions = [self.render(columns=value)[1][3] for value in ("abc", "0")]
        self.assertEqual(sessions, ["сессия кэш 93% ✗2 · тёплый 41м (tools_changed)   $4.12   +214 −37"] * 2)

    # ---- old bead binding files are ignored ------------------------------------

    def binding(self, bead_id="app-tqpi", cwd=None, **extra):
        """JSON text of a harness binding file whose cwd covers the session."""
        return json.dumps({"bead_id": bead_id, "stage": "agreed-design", "cwd": cwd or os.path.dirname(self.work_dir), **extra})

    def render_with_state(self, files, columns=160):
        """Run a copy of the script inside a fake clone whose state/sess-1 holds
        `files` ({name: json text}); returns (raw stdout, ANSI-stripped lines)."""
        script, state = self.make_clone()
        for name, text in files.items():
            self.write_state(state, "sess-1", name, text)
        self.payload["session_id"] = "sess-1"
        rc, out, err, clean = self.run_statusline(self.payload, columns=columns, script=script)
        return out, self.assert_clean_lines(rc, out, err, clean)

    def test_bead_and_stage_are_not_shown(self):
        out, lines = self.render_with_state({"a": self.binding()})
        self.assertEqual(present(out, ["бид", "app-tqpi", "agreed-design"]), [])
        self.assertIn("PR #41", lines[0])

    def test_closed_binding_is_not_shown(self):
        files = {"a": self.binding("old-1", bead_closed=True, cwd=self.work_dir), "b": self.binding()}
        _, lines = self.render_with_state(files)
        self.assertEqual(present(lines[0], ["old-1", "app-tqpi", "бид"]), [])

    def test_broken_state_file_keeps_the_rest(self):
        _, lines = self.render_with_state({"a": "{not json"})
        self.assertNotIn("бид", lines[0])
        self.assertIn("модель Opus 5.5", lines[1])

    def test_no_state_dir_gives_no_bead(self):
        self.payload["session_id"] = "sess-1"
        _, lines = self.render(columns=160)
        self.assertNotIn("бид", lines[0])

    def test_large_binding_file_still_renders(self):
        # Регрессия 11.10: текст файла привязки уходил в jq аргументом, файл на 24 КБ
        # вместе с программой раскладки упирался в лимит командной строки Windows
        # («Argument list too long») - статуслайн пропадал целиком.
        big = self.binding(bead={"notes": "x" * 40000})
        _, lines = self.render_with_state({"a": big})
        self.assertNotIn("бид", lines[0])

    def test_broken_layout_shows_reason_not_nothing(self):
        # Пустой вывод CLI молча прячет статуслайн целиком; поломка должна быть видна.
        shims = os.path.join(self.home_dir, "shims")
        os.makedirs(shims, exist_ok=True)
        with open(os.path.join(shims, "jq"), "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\necho 'boom: jq broke' >&2\nexit 3\n")
        os.chmod(os.path.join(shims, "jq"), 0o755)
        rc, _, _, clean = self.run_statusline(self.payload, extra_path=shims, columns=160)
        self.assertEqual(0, rc)
        self.assertIn("статуслайн не собрался", clean)
        self.assertIn("boom: jq broke", clean)

    def test_bead_stays_absent_at_any_width(self):
        shown = [("бид" in self.render_with_state({"a": self.binding()}, columns=w)[1][0]) for w in (160, 100, 70)]
        self.assertEqual(shown, [False, False, False])

    # ---- aligned grid ----------------------------------------------------------

    def test_groups_start_in_the_same_columns_on_every_row(self):
        # Сетка - строки «модель», «claude», «сессия»; длинный бид в «месте» её не раздвигает.
        _, lines = self.render_with_state({"a": self.binding()})
        starts = [[m.start() for m in re.finditer(r"(?<= {3})\S", line)] for line in lines[1:]]
        self.assertEqual([row[0] for row in starts], [starts[0][0]] * 3)
        self.assertEqual([row[1] for row in starts], [starts[0][1]] * 3)
        self.assertIn("ход", lines[1][: starts[0][1] + 3])

    def test_empty_group_keeps_its_column(self):
        self.payload["cost"].pop("total_cost_usd", None)
        _, lines = self.render_with_state({})
        self.assertEqual(lines[3].index("+214"), lines[2].index("сутки"))

    # ---- git facts are cached per cwd -------------------------------------------

    def test_git_status_is_cached_between_redraws(self):
        shims = os.path.join(self.work_dir, "shims")
        os.makedirs(shims)
        calls = os.path.join(self.work_dir, "git-calls")
        self.write_shim(
            shims, "git", f"#!/bin/sh\necho x >> '{calls.replace(os.sep, '/')}'\nprintf '# branch.head cached-br\n1 .M N... a b\n'\n"
        )
        first = self.run_statusline(self.payload, extra_path=shims, columns=160)
        second = self.run_statusline(self.payload, extra_path=shims, columns=160)
        self.assertIn("cached-br", first[3].split("\n")[0])
        self.assertEqual(first[3], second[3])
        with open(calls, encoding="utf-8") as f:
            self.assertEqual(len(f.read().split()), 1)

    # ---- Codex limits row from the newest rollout --------------------------------

    def write_rollout(self, limits, day_offset=0, name="rollout-a.jsonl"):
        """~/.codex/sessions/<local day>/<name> with one token_count event."""
        day = time.strftime("%Y/%m/%d", time.localtime(self.now - day_offset * 86400))
        folder = os.path.join(self.home_dir, ".codex", "sessions", *day.split("/"))
        os.makedirs(folder, exist_ok=True)
        event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta"}) + "\n" + json.dumps(event) + "\n")

    def codex_limits(self, primary_used=52.0, primary_reset=9000):
        return {
            "limit_id": "codex",
            "primary": {"used_percent": primary_used, "window_minutes": 300, "resets_at": self.now + primary_reset},
            "secondary": {"used_percent": 8.0, "window_minutes": 10080, "resets_at": self.now + 300600},
        }

    def test_codex_row_shows_both_windows(self):
        self.write_rollout(self.codex_limits())
        rc, out, err, clean = self.run_statusline(self.payload, columns=160)
        lines = self.assert_clean_lines(rc, out, err, clean, count=5)
        self.assertTrue(lines[3].startswith("codex  "), lines)
        self.assertEqual(missing(lines[3], ["5h ▰▰▰▰▰▱▱▱▱▱ 52% ▲темп сброс 2ч30м", "7d ▰▱▱▱▱▱▱▱▱▱ 8% сброс 3д11ч"]), [])

    def test_codex_exhausted_limit_keeps_full_bar_over_null_snapshot(self):
        # Исчерпав лимит, Codex дописывает снимок premium с primary/secondary null.
        day = time.strftime("%Y/%m/%d", time.localtime(self.now))
        self.write_rollout(self.codex_limits(primary_used=100.0))
        path = os.path.join(self.home_dir, ".codex", "sessions", *day.split("/"), "rollout-a.jsonl")
        empty = {"limit_id": "premium", "primary": None, "secondary": None}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": empty}}) + "\n")
        rc, out, err, clean = self.run_statusline(self.payload, columns=160)
        lines = self.assert_clean_lines(rc, out, err, clean, count=5)
        self.assertEqual(missing(lines[3], ["5h ▰▰▰▰▰▰▰▰▰▰ 100%", "сброс 2ч30м", "7d ▰▱▱▱▱▱▱▱▱▱ 8%"]), [], lines[3])

    def test_codex_window_past_reset_reads_zero(self):
        self.write_rollout(self.codex_limits(primary_used=90.0, primary_reset=-60), day_offset=1)
        rc, out, err, clean = self.run_statusline(self.payload, columns=160)
        lines = self.assert_clean_lines(rc, out, err, clean, count=5)
        self.assertIn("5h ▱▱▱▱▱▱▱▱▱▱ 0%", lines[3])

    def test_codex_row_absent_without_rollouts(self):
        rc, out, err, clean = self.run_statusline(self.payload, columns=160)
        self.assertNotIn("codex", self.assert_clean_lines(rc, out, err, clean)[3])

    def test_codex_weekly_only_window_has_no_pace_marker(self):
        # Окна 5h у Codex больше нет: primary - недельное; темп, как у Claude, только у окон до суток.
        self.write_rollout({"primary": {"used_percent": 1.0, "window_minutes": 10080, "resets_at": self.now + 604000}, "secondary": None})
        rc, out, err, clean = self.run_statusline(self.payload, columns=160)
        lines = self.assert_clean_lines(rc, out, err, clean, count=5)
        self.assertIn("7d ▱▱▱▱▱▱▱▱▱▱ 1% сброс 6д23ч", lines[3])
