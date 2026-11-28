# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""C30 producer -- a developer's first hour, install to delegation (OMN-19984).

WHAT C30 CLAIMS
    The public quickstart gets a developer from install to a Claude Code
    delegation and its model, tokens, cost and receipt on a local dashboard
    inside 60 minutes.

THE SHAPE OF THE PROOF
    Install the published plugin in a scratch profile, drive /onex:delegate
    through Claude Code's Bash tool, and find the receipt by its own correlation
    id and prompt nonce. A separately installed plugin with no skills must fail
    that same walk. Claude Code's process exit code alone proves nothing: an
    unknown slash command also exits zero. Parse stream-json tool events instead.

    The provider is a loopback model server. These clauses grade the plugin ->
    receipt path, not a provider's answer.

    The final clause reads the run back off the local dashboard. In the same
    scratch HOME it adopts the receipt's tenant with `onex local init`, starts
    the real `onex dashboard`, and reads two served exposures for the walk's
    correlation id: Runs (`delegation.decisions.v1`: model and tokens) and the
    correlation trace (`delegation.correlation-trace.v1`: cost). An exposure the
    server says it does not serve yet is a missing precondition naming its
    ticket, not a failed clause (`probe_base.SURFACE_ABSENT_ERRORS`).

WHEN IT STILL GRADES NOTHING
    Missing preconditions still prevent grading. The lab host carries Node >=22,
    npm and the claude CLI (the cloud workflow's runner setup is not ported). The subscription token
    remains operator-placed as CLAUDE_CODE_OAUTH_TOKEN; only its presence is
    checked, and the environment passes through to Claude Code untouched.
    The published marketplace is OmniNode-ai/omniclaude, named omninode-tools;
    its onex 0.3.1 plugin ships exactly one skill, delegate.

    Missing credentials report PRECONDITION-ABSENT (exit 2), distinct from a
    graded failure (exit 1). Neither outcome can publish a green C30.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers.probe_base import (
    Clause,
    ExposureRead,
    Precondition,
    ProbeResult,
    env_var_present,
    have_executable,
    local_tenant_identity,
    onex_cli,
    read_exposure,
    running_dashboard,
)
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.models.model_c30_probe import (
    DECISIONS_TOPIC,
    TRACE_TOPIC,
)

CRITERION = "C30"
#: The host's workspace binding. The scratch profile is hermetic: with these set, the walked `onex delegate` resolves to
#: the workspace's bus and is refused for want of an identity the scratch HOME does not hold.
WORKSPACE_BINDING_VARIABLES = (
    "OMNI_HOME",
    "OMNIBASE_PATH",
    "ONEX_WORKSPACE_CONFIG_ROOT",
    "ONEX_CONTRACTS_DIR",
    "ONEX_SKILL_OVERLAY_ROOTS",
)
#: The subscription token Claude Code reads; this probe checks it by presence and scrubs its value from the record.
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
BUDGET_SECONDS = 60 * 60
RECEIPT_COLUMNS = (
    "correlation_id",
    "terminal_ok",
    "quality_gate_detail",
    "model_name",
    "prompt_text",
    "tokens_input",
    "tokens_output",
)
DASHBOARD_CLAUSE = "the dashboard shows that run's model, tokens, cost and receipt"


def claude_cli() -> Precondition:
    present = have_executable("claude")
    return Precondition(
        name="claude CLI on PATH",
        present=present,
        detail=(
            "`claude` resolved on PATH"
            if present
            else "`claude` is not on PATH; the workflow installs Node and claude into RUNNER_TEMP"
        ),
        owner="OMN-19984",
    )


def installed_skills(install_path: Path) -> list[str]:
    """Read skill names from installed directories carrying SKILL.md."""
    try:
        return sorted(
            p.name
            for p in (install_path / "skills").iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()
        )
    except OSError:
        return []


@dataclass
class DelegationRun:
    ran_onex_delegate: bool = False
    correlation_id: str | None = None
    delegate_status: str | None = None


def parse_delegation_run(stream_text: str) -> DelegationRun:
    """Read Bash invocations and embedded delegation JSON from stream events."""
    parsed = DelegationRun()
    decoder = json.JSONDecoder()
    # The command the skill prescribes, and the only shape the headless
    # `--allowedTools` rule admits. A substring match would let `echo "onex
    # delegate"` count as a delegation.
    invocation = re.compile(r"\s*onex delegate(?:\s|$)")
    delegate_ids: set[str] = set()
    marker = re.compile(r'\{\s*"skill_name"\s*:\s*"node_delegate_skill_orchestrator"')
    for line in stream_text.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use":
                tool_input = block.get("input")
                command = (
                    tool_input.get("command") if isinstance(tool_input, dict) else None
                )
                if (
                    block.get("name") == "Bash"
                    and isinstance(command, str)
                    and invocation.match(command)
                ):
                    parsed.ran_onex_delegate = True
                    if isinstance(block.get("id"), str):
                        delegate_ids.add(block["id"])
            if event.get("type") != "user" or block.get("type") != "tool_result":
                continue
            # Evidence counts only from the result of the delegate invocation
            # itself, never from another tool's output that happens to look like it.
            if block.get("tool_use_id") not in delegate_ids:
                continue
            content = block.get("content")
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [
                    item["text"]
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                ]
            else:
                texts = []
            for text in texts:
                for match in marker.finditer(text):
                    try:
                        payload, _end = decoder.raw_decode(text[match.start() :])
                    except ValueError:
                        continue
                    correlation_id = payload.get("correlation_id")
                    if not isinstance(correlation_id, str) or not re.fullmatch(
                        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                        correlation_id,
                    ):
                        continue
                    parsed.correlation_id = correlation_id
                    status = payload.get("status")
                    parsed.delegate_status = (
                        status
                        if isinstance(status, str)
                        and re.fullmatch(r"[a-z_]{1,32}", status)
                        else None
                    )
    return parsed


def find_receipt(db_path: Path, correlation_id: str) -> dict[str, Any] | None:
    """Read this correlation's receipt without creating or changing a store."""
    try:
        if not db_path.is_file():
            return None
        with closing(
            sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT {', '.join(RECEIPT_COLUMNS)} FROM delegation_events WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
            return dict(row) if row is not None else None
    except (OSError, sqlite3.Error):
        return None


def _plain(value: object) -> str:
    """A receipt field as a short token; anything else is named by its type."""
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/@+-]{0,64}", value):
        return value
    return f"<{type(value).__name__}>"


@dataclass
class WalkResult:
    nonce: str = ""
    install_ok: bool = False
    skills: list[str] = field(default_factory=list)
    delegation: DelegationRun = field(default_factory=DelegationRun)
    receipt: dict[str, Any] | None = None
    execution_ok: bool = False
    claude_rc: int | None = None
    install_detail: str = "plugin did not install"
    skills_detail: str = "installed skills were not read"
    delegation_detail: str = "delegation was not run; correlation_id=None"
    receipt_detail: str = "receipt not found; correlation_id=None"
    dashboard: DashboardEvidence | None = None

    @property
    def skill_ok(self) -> bool:
        return self.install_ok and "delegate" in self.skills

    @property
    def delegation_ok(self) -> bool:
        return (
            self.execution_ok
            and self.delegation.ran_onex_delegate
            and bool(self.delegation.correlation_id)
            and self.delegation.delegate_status == "success"
        )

    @property
    def receipt_ok(self) -> bool:
        return (
            self.receipt is not None
            and self.delegation.correlation_id is not None
            and self.receipt.get("correlation_id") == self.delegation.correlation_id
            and bool(self.nonce)
            and isinstance(self.receipt.get("prompt_text"), str)
            and self.nonce in self.receipt["prompt_text"]
        )

    @property
    def ok(self) -> bool:
        return self.skill_ok and self.delegation_ok and self.receipt_ok


def _run_cmd(
    argv: list[str], timeout: float, *, env: dict[str, str], cwd: Path
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            argv,
            timeout=timeout,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"


@dataclass
class DashboardEvidence:
    """What the dashboard read proved: missing surfaces, or one graded clause."""

    preconditions: list[Precondition] = field(default_factory=list)
    clause: Clause | None = None


def _figure(value: object) -> bool:
    """A served figure is present: a number or numeric string, zero included."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value) is not None


def grade_dashboard_run(
    runs: ExposureRead, trace: ExposureRead, correlation_id: str
) -> DashboardEvidence:
    """Grade the walk's run as two served exposures show it.

    Runs must hold the walk's correlation id with a model name and both token
    figures; the trace must hold the same id with its cost. An exposure the
    server does not serve yet becomes a missing precondition, and then no
    clause is graded: half a readback is not evidence either way.
    """
    evidence = DashboardEvidence()
    for read in (runs, trace):
        absent = read.absent()
        if absent is not None:
            evidence.preconditions.append(absent)
    if evidence.preconditions:
        return evidence
    run_row = next(
        (r for r in runs.rows if r.get("correlation_id") == correlation_id), None
    )
    trace_row = next(
        (r for r in trace.rows if r.get("correlation_id") == correlation_id), None
    )
    problems = []
    if run_row is None:
        problems.append(f"Runs has no row for correlation_id={correlation_id}")
    else:
        if not isinstance(run_row.get("model_name"), str) or not run_row["model_name"]:
            problems.append("Runs row has no model_name")
        problems += [
            f"Runs row has no {f}"
            for f in ("tokens_input", "tokens_output")
            if not _figure(run_row.get(f))
        ]
    if trace_row is None:
        problems.append(f"trace has no row for correlation_id={correlation_id}")
    elif not _figure(trace_row.get("cost_usd")):
        problems.append("trace row has no cost_usd")
    detail = (
        "; ".join(problems)
        if problems or run_row is None or trace_row is None
        else (
            f"correlation_id={correlation_id} (the receipt's); model_name={_plain(run_row.get('model_name'))}; "
            f"tokens_input={_plain(run_row.get('tokens_input'))}; tokens_output={_plain(run_row.get('tokens_output'))}; "
            f"cost_usd={_plain(trace_row.get('cost_usd'))}"
        )
    )
    evidence.clause = Clause(DASHBOARD_CLAUSE, not problems, detail)
    return evidence


def _receipt_tenant(db_path: Path, correlation_id: str) -> str | None:
    """The tenant the receipt row was written under, if the store records one."""
    try:
        with closing(
            sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            row = conn.execute(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    tenant = row[0] if row else None
    return tenant if isinstance(tenant, str) and tenant else None


def read_dashboard(
    env: dict[str, str], db_path: Path, correlation_id: str
) -> DashboardEvidence:
    """Adopt the receipt's tenant, start the real `onex dashboard`, read the run back."""
    evidence = DashboardEvidence()
    identity, tenant = local_tenant_identity(
        env, adopt=_receipt_tenant(db_path, correlation_id)
    )
    evidence.preconditions.append(identity)
    if tenant is None:
        return evidence
    with running_dashboard(env) as start:
        evidence.preconditions.append(
            Precondition(
                "onex dashboard serves on loopback",
                start.base_url is not None,
                start.detail,
                "OMN-19976",
            )
        )
        if start.base_url is None:
            return evidence
        graded = grade_dashboard_run(
            read_exposure(
                start.base_url,
                DECISIONS_TOPIC,
                token=start.token,
                correlation_id=correlation_id,
            ),
            read_exposure(
                start.base_url,
                TRACE_TOPIC,
                token=start.token,
                correlation_id=correlation_id,
            ),
            correlation_id,
        )
    evidence.preconditions += graded.preconditions
    evidence.clause = graded.clause
    return evidence


def walk(
    profile_kind: str,
    marketplace_source: str,
    plugin_id: str,
    scratch: Path,
    nonce: str,
    *,
    endpoint_url: str,
    run_cmd: Callable[..., tuple[int, str, str]] = _run_cmd,
    deadline: float | None = None,
    dashboard: Callable[..., DashboardEvidence] | None = None,
) -> WalkResult:
    """Install and drive a scratch profile, recording failures without raising."""
    result = WalkResult(nonce=nonce)
    deadline = deadline if deadline is not None else time.monotonic() + BUDGET_SECONDS
    phase = "scratch setup"
    try:
        home, config, work = (scratch / name for name in ("home", "config", "work"))
        for directory in (home, config, work):
            directory.mkdir(parents=True, exist_ok=True)
        delegation_dir = home / ".omninode" / "delegation"
        delegation_dir.mkdir(parents=True, exist_ok=True)
        (delegation_dir / "bifrost_overrides.yaml").write_text(
            "backends:\n  - backend_id: local-coder\n"
            f"    endpoint_url: {endpoint_url}\n    model_name: probe-loopback\n",
            encoding="utf-8",
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in WORKSPACE_BINDING_VARIABLES
        }
        env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(config))
        # The walked `onex` is the running interpreter's own install, found first, as on the clean runner this probe
        # came from; the host's registry binding would resolve the delegation to the workspace bus (OMN-19966).
        env["PATH"] = os.pathsep.join(
            filter(None, (str(Path(sys.executable).parent), env.get("PATH")))
        )
        # The loopback model server must not be routed through a runner proxy.
        env["NO_PROXY"] = ",".join(
            filter(None, (env.get("NO_PROXY"), "127.0.0.1,localhost"))
        )

        def command(argv: list[str], limit: float = 120) -> tuple[int, str, str]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("overall budget exhausted")
            return run_cmd(argv, timeout=min(limit, remaining), env=env, cwd=work)

        for phase, argv in (
            (
                "marketplace add",
                ["claude", "plugin", "marketplace", "add", marketplace_source],
            ),
            ("plugin install", ["claude", "plugin", "install", plugin_id]),
        ):
            rc, _out, _err = command(argv)
            if rc != 0:
                result.install_detail = (
                    f"{profile_kind}: {phase} exited {rc}; control did not install"
                )
                return result
        phase = "plugin list"
        rc, out, _err = command(["claude", "plugin", "list", "--json"])
        if rc != 0:
            result.install_detail = f"{profile_kind}: plugin list exited {rc}"
            return result
        plugins = json.loads(out)
        plugin = (
            next(
                (
                    p
                    for p in plugins
                    if isinstance(p, dict)
                    and p.get("id") == plugin_id
                    and p.get("enabled") is True
                    and isinstance(p.get("installPath"), str)
                ),
                None,
            )
            if isinstance(plugins, list)
            else None
        )
        if plugin is None or not Path(plugin["installPath"]).is_dir():
            result.install_detail = (
                f"{profile_kind}: enabled installed plugin {plugin_id} not found"
            )
            return result
        result.install_ok = True
        result.install_detail = (
            f"{profile_kind}: installed {plugin_id} in scratch profile"
        )
        result.skills = installed_skills(Path(plugin["installPath"]))
        result.skills_detail = f"skills={result.skills}"
        phase = "claude delegation"
        prompt = f"Reply with the single word ok. Probe nonce {nonce}"
        rc, out, _err = command(
            [
                "claude",
                "-p",
                f"/onex:delegate {prompt}",
                "--output-format",
                "stream-json",
                "--verbose",
                "--allowedTools",
                "Bash(onex delegate:*)",
                "Skill",
                "--max-turns",
                "6",
            ],
            limit=300,
        )
        # The exit code grades nothing in either direction: an unknown slash
        # command exits 0, and hitting --max-turns after the delegation ran
        # does not undo it. It is reported, and the tool events decide.
        result.execution_ok = True
        result.claude_rc = rc
        result.delegation = parse_delegation_run(out)
        correlation_id = result.delegation.correlation_id
        result.delegation_detail = (
            f"claude exited {rc}; ran_onex_delegate={result.delegation.ran_onex_delegate}; "
            f"correlation_id={correlation_id}; delegate_status={result.delegation.delegate_status}"
        )
        phase = "receipt read"
        if correlation_id is not None:
            result.receipt = find_receipt(
                delegation_dir / "delegation.sqlite", correlation_id
            )
        result.receipt_detail = f"correlation_id={correlation_id}; receipt not found"
        if result.receipt is not None:
            result.receipt_detail = (
                f"correlation_id={correlation_id}; own nonce present={result.receipt_ok}; "
                f"terminal_ok={_plain(result.receipt.get('terminal_ok'))}; "
                f"model_name={_plain(result.receipt.get('model_name'))}; "
                f"tokens_input={_plain(result.receipt.get('tokens_input'))}; "
                f"tokens_output={_plain(result.receipt.get('tokens_output'))}"
            )
        if (
            dashboard is not None
            and correlation_id is not None
            and result.receipt is not None
        ):
            # Its own guard: a dashboard that cannot be read must not reach back
            # and fail the delegation clauses, which were already decided above.
            try:
                result.dashboard = dashboard(
                    env, delegation_dir / "delegation.sqlite", correlation_id
                )
            except Exception as exc:
                result.dashboard = DashboardEvidence(
                    preconditions=[
                        Precondition(
                            "dashboard read ran",
                            False,
                            f"dashboard read failed ({type(exc).__name__})",
                            "OMN-19984",
                        )
                    ]
                )
    except Exception as exc:
        # Neither command output nor exception messages belong in the record:
        # they may contain environment credentials. Record only the failure kind.
        detail = f"{profile_kind}: {phase} failed ({type(exc).__name__})"
        if not result.install_ok:
            result.install_detail = detail
        result.execution_ok = False
        result.delegation_detail = (
            detail + f"; correlation_id={result.delegation.correlation_id}"
        )
    return result


LOOPBACK_COMPLETION = {
    "id": "x",
    "object": "chat.completion",
    "created": 1,
    "model": "probe-loopback",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}


@contextmanager
def loopback_model_server() -> Iterator[str]:
    """Serve a loopback model server and close its socket on every outcome."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args  # quiet: the probe's own output is the record

        def reply(self, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.reply(LOOPBACK_COMPLETION)

        def do_GET(self) -> None:
            self.reply({"data": [{"id": "probe-loopback"}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield urlunsplit(("http", f"{host!s}:{port}", "/v1/chat/completions", "", ""))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _no_skill_marketplace(root: Path) -> Path:
    marketplace = root / ".claude-plugin"
    plugin = root / "plugins" / "onex" / ".claude-plugin"
    marketplace.mkdir(parents=True)
    plugin.mkdir(parents=True)
    (marketplace / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "c30-no-skill-control",
                "owner": {"name": "probe"},
                "plugins": [
                    {
                        "name": "onex",
                        "description": "C30 no-skill negative control",
                        "version": "0.0.0",
                        "source": "./plugins/onex",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (plugin / "plugin.json").write_text(
        json.dumps(
            {
                "name": "onex",
                "description": "C30 no-skill negative control",
                "version": "0.0.0",
            }
        ),
        encoding="utf-8",
    )
    return root


def _control_holds(negative: WalkResult) -> bool:
    """The no-skill plugin was really installed, really driven, and delivered nothing.

    `not negative.ok` alone is vacuous: with no skill installed the skill leg
    already fails. So the control must also show that `claude` ran to a finish
    (not a missing binary or a timeout, which would also look like nothing) and
    that no delegation, correlation id or receipt came out of it.
    """
    return (
        negative.install_ok
        and negative.skills == []
        and not negative.ok
        and negative.execution_ok
        and negative.claude_rc not in (None, 124, 127)
        and not negative.delegation.ran_onex_delegate
        and negative.delegation.correlation_id is None
        and negative.receipt is None
    )


def _dashboard_outcome(
    positive: WalkResult,
) -> tuple[list[Precondition], Clause | None]:
    """The dashboard clause, or the surfaces that kept it from being graded."""
    evidence = positive.dashboard
    if evidence is None:
        return [], Clause(
            DASHBOARD_CLAUSE,
            False,
            f"dashboard not read; receipt found={positive.receipt is not None}; "
            f"correlation_id={positive.delegation.correlation_id}",
        )
    missing = [p for p in evidence.preconditions if not p.present]
    if missing or evidence.clause is None:
        return missing, None
    return [], evidence.clause


def run(
    *,
    run_cmd: Callable[..., tuple[int, str, str]] = _run_cmd,
    dashboard: Callable[..., DashboardEvidence] | None = None,
) -> ProbeResult:
    """Grade C30. `dashboard` defaults to the real reader, looked up at call time."""
    dashboard = dashboard if dashboard is not None else read_dashboard
    result = ProbeResult(criterion=CRITERION)
    result.preconditions.append(onex_cli())
    result.preconditions.append(claude_cli())
    result.preconditions.append(env_var_present(TOKEN_ENV, owner="OMN-19984"))
    if result.missing:
        return result
    deadline = time.monotonic() + BUDGET_SECONDS
    positive, negative = WalkResult(), WalkResult()
    # Under RUNNER_TEMP when the runner provides it, so a cancelled or timed-out
    # job leaves no scratch profile in the shared TMPDIR (#1781 review, point 6).
    scratch_parent = os.environ.get("RUNNER_TEMP") or None
    try:
        with (
            tempfile.TemporaryDirectory(prefix="c30-", dir=scratch_parent) as temporary,
            loopback_model_server() as endpoint,
        ):
            scratch = Path(temporary)
            marketplace = _no_skill_marketplace(scratch / "no-skill-marketplace")
            positive = walk(
                "published",
                "OmniNode-ai/omniclaude",
                "onex@omninode-tools",
                scratch / "published",
                f"c30-{uuid.uuid4().hex}",
                endpoint_url=endpoint,
                run_cmd=run_cmd,
                deadline=deadline,
                dashboard=dashboard,
            )
            negative = walk(
                "no-skill control",
                str(marketplace),
                "onex@c30-no-skill-control",
                scratch / "negative",
                f"c30-{uuid.uuid4().hex}",
                endpoint_url=endpoint,
                run_cmd=run_cmd,
                deadline=deadline,
            )
    except Exception as exc:
        # The scratch directory or the loopback server could not be set up, so no
        # clause was graded. That is an infrastructure verdict (exit 2), not a
        # graded FAIL (#1781 review, point 5). Only the exception type is kept:
        # its message can carry environment detail.
        result.preconditions.append(
            Precondition(
                "scratch walk set up",
                False,
                f"scratch walk setup failed ({type(exc).__name__})",
                "OMN-19984",
            )
        )
        return result
    provider = "provider was a loopback model server; grades the plugin -> receipt path, not a provider's answer"
    result.clauses.extend(
        [
            Clause(
                "the published plugin installs into a scratch profile and ships the delegate skill",
                positive.skill_ok,
                f"{positive.install_detail}; {positive.skills_detail}; {provider}",
            ),
            Clause(
                "/onex:delegate run from that profile ran onex delegate and returned a correlation id",
                positive.delegation_ok,
                positive.delegation_detail,
            ),
            Clause(
                "that run's receipt is in the store under its own correlation id",
                positive.receipt_ok,
                positive.receipt_detail,
            ),
            Clause(
                "negative control: a plugin carrying no skill fails the positive leg",
                _control_holds(negative),
                (
                    f"{negative.install_detail}; {negative.skills_detail}; walk.ok={negative.ok}; "
                    f"{negative.delegation_detail}; {negative.receipt_detail}"
                    if negative.install_ok
                    else f"control did not install; {negative.install_detail}"
                ),
            ),
        ]
    )
    missing, dashboard_clause = _dashboard_outcome(positive)
    if missing and not all(c.ok for c in result.clauses):
        # A delegation clause already failed. That is a graded FAIL, and an
        # unserved dashboard surface must not turn it into exit 2 and hide it.
        dashboard_clause = Clause(
            DASHBOARD_CLAUSE,
            False,
            "not graded: "
            + "; ".join(f"{p.name}: {p.detail} (owner {p.owner})" for p in missing),
        )
        missing = []
    result.preconditions.extend(missing)
    if dashboard_clause is not None:
        result.clauses.append(dashboard_clause)
    # Every detail is built from parsed fields, but the record is published as a
    # CI artifact, so the credential's value is scrubbed from it regardless.
    token = os.environ.get(TOKEN_ENV, "")
    if token:
        result.clauses[:] = [
            Clause(c.name, c.ok, c.detail.replace(token, "<redacted>"))
            for c in result.clauses
        ]
    return result
