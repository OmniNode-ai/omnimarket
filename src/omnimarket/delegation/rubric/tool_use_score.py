# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Score one recorded agentic run with the tool_use rubric and write the verdict record (OMN-20233).

The I/O boundary around ``tool_use_transcript``: it reads a crush session store
or a Claude Code stream-json file, the tool schemas the model was offered and
the workspace manifest, runs node_delegation_rubric_check_compute, and writes
one JSON verdict record (to ``--out``, usually beside the run's receipt). The
record is evidence only; it decides nothing about the run.

    python -m omnimarket.delegation.rubric.tool_use_score crush --db crush.db \
        --declared-tools tools.json --workspace-root <worktree> \
        --workspace-manifest manifest.json --out rubric_verdict.json
    python -m omnimarket.delegation.rubric.tool_use_score claude-stream \
        --stream transcript.jsonl --request-file prompt.md --declared-tools tools.json \
        --workspace-manifest manifest.json --out rubric_verdict.json

``--manifest-from-root`` lists the tree under ``--workspace-root`` now (git's
tracked and untracked files); a hook that can should instead pass the
manifest it took before the run, which is the tree the model saw.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.delegation.rubric.attempt_verdict import attempt_verdict_from
from omnimarket.delegation.rubric.contract_loader import (
    load_delegation_class_rubrics,
)
from omnimarket.delegation.rubric.tool_use_transcript import (
    TOOL_USE_CLASS,
    CrushMessage,
    claude_stream_request,
    crush_request,
    declared_tools_from_schemas,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    ModelRubricCheckRequest,
    ModelRubricExecutionResult,
    ModelWorkspaceFile,
)

RECORD_SCHEMA = "tool-use-rubric-verdict.v1"


def _json_list(path: Path) -> list[object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path} must hold a JSON list")
    return value


def manifest_from_root(root: Path) -> tuple[ModelWorkspaceFile, ...]:
    """Tracked and untracked (not ignored) files under a git worktree, with line counts."""
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    rows: list[ModelWorkspaceFile] = []
    for name in dict.fromkeys(item for item in listed.split("\0") if item):
        path = root / name
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            lines = sum(1 for _ in handle)
        rows.append(ModelWorkspaceFile(path=name, line_count=lines))
    return tuple(rows)


def _manifest(args: argparse.Namespace) -> tuple[ModelWorkspaceFile, ...] | None:
    if args.workspace_manifest:
        return tuple(
            ModelWorkspaceFile.model_validate(row)
            for row in _json_list(Path(args.workspace_manifest))
        )
    if args.manifest_from_root:
        if not args.workspace_root:
            raise ValueError("--manifest-from-root needs --workspace-root")
        return manifest_from_root(Path(args.workspace_root))
    return None


def _execution_results(
    args: argparse.Namespace,
) -> tuple[ModelRubricExecutionResult, ...]:
    if not args.execution_results:
        return ()
    return tuple(
        ModelRubricExecutionResult.model_validate(row)
        for row in _json_list(Path(args.execution_results))
    )


def read_crush_session(db: Path, session: str | None) -> tuple[str, list[CrushMessage]]:
    """The named session (default: the newest) of a crush store, opened read-only."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if session is None:
            row = con.execute(
                "select id from sessions where parent_session_id is null "
                "order by created_at desc limit 1"
            ).fetchone()
            if row is None:
                raise ValueError(f"{db} holds no session")
            session = str(row[0])
        # The model column names the engine; a store from before it existed has none.
        columns = {str(row[1]) for row in con.execute("pragma table_info(messages)")}
        model_column = "model" if "model" in columns else "null"
        messages = [
            CrushMessage(
                role=str(role),
                parts_json=str(parts),
                created_at=int(created),
                finished_at=int(finished) if finished is not None else None,
                model=str(model) if model else None,
            )
            for role, parts, created, finished, model in con.execute(
                f"select role, parts, created_at, finished_at, {model_column} "
                "from messages where session_id = ? order by created_at, rowid",
                (session,),
            )
        ]
    finally:
        con.close()
    return session, messages


def score(
    request: ModelRubricCheckRequest, source: str, run_ref: str
) -> dict[str, object]:
    """The verdict record of one scored run."""
    verdict = HandlerDelegationRubricCheck().handle(request)
    transcript = request.transcript
    assert transcript is not None
    return {
        "schema": RECORD_SCHEMA,
        "source": source,
        "run_ref": run_ref,
        "verdict": verdict.model_dump(mode="json"),
        "attempt_verdict": attempt_verdict_from(verdict).model_dump(mode="json"),
        "measured": {
            "engine": transcript.engine,
            "turns": transcript.turn_count,
            "tool_calls": len(transcript.tool_calls),
            "wall_time_ms": transcript.wall_time_ms,
        },
        "inputs": {
            "declared_tools": len(transcript.declared_tools),
            "workspace_manifest": "absent"
            if transcript.workspace_files is None
            else f"{len(transcript.workspace_files)} files",
            "execution_results": [
                row.model_dump(mode="json") for row in request.execution_results
            ],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tool_use_score",
        description="Score one agentic run with the tool_use rubric.",
    )
    sub = parser.add_subparsers(dest="source", required=True)
    for name in ("crush", "claude-stream"):
        command = sub.add_parser(name)
        command.add_argument("--declared-tools", required=True, type=Path)
        command.add_argument("--workspace-root", default=None)
        command.add_argument("--workspace-manifest", default=None)
        command.add_argument("--manifest-from-root", action="store_true")
        command.add_argument("--execution-results", default=None)
        command.add_argument("--request-file", default=None, type=Path)
        command.add_argument("--rubric-contract", default=None, type=Path)
        command.add_argument("--run-ref", default="")
        command.add_argument("--out", default=None, type=Path)
        if name == "crush":
            command.add_argument("--db", required=True, type=Path)
            command.add_argument("--session", default=None)
        else:
            command.add_argument("--stream", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rubric = load_delegation_class_rubrics(args.rubric_contract).for_class(
            TOOL_USE_CLASS
        )
        tools = declared_tools_from_schemas(
            row for row in _json_list(args.declared_tools) if isinstance(row, dict)
        )
        manifest = _manifest(args)
        executions = _execution_results(args)
        request_text = (
            args.request_file.read_text(encoding="utf-8") if args.request_file else None
        )
        if args.source == "crush":
            session, messages = read_crush_session(args.db, args.session)
            request = crush_request(
                messages,
                rubric=rubric,
                declared_tools=tools,
                workspace_root=args.workspace_root,
                workspace_files=manifest,
                request_text=request_text,
                execution_results=executions,
            )
            run_ref = args.run_ref or f"crush:{session}"
        else:
            events = [
                json.loads(line)
                for line in args.stream.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("{")
            ]
            request = claude_stream_request(
                [event for event in events if isinstance(event, dict)],
                rubric=rubric,
                request_text=request_text or "",
                declared_tools=tools,
                workspace_root=args.workspace_root,
                workspace_files=manifest,
                execution_results=executions,
            )
            run_ref = args.run_ref or f"claude-stream:{args.stream}"
        record = score(request, args.source, run_ref)
    except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"TOOL-USE-RUBRIC error={type(exc).__name__}: {exc}\n")
        return 2
    text = json.dumps(record, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    verdict = record["verdict"]
    assert isinstance(verdict, dict)
    measured = record["measured"]
    assert isinstance(measured, dict)
    sys.stderr.write(
        f"TOOL-USE-RUBRIC run={run_ref} outcome={verdict['outcome']} "
        f"failed={','.join(verdict['failed_criteria']) or '-'} "
        f"turns={measured['turns']} tool_calls={measured['tool_calls']} "
        f"wall_time_ms={measured['wall_time_ms']}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
