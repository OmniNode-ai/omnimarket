# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Copy a remote lane's report files to the launching host (OMN-17427).

A lane writes its full report to ``$ONEX_REGISTRY_ROOT/.claude_scratch/reports/<lane>-<YYYY-MM-DDTHHMMZ>.md`` on the host it
ran on, so a lane placed on a lab host left a report nobody on the launching host could read. One ssh call tars the
lane's own report files from the host and each lands in the launching host's reports directory under the same name,
never over an existing file. A lane that wrote no report leaves its final message there instead, headed as such.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from omnimarket.handlers.overlay_keys import resolve_overlay_key

from ..models.model_lane_report_relay import (
    ModelLaneReportRelayRequest,
    ModelLaneReportRelayResult,
)

POLICY = json.loads(
    (Path(__file__).resolve().parents[1] / "contract.yaml").read_text()
)["policy"]
LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
STAMP = r"\d{4}-\d{2}-\d{2}T\d{4}Z"
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ServerAliveInterval=30",
)


def remote_script(remote_registry_root: str | None, lane: str) -> str:
    """Remote bash: list this lane's regular report files (a symlink is not one), then tar them."""
    if remote_registry_root:
        home = f"H={shlex.quote(remote_registry_root)}\n"
    else:
        candidates = resolve_overlay_key("lab_hosts.registry_root_candidates")
        # Candidate syntax was validated by the overlay model. HOME expands on the host.
        words = " ".join(
            '"' + value.replace("~", "$HOME", 1) + '"' for value in candidates
        )
        home = (
            'H="${ONEX_REGISTRY_ROOT:-}"\n'
            'if [ -z "$H" ]; then\n'
            f"  for candidate in {words}; do\n"
            '    if [ -d "$candidate" ]; then H="$candidate"; break; fi\n'
            "  done\nfi\n"
        )
    home += 'if [ -z "$H" ]; then printf "RELAY-NO-HOME\\n"; exit 0; fi\n'
    body = home + (
        'D="$H/.claude_scratch/reports"\n'
        "names=()\n"
        f'for f in "$D"/{lane}-[0-9][0-9][0-9][0-9]-*.md; do\n'
        '  if [ -f "$f" ] && [ ! -L "$f" ]; then names+=("${f##*/}"); fi\n'
        "done\n"
        "printf 'RELAY-TAR\\n'\n"
        'if [ "${#names[@]}" -gt 0 ]; then tar -cf - -C "$D" "${names[@]}"; fi\n'
    )
    return f"# lane={lane}\nbash -c {shlex.quote(body)}"


def tar_members(
    payload: bytes, name_re: re.Pattern[str]
) -> tuple[dict[str, tuple[bytes, float]], list[str]]:
    """({name: (bytes, mtime)}, [names too large]): only plain files named like this lane's report."""
    found: dict[str, tuple[bytes, float]] = {}
    too_large: list[str] = []
    if not payload:
        return found, too_large
    limit = POLICY["report_max_bytes"]
    with tarfile.open(fileobj=BytesIO(payload), mode="r:") as archive:
        for member in archive:
            if (
                not member.isfile()
                or not name_re.fullmatch(member.name)
                or member.name in found
            ):
                continue
            source = archive.extractfile(member)
            data = source.read(limit + 1) if source is not None else b""
            if len(data) > limit:
                too_large.append(member.name)
            else:
                found[member.name] = (data, member.mtime)
    return found, too_large


def write_once(
    directory: Path, name: str, data: bytes, mtime: float | None = None
) -> bool:
    """Create ``directory/name`` with ``data`` unless it exists; True when this call created it."""
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix=".relay-", delete=False
    ) as tmp:
        staged = Path(tmp.name)
        tmp.write(data)
    try:
        if mtime is not None:
            os.utime(staged, (mtime, mtime))
        try:
            os.link(
                staged, directory / name
            )  # fails when the name exists: the first report stands
        except FileExistsError:
            return False
        return True
    finally:
        staged.unlink(missing_ok=True)


