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
