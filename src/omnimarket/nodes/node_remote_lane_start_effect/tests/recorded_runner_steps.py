# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host scripts the frozen runner sent for provisioning steps of one lane start, as recorded (OMN-23425).

Recorded from the runner's ``omni_home_script`` (with ``--remote-omni-home /srv/example/registry``) and
``scratch_script`` (run id ``rlane-example-000001``) on 2026-11-10, and from its ``changes_script`` and
``hold_run_dir_guard`` (lane ``lane-example``, the same registry root; the guard's first line is the
scratch script's variable line) and ``ledger_outbox_script`` the same day. The parity tests compare the ssh
transport's scripts and printed lines with these; a change in either side's text fails them.
"""

RECORDED_OMNI_HOME_SCRIPT = "\n".join(
    [
        'H=""',
        "for d in /srv/example/registry; do",
        '  if [ -n "$d" ] && [ -d "$d/omnibase_infra" ]; then H="$d"; break; fi',
        "done",
        'if [ -z "$H" ]; then echo "REMOTE-OMNI-HOME-NONE $(hostname): none of /srv/example/registry holds omnibase_infra"; exit 96; fi',
        'echo "REMOTE-OMNI-HOME $H"',
    ]
)

RECORDED_SCRATCH_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; W="$LR/runs/rlane-example-000001"; C="$LR/runs/rlane-example-000001.ctx"',
        'mkdir -p "$W" "$C" || exit 70',
        'echo "REMOTE-HOME $HOME"',
    ]
)

RECORDED_CHANGES_SCRIPT = "\n".join(
    [
        'OH=/srv/example/registry; D="$OH/omni_worktrees/"lane-example',
        "rl_guard() {",
        '  t="$1"; tag="$2"; base="$3"; url="$4"',
        '  s=$(git -C "$t" status --porcelain 2>/dev/null) || { echo "keep status-unreadable"; return; }',
        '  if [ -n "$s" ]; then echo "keep uncommitted-changes"; return; fi',
        '  br=$(git -C "$t" symbolic-ref -q --short HEAD 2>/dev/null)',
        '  if [ -n "$br" ]; then',
        "    if git -C \"$t\" rev-parse -q --verify '@{u}' >/dev/null 2>&1; then",
        '      n=$(git -C "$t" rev-list --count \'@{u}..HEAD\' 2>/dev/null) || { echo "keep upstream-unreadable"; return; }',
        '      if [ "$n" = 0 ]; then echo ok; return; fi',
        "      why=unpushed-commits",
        "    else",
        '      n=$(git -C "$t" rev-list --count HEAD --not --remotes 2>/dev/null) || { echo "keep remotes-unreadable"; return; }',
        '      if [ "$n" = 0 ]; then echo ok; return; fi',
        "      why=no-upstream",
        "    fi",
        '  elif [ -n "$base" ]; then',
        '    n=$(git -C "$t" rev-list --count HEAD "^$base" 2>/dev/null) || { echo "keep base-unreadable"; return; }',
        '    if [ "$n" = 0 ]; then echo ok; return; fi',
        "    why=unpushed-commits",
        "  else",
        '    n=$(git -C "$t" rev-list --count HEAD --not --remotes 2>/dev/null) || { echo "keep remotes-unreadable"; return; }',
        '    if [ "$n" = 0 ]; then echo ok; return; fi',
        "    why=unpushed-commits",
        "  fi",
        '  dest="${url:-origin}"',
        '  if GIT_TERMINAL_PROMPT=0 git -C "$t" push -q "$dest" "HEAD:refs/heads/wip/$tag" >/dev/null 2>&1; then',
        '    echo "wip wip/$tag"',
        "  else",
        '    echo "keep $why-push-failed"',
        "  fi",
        "}",
        'if [ ! -d "$D" ]; then exit 0; fi',
        'TREES=$(find "$D" -mindepth 2 -maxdepth 3 -name .git 2>/dev/null |',
        '  while IFS= read -r p; do dirname "$p"; done | LC_ALL=C sort |',
        '  { listed=""; while IFS= read -r t; do',
        '    nested=""',
        "    while IFS= read -r parent; do",
        '      [ -n "$parent" ] || continue',
        '      case "$t" in ("$parent"/*) nested=1; break;; esac',
        "    done <<EOF",
        "$listed",
        "EOF",
        '    [ -n "$nested" ] && continue',
        '    listed="$listed$t',
        '"',
        '    printf "%s\\n" "${t#"$D"/}"',
        "  done; })",
        'printf "%s\\n" "$TREES" | while read -r n; do',
        '  [ -n "$n" ] || continue; t="$D/$n"',
        '  base=$(git -C "$t" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)',
        '  { git -C "$t" diff --name-only "$base"...HEAD 2>/dev/null; git -C "$t" status --porcelain 2>/dev/null | cut -c4-; } |',
        '    sort -u | while read -r f; do [ -n "$f" ] && echo "LANE-CHANGES tree=$t file=$f"; done',
        "done",
        "exit 0",
    ]
)

RECORDED_HOLD_GUARD_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; W="$LR/runs/rlane-example-000001"; C="$LR/runs/rlane-example-000001.ctx"',
        'OH=/srv/example/registry; D="$OH/omni_worktrees/"lane-example',
        'if [ -d "$D" ]; then',
        'TREES=$(find "$D" -mindepth 2 -maxdepth 3 -name .git 2>/dev/null |',
        '  while IFS= read -r p; do dirname "$p"; done | LC_ALL=C sort |',
        '  { listed=""; while IFS= read -r t; do',
        '    nested=""',
        "    while IFS= read -r parent; do",
        '      [ -n "$parent" ] || continue',
        '      case "$t" in ("$parent"/*) nested=1; break;; esac',
        "    done <<EOF",
        "$listed",
        "EOF",
        '    [ -n "$nested" ] && continue',
        '    listed="$listed$t',
        '"',
        '    printf "%s\\n" "${t#"$D"/}"',
        "  done; })",
        "  WP=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' \"$W\")",
        "  CP=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' \"$C\")",
        "  while IFS= read -r n; do",
        '    t="$D/$n"; [ -f "$t/.git" ] || continue',
        '    gd=$(sed -n "s/^gitdir: //p" "$t/.git")',
        '    [ -n "$gd" ] || continue',
        '    case "$gd" in /*) ;; *) gd="$t/$gd";; esac',
        "    gp=$(python3 -c 'import os,sys; print(os.path.realpath(os.path.dirname(sys.argv[1])))' \"$gd\") || continue",
        '    gd="$gp/$(basename "$gd")"; [ -e "$gd" ] || continue',
        '    for root in "$WP" "$CP"; do',
        '      [ -n "$root" ] || continue',
        '      case "$gd" in "$root"/*)',
        '        echo "LANE-WORKDIR kept tree=$W reason=hosts-git-dir-of:$t"; exit 0;;',
        "      esac",
        "    done",
        "  done <<EOF",
        "$TREES",
        "EOF",
        "fi",
        "echo LANE-RETENTION-SAFE",
    ]
)


RECORDED_LEDGER_OUTBOX_SCRIPT = 'cat "$HOME/lab-run/runs/rlane-example-000001.ctx/ledger-outbox.rows" 2>/dev/null; true'


# The runner's lane_guard.reap_script / drain_script and unit_name (the close path, onex_remote_lane.py), recorded on
# 2026-11-10 with the guard's host program replaced by RECORDED_GUARD_SOURCE (the real program is 740 lines; the
# scripts' shape is what is recorded). The unit is for lane "lane-example", run "rlane-example-000001".
RECORDED_GUARD_SOURCE = "print('guard host program')\n"
RECORDED_GUARD_UNIT = "onexlane-lane-example-000001"
RECORDED_GUARD_UNIT_FOR_UNSAFE_NAMES = ("a b/c", "x y-z", "onexlane-a-b-c-z")
_GUARD_PATH_LINE = (
    'export PATH="${ONEX_LANE_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/omni/lane-bin}'
    ':$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"'
)
_GUARD_SOURCE_LINE = """G_SRC='print('"'"'guard host program'"'"')\n'"""
RECORDED_REAP_SCRIPT = "\n".join(
    [
        _GUARD_PATH_LINE,
        _GUARD_SOURCE_LINE,
        'python3 -c "$G_SRC" reap --unit onexlane-lane-example-000001',
    ]
)
RECORDED_DRAIN_SCRIPT = "\n".join(
    [_GUARD_PATH_LINE, _GUARD_SOURCE_LINE, 'python3 -c "$G_SRC" spool-drain']
)


# The runner's stdin-carrying staging steps, recorded on 2026-11-10 for run "rlane-example-000001" with the registry
# root /srv/example/registry: ``ledger_unpack_script`` (read-only snapshot, and the bus mode that keeps the live ledger
# file owner-writable), ``put_file`` for the prompt file, and ``runtime_config_script``. The runner's target sits in
# the host's sibling checkout of the settings repository; the recorded text names that directory ``checkout``, the
# value the overlay declares as ``runtime_config_target``.
RECORDED_LEDGER_UNPACK_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; C="$LR/runs/rlane-example-000001.ctx"',
        'mkdir -p "$C/ledger" || exit 70',
        'tar -xzf - -C "$C/ledger" || exit 70',
        'chmod a-w "$C/ledger/"*.md "$C/ledger/archive/"*.md 2>/dev/null',
        'echo "LEDGER-SNAPSHOT $C/ledger"',
    ]
)
RECORDED_LEDGER_UNPACK_BUS_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; C="$LR/runs/rlane-example-000001.ctx"',
        'mkdir -p "$C/ledger" || exit 70',
        'tar -xzf - -C "$C/ledger" || exit 70',
        'chmod a-w "$C/ledger/"*.md "$C/ledger/archive/"*.md 2>/dev/null',
        'chmod u+w "$C/ledger/example-ledger.md" || exit 70',
        'echo "LEDGER-SNAPSHOT $C/ledger"',
    ]
)
RECORDED_PUT_FILE_SCRIPT = "\n".join(
    [
        "set -eu",
        'T=$(mktemp "$HOME/lab-run/runs/rlane-example-000001.ctx/prompt.md.XXXXXX")',
        "trap 'rm -f -- \"$T\"' EXIT",
        'cat > "$T"',
        'mv -f -- "$T" "$HOME/lab-run/runs/rlane-example-000001.ctx/prompt.md"',
    ]
)
RECORDED_RUNTIME_CONFIG_SCRIPT = "\n".join(
    [
        "T=/srv/example/registry/../checkout/config/onex/runtime/runtime_config.yaml",
        'if [ -f "$T" ]; then cat >/dev/null; echo "RUNTIME-CONFIG present $T"; exit 0; fi',
        'mkdir -p "$(dirname "$T")" || exit 70',
        'cat > "$T.tmp.$$" && mv "$T.tmp.$$" "$T" || exit 70',
        'echo "RUNTIME-CONFIG installed $T"',
    ]
)


# Recorded 2026-11-10 (OMN-23502) from the runner's ``ledger_mirror_script`` (run id ``rlane-example-000001``, ledger
# ``example-ledger.md``), from the report relay's ``remote_script`` (lane ``lane-example``; the recording keeps the
# lines from the report directory on, the registry-root head being the handler's own) and from the omni plugin's
# ``closeout_script`` (the same run id, lane, root and ledger name, no kept trees). The closeout recording names its
# tool repository ``example_tools`` and its ledger variable ``EXAMPLE_LEDGER_FILE`` where the plugin's text names the real ones: the
# transport takes both from the overlay, so the test passes the synthetic ones.
RECORDED_MIRROR_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; C="$LR/runs/rlane-example-000001.ctx"; F="$C/ledger/example-ledger.md"',
        'if [ -s "$F" ] && [ -n "$(tail -c 1 "$F")" ]; then echo >> "$F"; fi',
        'if cat >> "$C/ledger/example-ledger.md"; then R=ok; else R=failed; fi',
        'echo "LEDGER-MIRROR $R"',
    ]
)

RECORDED_MIRROR_RELOCK_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; C="$LR/runs/rlane-example-000001.ctx"; F="$C/ledger/example-ledger.md"',
        'chmod u+w "$F" 2>/dev/null',
        'if [ -s "$F" ] && [ -n "$(tail -c 1 "$F")" ]; then echo >> "$F"; fi',
        'if cat >> "$C/ledger/example-ledger.md"; then R=ok; else R=failed; fi',
        'chmod a-w "$F" 2>/dev/null',
        'echo "LEDGER-MIRROR $R"',
    ]
)

RECORDED_RELAY_REPORTS_SCRIPT = "\n".join(
    [
        'D="$H/.claude_scratch/reports"',
        "names=()",
        'for f in "$D"/lane-example-[0-9][0-9][0-9][0-9]-*.md; do',
        '  if [ -f "$f" ] && [ ! -L "$f" ]; then names+=("${f##*/}"); fi',
        "done",
        "printf '\"'\"'RELAY-TAR\\n'\"'\"'",
        'if [ "${#names[@]}" -gt 0 ]; then tar -cf - -C "$D" "${names[@]}"; fi',
        "'",
    ]
)

RECORDED_CLOSEOUT_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; C="$LR/runs/rlane-example-000001.ctx"; OH=/srv/example/registry; D="$OH/omni_worktrees/"lane-example',
        "rl_guard() {",
        '  t="$1"; tag="$2"; base="$3"; url="$4"',
        '  s=$(git -C "$t" status --porcelain 2>/dev/null) || { echo "keep status-unreadable"; return; }',
        '  if [ -n "$s" ]; then echo "keep uncommitted-changes"; return; fi',
        '  br=$(git -C "$t" symbolic-ref -q --short HEAD 2>/dev/null)',
        '  if [ -n "$br" ]; then',
        "    if git -C \"$t\" rev-parse -q --verify '@{u}' >/dev/null 2>&1; then",
        '      n=$(git -C "$t" rev-list --count \'@{u}..HEAD\' 2>/dev/null) || { echo "keep upstream-unreadable"; return; }',
        '      if [ "$n" = 0 ]; then echo ok; return; fi',
        "      why=unpushed-commits",
        "    else",
        '      n=$(git -C "$t" rev-list --count HEAD --not --remotes 2>/dev/null) || { echo "keep remotes-unreadable"; return; }',
        '      if [ "$n" = 0 ]; then echo ok; return; fi',
        "      why=no-upstream",
        "    fi",
        '  elif [ -n "$base" ]; then',
        '    n=$(git -C "$t" rev-list --count HEAD "^$base" 2>/dev/null) || { echo "keep base-unreadable"; return; }',
        '    if [ "$n" = 0 ]; then echo ok; return; fi',
        "    why=unpushed-commits",
        "  else",
        '    n=$(git -C "$t" rev-list --count HEAD --not --remotes 2>/dev/null) || { echo "keep remotes-unreadable"; return; }',
        '    if [ "$n" = 0 ]; then echo ok; return; fi',
        "    why=unpushed-commits",
        "  fi",
        '  dest="${url:-origin}"',
        '  if GIT_TERMINAL_PROMPT=0 git -C "$t" push -q "$dest" "HEAD:refs/heads/wip/$tag" >/dev/null 2>&1; then',
        '    echo "wip wip/$tag"',
        "  else",
        '    echo "keep $why-push-failed"',
        "  fi",
        "}",
        "rlc_lane=lane-example; rlc_oh=/srv/example/registry",
        "rlc_rm() {",
        '  [ -d "$1" ] && [ ! -L "$1" ] || return 0',
        '  rlc_kb=$(du -sk "$1" 2>/dev/null | cut -f1); rlc_kb=${rlc_kb:-0}',
        '  if rm -rf -- "$1" 2>/dev/null && [ ! -e "$1" ]; then echo "LANE-CACHE-CLOSEOUT removed kb=$rlc_kb path=$1"',
        '  else echo "LANE-CACHE-CLOSEOUT failed kb=$rlc_kb path=$1"; fi',
        "}",
        "rlc_named() {",
        "  for rlc_s in pre-commit pre-commit-cache uv uv-cache; do",
        '    case "$1" in "$rlc_lane-$rlc_s"|*"-$rlc_lane-$rlc_s"|"$rlc_s-$rlc_lane") return 0;; esac',
        "  done",
        "  return 1",
        "}",
        "rlc_kind() {",
        '  case "$1" in pre-commit|pre-commit-cache|uv|uv-cache|.pre-commit|.uv-cache) return 0;; esac',
        "  return 1",
        "}",
        "rlc_scratch() {",
        '  rlc_d="$1/$rlc_lane"',
        '  [ -d "$rlc_d" ] && [ ! -L "$rlc_d" ] || return 0',
        '  for rlc_p in "$rlc_d"/* "$rlc_d"/.[!.]*; do',
        '    [ -d "$rlc_p" ] || continue',
        "    rlc_n=${rlc_p##*/}",
        '    if rlc_kind "$rlc_n" || rlc_named "$rlc_n"; then rlc_rm "$rlc_p"; fi',
        "  done",
        "}",
        "rlc_sweep() {",
        '  [ -n "$rlc_lane" ] || return 0',
        '  for rlc_root in "$HOME/.cache" "${XDG_CACHE_HOME:-}"; do',
        '    [ -n "$rlc_root" ] && [ -d "$rlc_root" ] || continue',
        '    for rlc_p in "$rlc_root"/*; do',
        '      [ -d "$rlc_p" ] || continue',
        '      if rlc_named "${rlc_p##*/}"; then rlc_rm "$rlc_p"; fi',
        "    done",
        "  done",
        '  for rlc_root in "$rlc_oh/.claude_scratch" "$HOME/lab-run/scratch" /mnt/*/omninode/scratch; do rlc_scratch "$rlc_root"; done',
        '  rlc_ifs=$IFS; IFS=:; for rlc_root in ${ONEX_LANE_CACHE_SCRATCH_ROOTS:-}; do IFS=$rlc_ifs; [ -n "$rlc_root" ] && rlc_scratch "$rlc_root"; IFS=:; done; IFS=$rlc_ifs',
        "}",
        "rlc_sweep",
        'if [ ! -d "$D" ]; then echo "LANE-CLOSEOUT-NONE no $D"; exit 0; fi',
        'TREES=$(find "$D" -mindepth 2 -maxdepth 3 -name .git 2>/dev/null |',
        '  while IFS= read -r p; do dirname "$p"; done | LC_ALL=C sort |',
        '  { listed=""; while IFS= read -r t; do',
        '    nested=""',
        "    while IFS= read -r parent; do",
        '      [ -n "$parent" ] || continue',
        '      case "$t" in ("$parent"/*) nested=1; break;; esac',
        "    done <<EOF",
        "$listed",
        "EOF",
        '    [ -n "$nested" ] && continue',
        '    listed="$listed$t',
        '"',
        '    printf "%s\\n" "${t#"$D"/}"',
        "  done; })",
        'if [ -z "$TREES" ]; then echo "LANE-CLOSEOUT-NONE no trees under $D"; exit 0; fi',
        'RH=$(cd "$OH" && pwd -P)',
        'IH=""',
        'for d in "${EXAMPLE_TOOLS_HOME:-}" "$(dirname "$OH")/example_tools" "$(dirname "$RH")/example_tools"; do',
        '  if [ -n "$d" ] && [ -x "$d/.venv/bin/onex-lane-closeout" ]; then IH="$d"; break; fi',
        "done",
        'if [ -z "$IH" ]; then echo "LANE-CLOSEOUT-UNAVAILABLE $(hostname): no example_tools with onex-lane-closeout beside $OH; trees left: $(echo $TREES)"; exit 0; fi',
        'E=$(mktemp "${TMPDIR:-/tmp}/onex-lane-closeout.XXXXXX") || exit 70',
        "KEEP=''",
        'printf "%s\\n" "$TREES" | while read -r n; do',
        '  t="$D/$n"; k=0; wipnote=""',
        '  case " $KEEP " in *" $t "*) echo "LANE-CLOSEOUT rc=0 tree=$t cell=worktree=kept:$t:salvage-failed note="; continue;; esac',
        '  g=$(rl_guard "$t" lane-example-rlane-example-000001-"$(printf "%s" "$n" | tr "/" "-")" "" "")',
        '  case "$g" in',
        '    keep*) echo "LANE-CLOSEOUT rc=0 tree=$t cell=worktree=kept:$t:${g#keep } note="; continue;;',
        '    wip*) wipnote="wip-pushed=${g#wip } ";;',
        "  esac",
        "  while :; do",
        '    cell=$(env -u ONEX_WORKTREES_ROOT OMNI_HOME="$OH" EXAMPLE_TOOLS_HOME="$IH" EXAMPLE_LEDGER_FILE="$C/ledger/example-ledger.md" "$IH/.venv/bin/onex-lane-closeout" --lane lane-example --worktree "$t" 2>"$E" </dev/null); rc=$?',
        '    if [ "$rc" = 3 ] && [ "$k" -lt 3 ]; then',
        '      case "$cell" in *:process-inside*) k=$((k+1)); sleep 6; continue;; esac',
        "    fi",
        "    break",
        "  done",
        '  note="$wipnote$(tr "\\n" " " < "$E" | cut -c1-600)"',
        '  case "$cell" in *:remove-failed)',
        '    case "$g" in wip*)',
        '      queued=$(python3 "$HOME/.local/share/onex-lane-guard/lane_reaper.py" --queue-closeout "$t" --closeout-lane lane-example --closeout-omni-home "$OH" --closeout-command "$IH/.venv/bin/onex-lane-closeout" --pushed-ref "${g#wip }" --failure-file "$E" --closeout-rc "$rc" 2>&1); qr=$?',
        '      note="$note retry-queue-rc=$qr $(printf "%s" "$queued" | tr "\\n" " ")"',
        "      ;; esac",
        "    ;; esac",
        '  echo "LANE-CLOSEOUT rc=$rc tree=$t cell=$cell note=$note"',
        "done",
        'rm -f "$E"',
    ]
)


# Recorded 2026-11-10 (OMN-23502) from the omni plugin's ``plugin_ensure_script`` for one synthetic source: the lines
# that install a tree into the plugin store (a temporary directory renamed into place, then the tree touched as its
# last use). The runner fills the temporary directory from ``git archive``; the ssh transport fills it from the
# archive on its stdin, so the extraction line is the only one that differs.
RECORDED_PLUGIN_TREE_LINES = (
    '  T="$D.tmp.$$"; rm -rf "$T"; mkdir -p "$T" || exit 70',
    '  python3 -c "import os,sys; os.rename(sys.argv[1], sys.argv[2])" "$T" "$D" 2>/dev/null || rm -rf "$T"',
    'touch "$D" 2>/dev/null || true',
)
