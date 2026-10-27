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
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

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
    return 'rl_guard() {\n  t="$1"; tag="$2"; base="$3"; url="$4"\n  s=$(git -C "$t" status --porcelain 2>/dev/null) || { echo "keep status-unreadable"; return; }\n  if [ -n "$s" ]; then echo "keep uncommitted-changes"; return; fi\n  br=$(git -C "$t" symbolic-ref -q --short HEAD 2>/dev/null)\n  if [ -n "$br" ]; then\n    if git -C "$t" rev-parse -q --verify \'@{u}\' >/dev/null 2>&1; then\n      n=$(git -C "$t" rev-list --count \'@{u}..HEAD\' 2>/dev/null) || { echo "keep upstream-unreadable"; return; }\n      if [ "$n" = 0 ]; then echo ok; return; fi\n      why=unpushed-commits\n    else why=no-upstream; fi\n  elif [ -n "$base" ]; then\n    n=$(git -C "$t" rev-list --count HEAD "^$base" 2>/dev/null) || { echo "keep base-unreadable"; return; }\n    if [ "$n" = 0 ]; then echo ok; return; fi\n    why=unpushed-commits\n  else\n    n=$(git -C "$t" rev-list --count HEAD --not --remotes 2>/dev/null) || { echo "keep remotes-unreadable"; return; }\n    if [ "$n" = 0 ]; then echo ok; return; fi\n    why=unpushed-commits\n  fi\n  dest="${url:-origin}"\n  if GIT_TERMINAL_PROMPT=0 git -C "$t" push -q "$dest" "HEAD:refs/heads/wip/$tag" >/dev/null 2>&1; then\n    echo "wip wip/$tag"\n  else\n    echo "keep $why-push-failed"\n  fi\n}'


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