class HandlerLaneReportRelay:
    def handle(
        self, request: ModelLaneReportRelayRequest
    ) -> ModelLaneReportRelayResult:
        """Best effort only: the result carries any failure, nothing is raised."""
        copied: list[str] = []
        already: list[str] = []
        missing: list[str] = []
        from_result: str | None = None
        error: str | None = None
        try:
            if not request.local_registry_root:
                return ModelLaneReportRelayResult(
                    error="ONEX_REGISTRY_ROOT is unset on the launching host"
                )
            if LANE_RE.fullmatch(request.lane) is None:
                return ModelLaneReportRelayResult(
                    error="lane name is not a plain token"
                )
            name_re = re.compile(re.escape(request.lane) + "-" + STAMP + r"\.md")
            directory = (
                Path(request.local_registry_root) / ".claude_scratch" / "reports"
            )
            timeout = POLICY["ssh_timeout_seconds"]
            argv = [
                "ssh",
                *SSH_OPTIONS,
                request.target,
                remote_script(request.remote_registry_root, request.lane),
            ]
            try:
                proc = (request.run or subprocess.run)(
                    argv, capture_output=True, check=False, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                error = f"timed out after {timeout}s"
            except OSError as exc:
                error = str(exc)
            else:
                stdout = (
                    proc.stdout.encode()
                    if isinstance(proc.stdout, str)
                    else (proc.stdout or b"")
                )
                head, separator, tail = stdout.partition(b"RELAY-TAR\n")
                if proc.returncode != 0:
                    stderr = (
                        proc.stderr.decode("utf-8", errors="replace")
                        if isinstance(proc.stderr, bytes)
                        else str(proc.stderr or "")
                    )
                    error = f"ssh rc={proc.returncode}: {stderr[-200:]}"
                elif b"RELAY-NO-HOME" in head:
                    error = "no registry root on the host"
                elif not separator:
                    error = "host report lacks RELAY-TAR"
                else:
                    found, too_large = tar_members(tail, name_re)
                    missing = sorted(too_large)
                    for name in sorted(found):
                        data, mtime = found[name]
                        (
                            copied
                            if write_once(directory, name, data, mtime)
                            else already
                        ).append(name)
            have = (
                copied
                or already
                or any(
                    name_re.fullmatch(p.name)
                    for p in (directory.iterdir() if directory.is_dir() else ())
                )
            )
            if not have and request.result_text.strip():
                stamp = (
                    (request.now or datetime.now(UTC))
                    .astimezone(UTC)
                    .strftime("%Y-%m-%dT%H%MZ")
                )
                name = f"{request.lane}-{stamp}.md"
                header = f"<!-- No report file reached this host: this is the lane's final message, relayed from host {request.host_name} -->\n\n"
                if write_once(
                    directory,
                    name,
                    (header + request.result_text.strip() + "\n").encode("utf-8"),
                ):
                    from_result = name
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        return ModelLaneReportRelayResult(copied, already, missing, from_result, error)


# Preservation guards shared by artifact archival and host working-tree closeout.
LANE_TREES_SH = 'TREES=$(find "$D" -mindepth 2 -maxdepth 3 -name .git 2>/dev/null |\n  while IFS= read -r p; do dirname "$p"; done | LC_ALL=C sort |\n  { listed=""; while IFS= read -r t; do\n    nested=""\n    while IFS= read -r parent; do\n      [ -n "$parent" ] || continue\n      case "$t" in ("$parent"/*) nested=1; break;; esac\n    done <<EOF\n$listed\nEOF\n    [ -n "$nested" ] && continue\n    listed="$listed$t\n"\n    printf "%s\\n" "${t#"$D"/}"\n  done; })'


def workdir_guard_function() -> str:
    """POSIX sh defining ``rl_guard <tree> <tag> <base> <url>``, which prints one verdict line and never
    deletes anything: ``ok`` (nothing would be lost), ``wip <branch>`` (the commits were pushed to
    ``wip/<tag>``) or ``keep <reason>`` (the tree must stay). ``base`` is the sha a detached worktree was
    made at and ``url`` the push target for it; both empty means the tree's own ``origin``."""
    return 'rl_guard() {\n  t="$1"; tag="$2"; base="$3"; url="$4"\n  s=$(git -C "$t" status --porcelain 2>/dev/null) || { echo "keep status-unreadable"; return; }\n  if [ -n "$s" ]; then echo "keep uncommitted-changes"; return; fi\n  br=$(git -C "$t" symbolic-ref -q --short HEAD 2>/dev/null)\n  if [ -n "$br" ]; then\n    if git -C "$t" rev-parse -q --verify \'@{u}\' >/dev/null 2>&1; then\n      n=$(git -C "$t" rev-list --count \'@{u}..HEAD\' 2>/dev/null) || { echo "keep upstream-unreadable"; return; }\n      if [ "$n" = 0 ]; then echo ok; return; fi\n      why=unpushed-commits\n    else\n      n=$(git -C "$t" rev-list --count HEAD --not --remotes 2>/dev/null) || { echo "keep remotes-unreadable"; return; }\n      if [ "$n" = 0 ]; then echo ok; return; fi\n      why=no-upstream\n    fi\n  elif [ -n "$base" ]; then\n    n=$(git -C "$t" rev-list --count HEAD "^$base" 2>/dev/null) || { echo "keep base-unreadable"; return; }\n    if [ "$n" = 0 ]; then echo ok; return; fi\n    why=unpushed-commits\n  else\n    n=$(git -C "$t" rev-list --count HEAD --not --remotes 2>/dev/null) || { echo "keep remotes-unreadable"; return; }\n    if [ "$n" = 0 ]; then echo ok; return; fi\n    why=unpushed-commits\n  fi\n  dest="${url:-origin}"\n  if GIT_TERMINAL_PROMPT=0 git -C "$t" push -q "$dest" "HEAD:refs/heads/wip/$tag" >/dev/null 2>&1; then\n    echo "wip wip/$tag"\n  else\n    echo "keep $why-push-failed"\n  fi\n}'


def guarded_cleanup(cleanup: str, *, tag: str, base: str, url: str) -> str:
    """``cleanup`` (the controller's step, whose first line defines ``W``) behind the guard on ``$W``: a
    tree that must stay stops the step before anything is removed. Prints one ``LANE-WORKDIR`` line."""
    head, _, rest = cleanup.partition("\n")
    return "\n".join(
        [
            head,
            workdir_guard_function(),
            f'g=$(rl_guard "$W" {shlex.quote(tag)} {shlex.quote(base)} {shlex.quote(url)})',
            'case "$g" in',
            '  keep*) echo "LANE-WORKDIR kept tree=$W reason=${g#keep }"; exit 0;;',
            '  wip*) echo "LANE-WORKDIR wip-pushed tree=$W branch=${g#wip }";;',
            '  *) echo "LANE-WORKDIR clean tree=$W";;',
            "esac",
            rest,
        ]
    )


def hold_run_dir_guard(cleanup: str, *, lane: str, registry_root: str) -> str:
    """Keep the run directory while a lane tree's surviving git dir lives inside it (OMN-17427)."""
    head, _, rest = cleanup.partition("\n")
    return "\n".join(
        [
            head,
            f'OH={shlex.quote(registry_root)}; D="$OH/omni_worktrees/"{shlex.quote(lane)}',
            'if [ -d "$D" ]; then',
            LANE_TREES_SH,
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
            rest,
        ]
    )


CHANGES_LINE_RE = re.compile(r"^LANE-CHANGES tree=(\S+) file=(.+)$")


def changes_script(lane: str, registry_root: str) -> str:
    """Remote shell (POSIX sh): print ``LANE-CHANGES tree= file=`` for every file each of the lane's trees changed
    against its base (commits since the merge base with the default branch, plus uncommitted changes). It runs
    before the closeout, which removes the trees. It writes nothing and fails nothing (moved from the runner, OMN-23425)."""
    return "\n".join(
        [
            f'OH={shlex.quote(registry_root)}; D="$OH/omni_worktrees/"{shlex.quote(lane)}',
            workdir_guard_function(),
            'if [ ! -d "$D" ]; then exit 0; fi',
            LANE_TREES_SH,
            'printf "%s\\n" "$TREES" | while read -r n; do',
            '  [ -n "$n" ] || continue; t="$D/$n"',
            '  base=$(git -C "$t" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)',
            '  { git -C "$t" diff --name-only "$base"...HEAD 2>/dev/null; git -C "$t" status --porcelain 2>/dev/null | cut -c4-; } |',
            '    sort -u | while read -r f; do [ -n "$f" ] && echo "LANE-CHANGES tree=$t file=$f"; done',
            "done",
            "exit 0",
        ]
    )


def lab_work_script(script: str, declared_root: str | None) -> str:
    """Render existing host templates against the table's optional data disk root.

    All remote steps pass through this one renderer, so the engine, snapshots,
    git-dir guards and cleanup agree. Defaults remain in the host table alone.
    """
    if declared_root is None:
        return script
    root = declared_root
    if not re.fullmatch(r"/[A-Za-z0-9_./+-]+/lab-run", root) or ".." in root.split("/"):
        raise ValueError("unsafe declared lab root")
    return script.replace("$HOME/lab-run", str(root)).replace(
        "${HOME}/lab-run", str(root)
    )


# ---------------------------------------------------------------------------------------------------
# Closeout on the host that owns the worktree (OMN-20148), moved from the omni plugin's handler (OMN-23502).
# The texts are the frozen runner's, byte for byte: tests/recorded_runner_steps.py holds the recording and the
# ssh transport's parity test fails when either side changes.
# ---------------------------------------------------------------------------------------------------


def wip_tag(lane: str, run_id: str) -> str:
    """The WIP branch the guard pushes to: ``wip/<tag>``, one per lane run."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", f"{lane}-{run_id}").strip("-.")


# A remote lane's trees live on the lab host, so the launching host cannot remove them, and no host had
# the closeout command installed. The lane is told to make its trees under ``$OMNI_HOME/omni_worktrees/
# <lane>/<repo>`` (the footer), and when it ends the runner runs the host's own ``onex-lane-closeout`` for
# every tree under that directory, before the TERMINAL is written. The directory is the ownership proof:
# a lane name is unique among open CLAIMs, so no other lane's tree is ever touched. The consent row is
# the operator's of 2026-09-30T15:01:08Z, read by the command from the lane's ledger snapshot.
CLOSEOUT_LINE_RE = re.compile(
    r"^LANE-CLOSEOUT rc=(\d+) tree=(\S+) cell=(\S*)(?: note=(.*))?$"
)


CACHE_CLOSEOUT_LINE_RE = re.compile(
    r"^LANE-CACHE-CLOSEOUT (removed|failed) kb=(\d+) path=(.+)$"
)
CACHE_SCRATCH_ROOTS_ENV = "ONEX_LANE_CACHE_SCRATCH_ROOTS"


def lane_cache_sweep_script(lane: str, registry_root: str) -> str:
    """Remote shell (POSIX sh) that removes the lane's own pre-commit and uv caches and prints one
    ``LANE-CACHE-CLOSEOUT removed|failed kb=<n> path=<p>`` line per directory it handled.

    Cache roots: ``$HOME/.cache`` and ``$XDG_CACHE_HOME`` (lane-named directories directly under them). Scratch
    roots, whose ``<lane>`` directory may hold a ``uv-cache`` or ``pre-commit`` directory: the registry's
    ``.claude_scratch``, ``$HOME/lab-run/scratch``, ``/mnt/*/omninode/scratch`` and the colon-separated roots
    in the host's ``ONEX_LANE_CACHE_SCRATCH_ROOTS``. The scratch directory itself (reports, results) stays."""
    return "\n".join(
        [
            f"rlc_lane={shlex.quote(lane)}; rlc_oh={shlex.quote(registry_root)}",
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
            f'  rlc_ifs=$IFS; IFS=:; for rlc_root in ${{{CACHE_SCRATCH_ROOTS_ENV}:-}}; do IFS=$rlc_ifs; [ -n "$rlc_root" ] && rlc_scratch "$rlc_root"; IFS=:; done; IFS=$rlc_ifs',
            "}",
            "rlc_sweep",
        ]
    )


def parse_cache_closeout(stdout: str) -> list[dict[str, Any]]:
    """One ``{status, kb, path}`` per ``LANE-CACHE-CLOSEOUT`` line the host printed."""
    found: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        m = CACHE_CLOSEOUT_LINE_RE.match(line.strip())
        if m:
            found.append(
                {"status": m.group(1), "kb": int(m.group(2)), "path": m.group(3)}
            )
    return found


def closeout_script(
    run_id: str,
    lane: str,
    registry_root: str,
    ledger_name: str | None,
    *,
    tool_repo: str,
    ledger_env: str,
    retry_s: int = 6,
    keep: Sequence[str] = (),
) -> str:
    """Remote shell (POSIX sh): run ``onex-lane-closeout`` once per tree under the lane's directory.

    Prints one ``LANE-CLOSEOUT rc= tree= cell= note=`` line per tree, ``LANE-CLOSEOUT-NONE`` when the lane
    made none, or ``LANE-CLOSEOUT-UNAVAILABLE`` (trees left in place) when the host has no clone of ``tool_repo``
    with the command beside its registry. The clone is looked for where the tools resolve it: the
    host's ``<TOOL_REPO>_HOME``, else beside ``OMNI_HOME``, else beside its resolved target (a
    symlinked ``OMNI_HOME`` puts the clone beside the target). A tree whose process is still ending (the
    engine's watchdog follows the dropped ssh session within seconds) is retried. A tree with uncommitted
    changes, unpushed commits or a branch with no upstream never reaches the command (OMN-20281): its
    commits are pushed to a WIP branch first, and when that fails or there are uncommitted changes the
    line's cell is ``worktree=kept:<tree>:<reason>`` and the tree stays. A tree in ``keep`` (its salvage
    failed, OMN-22229) is never offered to the command: its cell is ``worktree=kept:<tree>:salvage-failed``."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", tool_repo):
        raise ValueError(
            f"closeout_script: tool_repo {tool_repo!r}: a plain repository directory name"
        )
    tool_home_env = f"{tool_repo.upper()}_HOME"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", ledger_env):
        raise ValueError(
            f"closeout_script: ledger_env {ledger_env!r}: an upper-case variable name"
        )
    ledger_assignment = f'{ledger_env}="$C/ledger/{ledger_name}"' if ledger_name else ""
    q_lane = shlex.quote(lane)
    q_tag = shlex.quote(wip_tag(lane, run_id))
    return "\n".join(
        [
            f'LR="$HOME/lab-run"; C="$LR/runs/{run_id}.ctx"; OH={shlex.quote(registry_root)}; D="$OH/omni_worktrees/"{q_lane}',
            workdir_guard_function(),
            lane_cache_sweep_script(lane, registry_root),
            'if [ ! -d "$D" ]; then echo "LANE-CLOSEOUT-NONE no $D"; exit 0; fi',
            LANE_TREES_SH,
            'if [ -z "$TREES" ]; then echo "LANE-CLOSEOUT-NONE no trees under $D"; exit 0; fi',
            'RH=$(cd "$OH" && pwd -P)',
            'IH=""',
            f'for d in "${{{tool_home_env}:-}}" "$(dirname "$OH")/{tool_repo}" "$(dirname "$RH")/{tool_repo}"; do',
            '  if [ -n "$d" ] && [ -x "$d/.venv/bin/onex-lane-closeout" ]; then IH="$d"; break; fi',
            "done",
            f'if [ -z "$IH" ]; then echo "LANE-CLOSEOUT-UNAVAILABLE $(hostname): no {tool_repo} with onex-lane-closeout beside $OH; trees left: $(echo $TREES)"; exit 0; fi',
            'E=$(mktemp "${TMPDIR:-/tmp}/onex-lane-closeout.XXXXXX") || exit 70',
            f"KEEP={shlex.quote(' '.join(keep))}",
            'printf "%s\\n" "$TREES" | while read -r n; do',
            '  t="$D/$n"; k=0; wipnote=""',
            '  case " $KEEP " in *" $t "*) echo "LANE-CLOSEOUT rc=0 tree=$t cell=worktree=kept:$t:salvage-failed note="; continue;; esac',
            f'  g=$(rl_guard "$t" {q_tag}-"$(printf "%s" "$n" | tr "/" "-")" "" "")',
            '  case "$g" in',
            '    keep*) echo "LANE-CLOSEOUT rc=0 tree=$t cell=worktree=kept:$t:${g#keep } note="; continue;;',
            '    wip*) wipnote="wip-pushed=${g#wip } ";;',
            "  esac",
            "  while :; do",
            (
                f'    cell=$(env -u ONEX_WORKTREES_ROOT OMNI_HOME="$OH" {tool_home_env}="$IH" {ledger_assignment} '
                f'"$IH/.venv/bin/onex-lane-closeout" --lane {q_lane} --worktree "$t" 2>"$E" </dev/null); rc=$?'
            ),
            '    if [ "$rc" = 3 ] && [ "$k" -lt 3 ]; then',
            '      case "$cell" in *:process-inside*) k=$((k+1)); sleep '
            + str(int(retry_s))
            + "; continue;; esac",
            "    fi",
            "    break",
            "  done",
            '  note="$wipnote$(tr "\\n" " " < "$E" | cut -c1-600)"',
            '  case "$cell" in *:remove-failed)',
            '    case "$g" in wip*)',
            (
                f'      queued=$(python3 "$HOME/.local/share/onex-lane-guard/lane_reaper.py" --queue-closeout "$t" '
                f'--closeout-lane {q_lane} --closeout-omni-home "$OH" --closeout-command "$IH/.venv/bin/onex-lane-closeout" '
                '--pushed-ref "${g#wip }" --failure-file "$E" --closeout-rc "$rc" 2>&1); qr=$?'
            ),
            '      note="$note retry-queue-rc=$qr $(printf "%s" "$queued" | tr "\\n" " ")"',
            "      ;; esac",
            "    ;; esac",
            '  echo "LANE-CLOSEOUT rc=$rc tree=$t cell=$cell note=$note"',
            "done",
            'rm -f "$E"',
        ]
    )


def parse_closeout(stdout: str) -> list[dict[str, Any]]:
    """One ``{rc, tree, cell, note}`` per ``LANE-CLOSEOUT`` line the host printed."""
    found: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        m = CLOSEOUT_LINE_RE.match(line.strip())
        if m:
            found.append(
                {
                    "rc": int(m.group(1)),
                    "tree": m.group(2),
                    "cell": m.group(3),
                    "note": (m.group(4) or "").strip(),
                }
            )
    return found


def closeout_unavailable(stdout: str) -> bool:
    return "LANE-CLOSEOUT-UNAVAILABLE" in stdout
