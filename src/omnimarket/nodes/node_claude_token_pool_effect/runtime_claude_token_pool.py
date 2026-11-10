# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_claude_token_pool_effect (OMN-17427), run as ``python -m omnimarket.nodes.node_claude_token_pool_effect``.

Usage, on the lab host whose pool it is:
    python -m omnimarket.nodes.node_claude_token_pool_effect add --label <account>
        reads the pasted ``claude setup-token`` output on stdin (a terminal gets
        a hidden prompt for the token line); prints the label and fingerprint
    python -m omnimarket.nodes.node_claude_token_pool_effect resolve
        writes the chosen token to stdout for ``export X="$(...)"`` and refuses a
        terminal; the status line, with the chosen label, goes to stderr
    python -m omnimarket.nodes.node_claude_token_pool_effect mark-limited --label <account> --seconds <n>
    python -m omnimarket.nodes.node_claude_token_pool_effect mark-revoked --label <account>
    python -m omnimarket.nodes.node_claude_token_pool_effect reorder --labels <a>,<b>,...
        draws these labels first, in this order, then every other token as it stands
    python -m omnimarket.nodes.node_claude_token_pool_effect status [--json]
    python -m omnimarket.nodes.node_claude_token_pool_effect tick --host <name> [--dry-run]
        the rotation tick (OMN-23000): re-syncs the pool's marks from the overlay's one source, sets this
        host's draw order, and swaps the host's login to a usable token when the one in use is limited
        or revoked; when the overlay's spread rule is on, it also spreads a usable login that has
        been in use longer than the bound onto a token no host's lanes have used yet. Prints one
        line per result, labels only. Exit 0 on kept, swapped or spread, 4 when nothing usable is
        left, 3 when the overlay, the source or the pool cannot be read.
    python -m omnimarket.nodes.node_claude_token_pool_effect assign --host <name> --label <label> [--dry-run]
        installs the pool token named --label on this host's login and pins it first in the pool's
        draw order (what ``resolve`` reads, and the rotation tick's reset keeps) until ``release``;
        refuses one that is not in the pool, limited or revoked. Prints one line naming the outcome and any refusal, labels only.
        Exit 0 on assigned or unchanged, 2 on a refusal, 3 when the overlay or the pool cannot be
        read.
    python -m omnimarket.nodes.node_claude_token_pool_effect release
        drops the pin an assign or the spread rule set, so the tick's draw order stands again.
    python -m omnimarket.nodes.node_claude_token_pool_effect login --host <name> [--record]
        prints the pool token whose fingerprint equals this host's login (or none); with --record
        it also counts the use of that token in the pool, so the spread rule can tell a login in
        use from one no lanes have used yet.
    python -m omnimarket.nodes.node_claude_token_pool_effect schedule check|install|uninstall|status|run-now
        the tick's own timer on a host the overlay declares (install --host <name> [--dry-run]).

Exit 0 on success, 2 on a refusal, 3 when ``resolve`` finds an empty pool and 4
when every token is limited or revoked (``resolve`` chose nothing;
``mark-limited``, ``mark-revoked`` and ``status`` print the ALARM line).
``mark-revoked`` writes one ALERT line naming the label to stderr when it newly
revokes it. No output names a token's value.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from getpass import getpass
from pathlib import Path

from pydantic import SecretStr, ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    FileTokenFile,
    MirrorMarksSource,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    ClaudeTokenPoolRefusedError,
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation_tick import (
    HandlerClaudeTokenAssign,
    HandlerClaudeTokenRotation,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.schedule import (
    NODE_NAME,
    RotationScheduler,
    load_schedule,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenAssignRequest,
    ModelClaudeTokenAssignResult,
    ModelClaudeTokenRotationRequest,
    ModelClaudeTokenRotationResult,
    ModelRotationOverlay,
)

EXIT_REFUSED = 2
EXIT_EMPTY = 3
EXIT_ALL_LIMITED = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node_claude_token_pool_effect")
    verbs = parser.add_subparsers(dest="verb", required=True)
    add = verbs.add_parser(
        "add", help="store a token from `claude setup-token` output read on stdin"
    )
    add.add_argument("--label", required=True, help="the account the token belongs to")
    add.add_argument(
        "--force", action="store_true", help="replace the token stored under this label"
    )
    verbs.add_parser(
        "resolve", help="write the first unlimited token to stdout (never a terminal)"
    )
    mark = verbs.add_parser(
        "mark-limited", help="the account behind --label hit its usage limit"
    )
    mark.add_argument("--label", required=True)
    mark.add_argument("--seconds", required=True, type=float)
    revoke = verbs.add_parser(
        "mark-revoked",
        help="the OAuth token behind --label was revoked; skip it until re-added",
    )
    revoke.add_argument("--label", required=True)
    order = verbs.add_parser(
        "reorder", help="draw --labels first, in order, then every other token"
    )
    order.add_argument("--labels", required=True, help="comma-separated labels")
    verbs.add_parser(
        "release",
        help="drop the pin an assign or the spread rule set, so the tick's draw order stands",
    )
    tick = verbs.add_parser(
        "tick",
        help="re-sync marks, spread the draw order, swap a limited or revoked login",
    )
    tick.add_argument("--host", required=True, help="this host's name in the overlay")
    tick.add_argument("--dry-run", action="store_true")
    assign = verbs.add_parser(
        "assign", help="install the pool token named --label on this host's login"
    )
    assign.add_argument("--host", required=True, help="this host's name in the overlay")
    assign.add_argument(
        "--label", required=True, help="the account to put on this host"
    )
    assign.add_argument("--dry-run", action="store_true")
    login = verbs.add_parser(
        "login", help="name the pool token whose fingerprint equals this host's login"
    )
    login.add_argument("--host", required=True, help="this host's name in the overlay")
    login.add_argument(
        "--record",
        action="store_true",
        help="also count the use of that token in the pool",
    )
    status = verbs.add_parser("status", help="print the pool status line")
    status.add_argument(
        "--json", action="store_true", help="the whole result as JSON (no token)"
    )
    return parser


def _pasted() -> str:
    if sys.stdin.isatty():
        return getpass(
            "Paste the token line `claude setup-token` printed (input hidden): "
        )
    return sys.stdin.read()


def load_overlay() -> ModelRotationOverlay:
    return load_structured_overlay(NODE_NAME, ModelRotationOverlay)


def run_tick(
    host: str,
    *,
    dry_run: bool = False,
    overlay: ModelRotationOverlay | None = None,
    home: Path | None = None,
    pool: HandlerClaudeTokenPool | None = None,
    registry_root: Path | None = None,
) -> ModelClaudeTokenRotationResult:
    """One tick for ``host`` with the production ports: its login file, the source's mirror, its pool."""
    declared = overlay or load_overlay()
    row = declared.host(host)
    if row is None:
        raise ValueError(f"{host} is not a host of the {NODE_NAME} overlay")
    user_home = home or Path.home()
    # Absent is decided on the text, before it becomes a Path: Path("") is ".", the working directory.
    root = registry_root
    if root is None:
        root_text = os.environ.get("OMNI_HOME", "")
        if not root_text:
            raise ValueError(
                "OMNI_HOME is not set; the tick keeps its rotation record under it"
            )
        root = Path(root_text)
    source = declared.marks_source
    handler = HandlerClaudeTokenRotation(
        pool=pool or HandlerClaudeTokenPool(host=host),
        token_file=FileTokenFile(user_home / row.token_file),
        marks_source=MirrorMarksSource(
            home=user_home,
            mirror_path=source.mirror_path,
            ssh_target=None if source.host == host else source.ssh_target,
        ),
        overlay=declared,
        host=host,
        events_path=root / load_schedule().state_directory / declared.events_file,
    )
    return handler.handle(ModelClaudeTokenRotationRequest(host=host, dry_run=dry_run))


def run_assign(
    host: str,
    label: str,
    *,
    dry_run: bool = False,
    overlay: ModelRotationOverlay | None = None,
    home: Path | None = None,
    pool: HandlerClaudeTokenPool | None = None,
    registry_root: Path | None = None,
) -> ModelClaudeTokenAssignResult:
    """Install the pool token ``label`` on ``host``'s login: the rotation's ports and journal discipline."""
    declared = overlay or load_overlay()
    row = declared.host(host)
    if row is None:
        raise ValueError(f"{host} is not a host of the {NODE_NAME} overlay")
    user_home = home or Path.home()
    root = registry_root
    if root is None:
        root_text = os.environ.get("OMNI_HOME", "")
        if not root_text:
            raise ValueError(
                "OMNI_HOME is not set; the assign keeps its rotation record under it"
            )
        root = Path(root_text)
    handler = HandlerClaudeTokenAssign(
        pool=pool or HandlerClaudeTokenPool(host=host),
        token_file=FileTokenFile(user_home / row.token_file),
        host=host,
        events_path=root / load_schedule().state_directory / declared.events_file,
    )
    return handler.handle(
        ModelClaudeTokenAssignRequest(host=host, label=label, dry_run=dry_run)
    )


def _tick(args: argparse.Namespace) -> int:
    result = run_tick(args.host, dry_run=args.dry_run)
    sync = result.marks_sync
    sys.stdout.write(
        f"claude-pool-tick host={result.host} rank={result.rank} outcome={result.outcome} "
        f"in_use={result.in_use_state} from={result.from_label or 'none'} to={result.to_label or 'none'} "
        f"marks={sync.state}"
        + (f" changed={','.join(sync.changed)}" if sync.changed else "")
        + (f" ({sync.detail})" if sync.detail else "")
        + "\n"
    )
    if result.alarm:
        sys.stderr.write(result.alarm + "\n")
    if result.outcome == "no_usable":
        return EXIT_ALL_LIMITED
    # A tick that fell back to local marks still did its work, but its exit status says the sync was refused.
    return EXIT_EMPTY if sync.state in ("unreadable", "stale") else 0


def _assign(args: argparse.Namespace) -> int:
    result = run_assign(args.host, args.label, dry_run=args.dry_run)
    sys.stdout.write(
        f"claude-pool-assign host={result.host} label={result.label} "
        f"outcome={result.outcome} refusal={result.refusal}\n"
    )
    return EXIT_REFUSED if result.outcome == "refused" else 0


def _login(args: argparse.Namespace) -> int:
    declared = load_overlay()
    row = declared.host(args.host)
    if row is None:
        raise ValueError(f"{args.host} is not a host of the {NODE_NAME} overlay")
    login = FileTokenFile(Path.home() / row.token_file)
    fingerprint = login.fingerprint()
    label: str | None = None
    if fingerprint is not None:
        pool = HandlerClaudeTokenPool(host=args.host)
        status = pool.handle(ModelClaudeTokenPoolRequest(operation="status"))
        label = next(
            (t.label for t in status.tokens if t.fingerprint == fingerprint), None
        )
        if args.record and label is not None:
            pool.handle(
                ModelClaudeTokenPoolRequest(operation="record_use", label=label)
            )
    sys.stdout.write(f"claude-pool-login host={args.host} label={label or 'none'}\n")
    return 0


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("check")
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    scheduler = RotationScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "check":
        done = scheduler.check()
    elif args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        done = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        done = scheduler.uninstall(args.host)
    elif args.verb == "status":
        done = scheduler.status(args.host)
    else:
        done = scheduler.run_now(args.host)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    return done.exit_code


def main(
    argv: list[str] | None = None, handler: HandlerClaudeTokenPool | None = None
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "schedule":
        try:
            return _schedule(arguments[1:])
        except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
            sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
            return EXIT_EMPTY
    args = _parser().parse_args(arguments)
    if args.verb == "tick":
        try:
            return _tick(args)
        except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
            sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
            return EXIT_EMPTY
    if args.verb == "assign":
        try:
            return _assign(args)
        except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
            sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
            return EXIT_EMPTY
    if args.verb == "login":
        try:
            return _login(args)
        except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
            sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
            return EXIT_EMPTY
    if args.verb == "resolve" and sys.stdout.isatty():
        sys.stderr.write(
            'refusing to write a token to a terminal; capture it: export X="$(... resolve)"\n'
        )
        return EXIT_REFUSED
    operation = args.verb.replace("-", "_")
    try:
        request = ModelClaudeTokenPoolRequest(
            operation=operation,
            label=getattr(args, "label", None),
            setup_token_output=SecretStr(_pasted()) if args.verb == "add" else None,
            labels=tuple(filter(None, getattr(args, "labels", "").split(","))),
            seconds=getattr(args, "seconds", None),
            force=getattr(args, "force", False),
        )
        result = (handler or HandlerClaudeTokenPool()).handle(request)
    except (ClaudeTokenPoolRefusedError, ValueError) as refusal:
        sys.stderr.write(f"claude-pool refused: {refusal}\n")
        return EXIT_REFUSED
    if args.verb == "resolve":
        if result.token is None:
            sys.stderr.write(result.status_line + " chosen=none\n")
            return EXIT_EMPTY if not result.tokens else EXIT_ALL_LIMITED
        sys.stderr.write(f"{result.status_line} label={result.chosen}\n")
        sys.stdout.write(result.token.get_secret_value())
        return 0
    if args.verb == "add":
        added = next(t for t in result.tokens if t.label == args.label)
        sys.stdout.write(
            f"claude-pool added label={added.label} fingerprint={added.fingerprint}\n"
        )
    if getattr(args, "json", False):
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(result.status_line + "\n")
    if result.alert:
        sys.stderr.write(result.alert + "\n")
    return (
        EXIT_ALL_LIMITED
        if result.all_limited and args.verb in ("mark-limited", "mark-revoked")
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
