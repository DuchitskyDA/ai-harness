#!/bin/sh
# Cross-platform statusline for Claude Code (macOS, Linux, Windows/Git Bash).
# Four "dashboard" lines: place / model / limits / session. sh gathers what
# only it can (git, transcript, date fallback); ONE jq call at the end lays the
# lines out and trims them to $COLUMNS -- no fork per segment, the script runs
# on every turn.

# Canonicalise the platform lazily: `uname` is a process (70-120 ms here), so it
# runs only when something OS-specific is needed (jq missing from PATH, `date`).
OS_KIND=""
detect_os() {
  [ -n "$OS_KIND" ] && return
  OS_KIND=$(uname -s 2>/dev/null)
  case "$OS_KIND" in
    Darwin) OS_KIND="Darwin" ;;
    MINGW*|MSYS*|CYGWIN*) OS_KIND="Windows" ;;
    *) OS_KIND="Linux" ;;
  esac
}

SCRIPT=$0
# $0 may carry backslashes when the host starts sh with a native path.
case "$SCRIPT" in
  *\\*) SCRIPT=${SCRIPT//\\//} ;;
esac
# ~/.claude/statusline-command.sh is a link into the clone; the state dir and the
# jq module (statusline-turns.jq) live next to the target. One fork, only for a link.
if [ -L "$SCRIPT" ]; then
  target=$(readlink "$SCRIPT")
  case "$target" in /*|?:/*) SCRIPT=$target ;; *) SCRIPT="${SCRIPT%/*}/$target" ;; esac
fi

# Make sure jq is reachable even if the statusline subprocess inherits a
# trimmed PATH.
if ! command -v jq >/dev/null 2>&1; then
  detect_os
  case "$OS_KIND" in
    Darwin)
      # jq normally lives at /usr/bin/jq on macOS; Homebrew installs elsewhere.
      PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
      ;;
    Windows)
      # jq installed via WinGet lands under %LOCALAPPDATA%/Microsoft/WinGet/Links,
      # which a trimmed statusline PATH may not include.
      if [ -n "$LOCALAPPDATA" ]; then
        winget_links=${LOCALAPPDATA//\\//}
        PATH="$PATH:${winget_links}/Microsoft/WinGet/Links"
      fi
      ;;
    *)
      ;;
  esac
fi

# Process count is the cost here (every fork is 70-120 ms on Windows), so the
# script runs two jq processes in total. Pass 1 reads the payload straight from
# stdin and returns what sh itself needs (git wants the cwd, the cache the clock) plus the payload re-encoded on one line for
# pass 2 (the layout). Fields are joined on \037 (ASCII unit separator) so empty
# ones stay intact; `// ""` (not `// empty`) keeps every slot filled; s() turns
# a field of the wrong type into "" instead of an error that would lose the other
# fields. -b: no CRLF from a Windows jq build. The line is split with parameter
# expansion (no fork, and a heredoc would eat the payload's backslashes).
US=$(printf '\037')
pass1=$(jq -b -r --arg sep "$US" '
  def s(f): (f? | strings) // "";
  (first(s(.cwd), s(.workspace.current_dir) | select(. != "")) // "") as $cwd
  | [
    $cwd,
    s(.workspace.git_worktree),
    s(.transcript_path),
    (if (try (.rate_limits.seven_day | .used_percentage != null and .resets_at == null) catch false) then "1" else "" end),
    ($cwd | gsub("[^A-Za-z0-9_.-]"; "_") | .[:150]),
    (now | floor | tostring),
    (now, now - 86400 | try (localtime | strftime("%Y/%m/%d")) catch (gmtime | strftime("%Y/%m/%d"))),
    tojson
  ] | join($sep)
' 2>/dev/null)
rest="$pass1$US"
cwd=${rest%%"$US"*}; rest=${rest#*"$US"}
worktree=${rest%%"$US"*}; rest=${rest#*"$US"}
transcript=${rest%%"$US"*}; rest=${rest#*"$US"}
need_dow=${rest%%"$US"*}; rest=${rest#*"$US"}
cwd_key=${rest%%"$US"*}; rest=${rest#*"$US"}
now_s=${rest%%"$US"*}; rest=${rest#*"$US"}
codex_today=${rest%%"$US"*}; rest=${rest#*"$US"}
codex_yesterday=${rest%%"$US"*}; rest=${rest#*"$US"}
payload=${rest%"$US"}

# local weekday (ISO 1=Mon..7=Sun) for the weekly budget when the payload has
# no seven_day.resets_at. One `date` call returns midnight epoch and weekday.
today_midnight_epoch_and_dow() {
  case "$OS_KIND" in
    Darwin)
      date -v0H -v0M -v0S +'%s %u' 2>/dev/null
      ;;
    *)
      # GNU date: Linux, and Git Bash's coreutils date on Windows.
      date -d today +'%s %u' 2>/dev/null
      ;;
  esac
}
dow=""
if [ -n "$need_dow" ]; then
  detect_os
  read -r _ dow <<EOF
$(today_midnight_epoch_and_dow)
EOF
fi

# Codex limits: the account-wide rate_limits snapshot of the last token_count
# event in the newest rollout of today or yesterday (local days), cached for 60 s
# in "<epoch>\n<event line>". No rollout or no snapshot keeps the previous line:
# the windows stay valid until their resets_at. -nt is a builtin, so the search
# itself forks nothing; grep and tail run once a minute at most.
cache_dir="${XDG_CACHE_HOME:-${HOME:-${TMPDIR:-/tmp}}/.cache}/claude-statusline"
codex_cache="$cache_dir/codex-limits"
codex_home="${CODEX_HOME:-${HOME:-$USERPROFILE}/.codex}"
case "$codex_home" in
  *\\*) codex_home=${codex_home//\\//} ;;
esac
cx_at=""
cx_line=""
if [ -f "$codex_cache" ]; then
  { IFS= read -r cx_at; IFS= read -r cx_line; } < "$codex_cache" 2>/dev/null
fi
case "$cx_at$now_s" in
  *[!0-9]*) cx_stale=1 ;;
  *) if [ -n "$cx_at" ] && [ -n "$now_s" ] && [ "$now_s" -ge "$cx_at" ] && [ $(( now_s - cx_at )) -lt 60 ]; then cx_stale=""; else cx_stale=1; fi ;;
esac
if [ -n "$cx_stale" ] && [ -n "$now_s" ] && [ -n "$codex_today" ] && [ -d "$codex_home/sessions" ]; then
  newest=""
  for rollout in "$codex_home/sessions/$codex_yesterday"/rollout-*.jsonl "$codex_home/sessions/$codex_today"/rollout-*.jsonl; do
    [ -f "$rollout" ] || continue
    if [ -z "$newest" ] || [ "$rollout" -nt "$newest" ]; then newest=$rollout; fi
  done
  if [ -n "$newest" ]; then
    # Past an exhausted limit Codex appends a "premium" snapshot with null
    # windows; only a snapshot with a primary window counts. The newest rollout
    # may hold none (a session refused at once), then the others are searched.
    pat='"rate_limits": *{[^}]*"primary": *{'
    found=$(grep "$pat" "$newest" 2>/dev/null | tail -n 1)
    [ -n "$found" ] || found=$(cat "$codex_home/sessions/$codex_yesterday"/rollout-*.jsonl "$codex_home/sessions/$codex_today"/rollout-*.jsonl 2>/dev/null | grep "$pat" | tail -n 1)
    [ -n "$found" ] && cx_line=$found
  fi
  if { [ -d "$cache_dir" ] || mkdir -p "$cache_dir" 2>/dev/null; }; then
    printf '%s\n%s\n' "$now_s" "$cx_line" > "$codex_cache.$$" 2>/dev/null \
      && mv -f "$codex_cache.$$" "$codex_cache" 2>/dev/null
  fi
fi

set --
[ -f "$codex_cache" ] && set -- "$@" --rawfile cx "$codex_cache"

# Git facts (branch, changed files, ahead/behind) come from one `git status`,
# cached per cwd for 10 s: the statusline redraws on every event, git is the
# slowest part and the facts do not change that fast. A cache line is
# "<epoch>\037<branch>\037<changed>\037<ahead>\037<behind>"; it is written to a
# temp name and renamed, so a reader never sees half a line.
branch=""
changed=""
ahead=""
behind=""
if [ -n "$cwd" ]; then
  cache_file="$cache_dir/git-$cwd_key"
  cached=""
  if [ -f "$cache_file" ]; then
    IFS="$US" read -r c_at c_branch c_changed c_ahead c_behind < "$cache_file" 2>/dev/null
    case "$c_at" in
      ""|*[!0-9]*) ;;
      *)
        case "$now_s" in
          ""|*[!0-9]*) ;;
          *) [ "$now_s" -ge "$c_at" ] && [ $(( now_s - c_at )) -lt 10 ] && cached=1 ;;
        esac
        ;;
    esac
  fi
  if [ -n "$cached" ]; then
    branch=$c_branch; changed=$c_changed; ahead=$c_ahead; behind=$c_behind
  else
    git_status=$(git -C "$cwd" status --porcelain=v2 --branch 2>/dev/null)
    if [ -n "$git_status" ]; then
      changed=0
      while IFS= read -r gline; do
        case "$gline" in
          "# branch.head "*)
            branch="${gline#\# branch.head }"
            [ "$branch" = "(detached)" ] && branch=""
            ;;
          "# branch.ab "*)
            ab="${gline#\# branch.ab }"
            ahead="${ab#+}"; ahead="${ahead%% *}"
            behind="${ab##* -}"
            ;;
          "#"*) ;;
          *) changed=$(( changed + 1 )) ;;
        esac
      done <<EOF
$git_status
EOF
    fi
    if [ -n "$now_s" ] && { [ -d "$cache_dir" ] || mkdir -p "$cache_dir" 2>/dev/null; }; then
      tmp_file="$cache_file.$$"
      printf '%s\037%s\037%s\037%s\037%s\n' "$now_s" "$branch" "$changed" "$ahead" "$behind" > "$tmp_file" 2>/dev/null \
        && mv -f "$tmp_file" "$cache_file" 2>/dev/null
    fi
  fi
fi
[ -z "$branch" ] && branch="$worktree"

# Turn count: user records that are neither tool results nor meta; tok/s of the last
# turn with output: per API response, from api-request (or the user record before it)
# to its last assistant record, tool time excluded - as the Codex Stop hook
# (harness/runtime/codex_speed.py) counts it. The layout jq
# streams the transcript as a raw file (inputs), so a long session costs no memory.
if [ -n "$transcript" ] && [ -f "$transcript" ]; then
  set -- "$@" "$transcript"
else
  transcript=""
fi

# Layout program (jq). Every row is {l: label, g: [group, ...]}; a group is a
# list of parts {k: drop step (0 = never), p: separator before, s: text, r: red
# = never dropped on steps 1-5}. Width is counted in code points after
# stripping ANSI by hand (pl), so no regex is needed. Drop order while a row is
# longer than COLUMNS-1: 1 miss cause, 2 day budget, 3 cost, 4 +/- lines, 5
# resets (row by row); 6 row labels (all rows; then steps 1-5 apply to all rows
# too, so a surviving detail never outlives a later-dropped one); 7 the tail is
# cut and gets an ellipsis. The 5h pace marker and anything red survive to 7.
LAYOUT='
include "statusline-turns";
"\u001b[0;38;5;115m" as $G | "\u001b[0;38;5;222m" as $Y | "\u001b[0;38;5;210m" as $RD | "\u001b[0;38;5;109m" as $DM
| "\u001b[0;38;5;152m" as $LB | "\u001b[0;38;5;255m" as $WH | "\u001b[0;38;5;117m" as $CY | "\u001b[0;38;5;183m" as $MG
| "\u001b[1;38;5;255m" as $BD | "\u001b[0m" as $RS
| def k($c): if . == "" then "" else $c + tostring + $RS end;
def num: tonumber? // null;
def s(f): (f? | strings) // "";
def o(f): (f? | objects) // {};
def when(c; x): if c then x else empty end;
def p2: if . < 10 then "0\(.)" else "\(.)" end;
def P($k; $pre; $s): {k: $k, p: $pre, s: $s};
def base: [split("/")[] | split("\\")[] | select(. != "")] | last // "~";
def pl: split("\u001b") | .[0] + (.[1:] | map(.[index("m") + 1:]) | add // "");
def pc: if . < 50 then $G elif . < 80 then $Y else $RD end;
def tc: if . < 60 then $G elif . < 120 then $Y else $RD end;
def cc: if . >= 90 then $G elif . >= 70 then $Y else $RD end;
def bar($c): ([(. * 10 + 50) / 100 | floor, 10] | min | [., 0] | max) as $f
  | ("▰▰▰▰▰▰▰▰▰▰"[:$f] | k($c)) + ("▱▱▱▱▱▱▱▱▱▱"[$f:] | k($DM));
def meter($lab; $v): if $v == null then $lab + " " + ("—" | k($LB))
  else ($v | floor) as $i | ($i | pc) as $c | $lab + " " + ($i | bar($c)) + " " + ("\($i)%" | k($c)) end;
def hm: ((. + 30) / 60 | floor) as $m | if $m >= 60 then "\($m / 60 | floor)ч\($m % 60 | p2)м" else "\($m)м" end;
def rst: ("сброс" | k($LB)) + " " + k($WH);
def dh: (. / 86400 | floor) as $d | (. % 86400 / 3600 | floor) as $h
  | if $d > 0 then "\($d)д\($h)ч" elif $h > 0 then "\($h)ч" else "\(. / 60 | floor)м" end;
def dur: (. / 1000 | floor) as $s
  | if $s < 60 then "\($s)с" elif $s < 3600 then "\($s / 60 | floor)м" else "\($s / 3600 | floor)ч\($s % 3600 / 60 | floor)м" end;
def usd: (. * 100 + 0.5 | floor) as $c | "$\($c / 100 | floor).\($c % 100 | p2)";
def wlab: if . == null then "?" elif . % 1440 == 0 then "\(. / 1440)d" else "\(. / 60 | floor)h" end;
def cxwin($now): objects | (.used_percent | num) as $u | (.resets_at | num) as $r | (.window_minutes | num) as $m
  | if $r != null and $r <= $now then {u: 0, r: null, m: $m} else {u: $u, r: $r, m: $m} end
  | .share = (if .r == null or (.m // 0) <= 0 then null else ([[(.m * 60 - (.r - $now)) * 100 / (.m * 60), 0] | max, 100] | min) end);
def cxgroup($now): (.m // 0) as $m | [ P(0; ""; meter(.m | wlab | k($LB); .u)),
    when(.u != null and .share != null and .u > .share and (.m // 0) <= 1440; P(0; " "; "▲темп" | k($RD))),
    when(.r != null; P(5; " "; [.r - $now, 0] | max | floor | (if $m > 1440 then dh else hm end) | rst)) ];
def grp($n): map(select(.k == 0 or .k > $n or .r)) | to_entries
  | map((if .key == 0 then "" else .value.p end) + .value.s) | join("");
def gapped($gs; $w): ([$gs | to_entries[] | select(.value != "") | .key] | max // -1) as $li
  | [range(0; $li + 1) | . as $i | $gs[$i] | if $i < $li then . + (" " * ([$w[$i] - (pl | length), 0] | max)) else . end] | join("   ");
def ln($n; $lab; $w): . as $x | ($x.g | map(grp($n))) as $gs
  | (if $w == null then ($gs | map(select(. != "")) | join("   ")) else gapped($gs; $w) end) as $body
  | (if $lab then ($x.l | k($LB)) else "" end) + $body;
def ln($n; $lab): ln($n; $lab; null);
def widths($n): map(.g | map(grp($n) | pl | length)) as $m | [range(0; 4) | . as $i | [$m[][$i] // 0] | max];
def lvl($L): first(range(0; 7) as $n | select(ln($n; true) | pl | length <= $L) | $n) // 6;
def fitted($L): . as $rows | map(lvl($L)) | max as $plain
  | first(range(0; 7) as $n | ($rows[1:] | widths($n)) as $w | ([$rows[0] | ln($n; true)] + ($rows[1:] | map(ln($n; true; $w)))) as $al
      | if all($al[]; pl | length <= $L) then $al elif $plain <= $n then ($rows | map(ln(lvl($L); true))) else empty end);
def trunc($n): reduce (split("\u001b") | to_entries[]) as $e ({o: "", u: 0};
    ($e.value | if $e.key == 0 then ["", .] else ["\u001b" + .[:(index("m") + 1)], .[(index("m") + 1):]] end) as [$esc, $t]
    | if .u >= $n then . else .o += $esc + $t[:($n - .u)] | .u += ($t | length) end) | .o + $RS + "…";
(now | floor) as $now
| turns as $red
| $red.n as $tn
| (if $red.c.t > 0 then $red.c else $red.v end) as $last
| (if $tr == "" then null else $tn end) as $turns
| ((try ($inp | fromjson) catch null) | objects // {}) as $p
| (((try ($ARGS.named.cx // "" | split("\n")[1] | fromjson | .payload.rate_limits) catch null) | objects | select([.primary, .secondary] | any(type == "object"))) // null) as $cxl
| o($p.rate_limits.five_hour) as $fh | o($p.rate_limits.seven_day) as $sd
| o($p.cost) as $co | o($p.prompt_cache) as $pk | o($p.pr) as $pr
| ($fh.used_percentage | num) as $fhu | ($fh.resets_at | num) as $fhr
| ($sd.used_percentage | num) as $wk | ($sd.resets_at | num) as $wr
| ($p.context_window.used_percentage? | num) as $ctx | ($co.total_api_duration_ms | num) as $ms
| (if $tr != "" and $last.t > 0 and $last.s > 0 then $last.t / $last.s | round else null end) as $tps
| ($turns | num) as $t | ($chg | num) as $cn | ($ahead | num) as $an | ($behind | num) as $bn
| ($pk.misses | num) as $mi | ($co.total_lines_added | num) as $la | ($co.total_lines_removed | num) as $lr
| ($co.total_cost_usd | num) as $cost | ($pk.expires_at | num) as $ex | s($pk.last_miss_cause.causes[0]) as $cause
| ($pk.hit_ratio | num | if . == null then null else (. * 10000 | round + 50) / 100 | floor end) as $hit
| ([$p.workspace.project_dir?, $p.cwd?, $p.workspace.current_dir?] | map(strings | select(. != "")) | .[0] // "" | base) as $dir
| (($p.thinking.enabled? // false) == true) as $think
| (if $pr.kind == "mr" then "MR !" else "PR #" end) as $prk | ($pr.number? | num) as $prn
| ({approved: [" ✓ одобрен", $G], pending: [" ждёт ревью", $Y], changes_requested: [" ✗ правки", $RD], draft: [" черновик", $LB]}[s($pr.review_state)]) as $pst
| (if $prn == null then "" elif $pst then ($prk + "\($prn)" + $pst[0] | k($pst[1])) else ($prk + "\($prn)" | k($WH)) end) as $prt
| (if $fhr == null then null else ([[(18000 - ($fhr - $now)) * 100 / 18000, 0] | max, 100] | min) end) as $share
| (if $wk == null then null else
    ((if $wr != null then (($now - ($wr - 604800)) / 86400 | floor) else (($dow | num) // 1) - 1 end) | [., 0] | max | [., 6] | min) as $de
    | (($de + 1) * 100 / 7) as $al | ($al - $wk) as $left
    | {txt: "\($wk + 0.5 | floor)/\($al + 0.5 | floor)%", red: ($left < 0),
       col: (if $left < 0 then $RD elif $left < (100 / 14) then $Y else $G end)}
  end) as $day
| [
  {l: "место  ", g: [
    [ P(0; ""; $dir | k($BD)),
      when($branch != ""; P(0; " "; ("⎇" | k($LB)) + " " + ($branch | k($WH)))),
      when($cn != null; P(0; " "; if $cn > 0 then "±\($cn)" | k($Y) else "±\($cn)" | k($WH) end)),
      when(($an // 0) > 0; P(0; " "; "↑\($an)" | k($CY))),
      when(($bn // 0) > 0; P(0; " "; "↓\($bn)" | k($RD))) ],
    [ when($prn != null; P(0; ""; $prt)) ] ]},
  {l: "модель ", g: [
    [ when(s($p.model.display_name) != ""; P(0; ""; s($p.model.display_name) | k($MG))),
      when(s($p.effort.level) != ""; P(0; " · "; s($p.effort.level) | k($WH))),
      when($think; P(0; " · "; "think " + ("●" | k($G)))) ],
    [ P(0; ""; meter("ctx" | k($LB); $ctx)) ],
    [ P(0; ""; ("ход" | k($LB)) + " " + (if ($t // 0) > 0 then "\($t)" | k($t | tc) else "—" | k($LB) end)) ],
    [ P(0; ""; ("работа" | k($LB)) + " " + (if $ms != null then ($ms | dur | k($WH)) else "—" | k($LB) end)),
      when($tps != null; P(1; " · "; "\($tps) tok/s" | k($WH))) ] ]},
  {l: "claude ", g: [
    [ P(0; ""; meter("5h" | k($LB); $fhu)),
      when($fhu != null and $share != null and $fhu > $share; P(0; " "; "▲темп" | k($RD))),
      when($fhr != null; P(5; " "; [$fhr - $now, 0] | max | floor | hm | rst)) ],
    [ P(0; ""; meter("7d" | k($LB); $wk)),
      when($wr != null; P(5; " "; [$wr - $now, 0] | max | floor | dh | rst)) ],
    [ P(2; ""; ("сутки" | k($LB)) + " " + (if $day then $day.txt | k($day.col) else "—" | k($LB) end))
      + (if $day.red then {r: true} else {} end) ] ]},
  (if $cxl == null then empty else
    {l: "codex  ", g: [$cxl.primary, $cxl.secondary | cxwin($now) | cxgroup($now)]} end),
  {l: "сессия ", g: [
    [ P(0; ""; ("кэш " | k($LB)) + (if $hit != null then "\($hit)%" | k($hit | cc) else "—" | k($LB) end)),
      when(($mi // 0) > 0; P(0; " "; "✗\($mi)" | k($Y))),
      (if $pk.warm == false then P(0; " · "; "остыл" | k($Y))
       elif $pk.warm == true and $ex != null then ([($ex - $now) / 60 | ceil, 0] | max) as $w
         | P(0; " · "; "тёплый \($w)м" | k(if $w > 5 then $G else $Y end))
       else empty end),
      when($cause != ""; P(1; " "; "(\($cause))" | k($LB))) ],
    [ when($cost != null; P(3; ""; $cost | usd | k($LB))) ],
    [ when(($la // 0) > 0 or ($lr // 0) > 0; P(4; ""; "+\($la // 0)" | k($G))),
      when(($la // 0) > 0 or ($lr // 0) > 0; P(4; " "; "−\($lr // 0)" | k($RD))) ] ]}
] as $rows
| (($cols | num) as $c | if $c != null and $c > 0 and $c == ($c | floor) then ([$c - 1, 1] | max) else null end) as $L
| if $L == null then $rows | map(ln(0; true))
  else (any($rows[]; ln(6; true) | pl | length > $L)) as $tight
    | (if $tight then $rows | map(ln(6; false)) else $rows | fitted($L) end)
    | map(if (pl | length) > $L then trunc($L - 1) else . end)
  end | join("\n")
'
# Safety net: an empty statusline is hidden by the CLI without a word, so a broken
# layout (a jq error, a bad field) prints its reason in red instead of nothing.
out=$(jq -n -R -b -j -L "${SCRIPT%/*}" --arg inp "$payload" --arg tr "$transcript" --arg branch "$branch" --arg chg "$changed" \
  --arg ahead "$ahead" --arg behind "$behind" --arg dow "$dow" --arg cols "$COLUMNS" \
  "$LAYOUT" "$@" </dev/null 2>&1) && [ -n "$out" ] && { printf '%s' "$out"; exit 0; }
printf '\033[1;91m⚠ статуслайн не собрался:\033[0m %s' "$(printf '%s' "${out:-пустой вывод}" | head -n 1 | cut -c1-140)"
exit 0
