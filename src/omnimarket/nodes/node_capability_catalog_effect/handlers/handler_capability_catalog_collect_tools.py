# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only tool collection, hosted by the capability catalog node.

Unavailable inputs carry UNKNOWN probes. In particular, a failed launchctl read never
turns a plist into a dead job, and a credential reference never implies engine health.
Only credential names and providers are selected; MCP command lines are never retained.
"""

from __future__ import annotations

import datetime as dt
import json
import plistlib
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from ..models.model_capability_catalog import (
    SCHEMA_ID,
    Catalog,
    CLITool,
    CredentialRef,
    Engine,
    LaunchdJob,
    MCPServer,
    Plugin,
    Probe,
    ProbeState,
    Skill,
)
from ..models.model_capability_catalog_collect_tools_request import (
    ModelCapabilityCatalogCollectToolsRequest,
)
from ..models.model_capability_catalog_collect_tools_result import (
    ModelCapabilityCatalogCollectToolsResult,
)
from ..models.model_capability_catalog_sources import RemoteResult
from .handler_capability_catalog_render import load_static_facts
from .ledger_usage_rows import ledger_path, read_rows
from .scrubbed_git_env import scrubbed_git_env

Runner = Callable[[Sequence[str], float], RemoteResult]
Connect = Callable[..., sqlite3.Connection]


def default_runner(argv: Sequence[str], timeout: float) -> RemoteResult:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=scrubbed_git_env(),
        )
        return RemoteResult(
            stdout=done.stdout, exit_code=done.returncode, timed_out=False
        )
    except subprocess.TimeoutExpired:
        return RemoteResult(stdout="", exit_code=None, timed_out=True)
    except OSError:
        return RemoteResult(stdout="", exit_code=127, timed_out=False)


def _connect(uri: str, *, uri_enabled: bool) -> sqlite3.Connection:
    return sqlite3.connect(uri, uri=uri_enabled)


def _json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _moment(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def _probe(now: str, name: str, result: RemoteResult | None = None) -> Probe:
    state = ProbeState.UNKNOWN
    if result is not None and not result.timed_out and result.exit_code != 127:
        state = ProbeState.OK if result.exit_code == 0 else ProbeState.DOWN
    return Probe(
        state=state,
        probe=name,
        exit_code=result.exit_code if result else None,
        read_at=now,
    )


def _file_probe(now: str, name: str, ok: bool) -> Probe:
    return _probe(
        now, name, RemoteResult(stdout="", exit_code=0, timed_out=False) if ok else None
    )


def collect_jobs(home: Path, now: str, result: RemoteResult) -> list[LaunchdJob]:
    loaded: dict[str, int] = {}
    readable = result.exit_code == 0 and not result.timed_out
    for line in result.stdout.splitlines() if readable else []:
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith(("ai.omninode.", "com.omninode.")):
            try:
                loaded[parts[2]] = int(parts[1])
            except ValueError:
                readable = False
    disk: set[str] = set()
    directory = home / "Library/LaunchAgents"
    for prefix in ("ai.omninode.", "com.omninode."):
        for path in sorted(directory.glob(f"{prefix}*.plist")):
            try:
                data = plistlib.loads(path.read_bytes())
                disk.add(str(data.get("Label", path.stem)))
            except (OSError, ValueError, ExpatError, plistlib.InvalidFileException):
                disk.add(path.stem)
    probe = _probe(
        now, "launchctl list; LaunchAgents {ai,com}.omninode.*.plist", result
    )
    if not readable:
        probe = _probe(now, probe.probe)
    jobs = []
    for label in sorted(disk | loaded.keys()):
        code = loaded.get(label)
        state = (
            "unknown"
            if not readable
            else (
                "dead"
                if label not in loaded
                else "loaded-ok"
                if code == 0
                else "loaded-failing"
            )
        )
        jobs.append(
            LaunchdJob(
                label=label,
                state=state,
                last_exit_code=code,
                successor=None,
                probe=probe,
            )
        )
    return jobs


def _skills(
    directory: Path,
    plugin: str,
    origin: str,
    usage: Mapping[str, Any],
    front: set[str],
    since: dt.datetime,
    now: dt.datetime,
) -> list[Skill]:
    result = []
    for path in sorted((directory / "skills").glob("*/SKILL.md")):
        name = f"{plugin}:{path.parent.name}"
        entry = usage.get(name, {})
        last = None
        try:
            used = dt.datetime.fromtimestamp(
                float(entry.get("lastUsedAt", 0)) / 1000, dt.UTC
            )
            if since <= used <= now:
                last = used.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (AttributeError, ValueError, TypeError, OverflowError):
            pass
        result.append(
            Skill(
                name=name,
                plugin=plugin,
                front_door=name in front,
                last_used_at=last,
                origin=origin,
            )
        )
    return result


def collect_plugins(
    request: ModelCapabilityCatalogCollectToolsRequest,
    runner: Runner,
    facts: Mapping[str, Any],
    since: dt.datetime,
    now: dt.datetime,
    probes: dict[str, Probe],
) -> tuple[list[Plugin], list[Skill]]:
    home = request.home
    installed = _json(home / ".claude/plugins/installed_plugins.json")
    marketplaces = _json(home / ".claude/plugins/known_marketplaces.json") or {}
    usage_file = _json(home / ".claude.json")
    usage = (usage_file or {}).get("skillUsage", {})
    probes["plugins"] = _file_probe(
        request.now, "installed_plugins.json", installed is not None
    )
    probes["skill_usage"] = _file_probe(
        request.now, ".claude.json skillUsage", usage_file is not None
    )
    plugins: list[Plugin] = []
    skills: list[Skill] = []
    front = set(facts.get("front_door_skills", []))
    for key, entries in sorted((installed or {}).get("plugins", {}).items()):
        plugin, _, market = key.partition("@")
        clone_path = marketplaces.get(market, {}).get("installLocation")
        available = None
        version_probe = _probe(request.now, f"marketplace origin/main plugin {key}")
        if clone_path:
            result = runner(
                [
                    "git",
                    "-C",
                    str(clone_path),
                    "show",
                    f"origin/main:plugins/{plugin}/.claude-plugin/plugin.json",
                ],
                10,
            )
            if result.exit_code == 0:
                with suppress(ValueError, TypeError, KeyError):
                    available = str(json.loads(result.stdout)["version"])
            version_probe = _file_probe(
                request.now,
                f"marketplace origin/main plugin {key}",
                available is not None,
            )
        for entry in entries:
            installed_version = str(entry.get("version", "unknown"))
            plugins.append(
                Plugin(
                    name=key,
                    installed_version=installed_version,
                    available_version=available,
                    probe=version_probe,
                    update_command=f"claude plugin update {key}"
                    if available and available != installed_version
                    else None,
                )
            )
            if entry.get("installPath"):
                skills.extend(
                    _skills(
                        Path(entry["installPath"]),
                        plugin,
                        "cache",
                        usage,
                        front,
                        since,
                        now,
                    )
                )
    source_root = request.registry_root / "omniclaude/plugins"
    if source_root.is_dir():
        for source in sorted(source_root.iterdir()):
            skills.extend(
                _skills(source, source.name, "source", usage, front, since, now)
            )
    return plugins, skills


def collect_credentials(
    home: Path, now: str, connect: Connect, probes: dict[str, Probe]
) -> tuple[CredentialRef, ...]:
    database = home / ".omninode/delegation/delegation.sqlite"
    probe_name = "SELECT secret_ref, provider FROM local_inference_credentials"
    refs: tuple[CredentialRef, ...] = ()
    try:
        with closing(connect(database.as_uri() + "?mode=ro", uri_enabled=True)) as conn:
            rows = conn.execute(probe_name).fetchall()
        refs = tuple(
            CredentialRef(secret_ref=str(ref), provider=str(provider))
            for ref, provider in rows
        )
        probes["credentials"] = _file_probe(now, probe_name, True)
    except sqlite3.Error:
        probes["credentials"] = _probe(now, probe_name)
    return refs


def collect_receipts(
    root: Path,
    since: dt.datetime,
    now: dt.datetime,
    probes: dict[str, Probe],
    stamp: str,
) -> tuple[Mapping[str, Any], ...]:
    runs = root / ".onex_state/runs"
    collected: list[Mapping[str, Any]] = []
    unreadable = not runs.is_dir()
    for path in sorted(runs.glob("*/receipt.json")):
        data = _json(path)
        if data is None:
            unreadable = True
            continue
        receipt = data.get("receipt") or {}
        identity = receipt.get("runtime_identity") or {}
        timestamp = data.get("started_at") or identity.get("stamped_at")
        time = _moment(timestamp)
        if time is None:
            unreadable = True
            continue
        if not since <= time <= now:
            continue
        # An explicit projection, never the receipt's prompt, response or provider config.
        collected.append(
            {
                "run_id": str(data.get("run_id", path.parent.name)),
                "read_at": str(timestamp),
                "engine": data.get("backend_id"),
                "model": data.get("model"),
                "host": identity.get("host"),
                "status": data.get("status"),
                "tokens_in": data.get("tokens_in"),
                "tokens_out": data.get("tokens_out"),
            }
        )
    probes["delegation_runs"] = _file_probe(
        stamp, ".onex_state/runs/*/receipt.json (7 days)", not unreadable
    )
    return tuple(collected)


def collect_ledger_usage(
    path: Path,
    since: dt.datetime,
    now: dt.datetime,
    probes: dict[str, Probe],
    stamp: str,
) -> Mapping[str, Any]:
    counts: dict[str, dict[str, dict[str, int]]] = {"engines": {}, "hosts": {}}
    try:
        rows = read_rows(path, archives=True)
        for row in rows:
            time = _moment(row.stamp)
            if time is None or not since <= time <= now:
                continue
            values = {k: row.token(k) for k in ("tokens_in", "tokens_out")}
            if not any(v is not None for v in values.values()):
                continue
            for group, name in [
                (
                    "engines",
                    row.token("engine") or row.token("model") or row.token("actor"),
                ),
                ("hosts", row.token("host")),
            ]:
                if name:
                    total = counts[group].setdefault(
                        name, {"tokens_in": 0, "tokens_out": 0}
                    )
                    for key, value in values.items():
                        if value is not None and value.isdecimal():
                            total[key] += int(value)
        probes["ledger_usage"] = _file_probe(
            stamp, "onex-ledger reader (7 days, explicit token cells)", True
        )
        return counts
    except OSError:
        probes["ledger_usage"] = _probe(
            stamp, "onex-ledger reader (7 days, explicit token cells)"
        )
        return {}


class HandlerCapabilityCatalogCollectTools:
    def __init__(
        self,
        *,
        runner: Runner | None = None,
        static_facts: Mapping[str, Any] | None = None,
        connect: Connect = _connect,
    ) -> None:
        self.runner = runner or default_runner
        self.static_facts = static_facts
        self.connect = connect

    def handle(
        self, request: ModelCapabilityCatalogCollectToolsRequest
    ) -> ModelCapabilityCatalogCollectToolsResult:
        facts = (
            self.static_facts if self.static_facts is not None else load_static_facts()
        )
        now = _moment(request.now)
        if now is None:
            raise ValueError("tool collection needs an ISO timestamp")
        since = now - dt.timedelta(days=7)
        probes: dict[str, Probe] = {}
        launch = self.runner(["launchctl", "list"], 10)
        probes["launchd"] = _probe(request.now, "launchctl list", launch)
        jobs = collect_jobs(request.home, request.now, launch)
        plugins, skills = collect_plugins(
            request, self.runner, facts, since, now, probes
        )
        refs = collect_credentials(request.home, request.now, self.connect, probes)
        wrapper = str(request.registry_root / "omnibase_infra/scripts/onex")
        help_result = self.runner([wrapper, "--help"], 10)
        help_probe = _probe(request.now, "onex --help", help_result)
        probes["clis"] = help_probe
        clis = [CLITool(name="onex", probe=help_probe)]
        if help_result.exit_code == 0 and not help_result.timed_out:
            commands = re.findall(
                r"^\s{2,}([a-z][a-z0-9-]*)\s{2,}", help_result.stdout, re.MULTILINE
            )
            clis.extend(
                CLITool(name=f"onex {name}", probe=help_probe)
                for name in sorted(set(commands))
            )
        mcp_result = self.runner(["claude", "mcp", "list"], 30)
        probes["mcp_servers"] = _probe(
            request.now, "claude mcp list (names only)", mcp_result
        )
        servers = []
        if mcp_result.exit_code == 0 and not mcp_result.timed_out:
            for line in mcp_result.stdout.splitlines():
                match = re.match(r"^([\w.-]+):\s", line)
                if match:
                    state = (
                        ProbeState.OK
                        if "✓ Connected" in line
                        else ProbeState.DOWN
                        if "✗" in line
                        else ProbeState.UNKNOWN
                    )
                    servers.append(
                        MCPServer(
                            name=match[1],
                            probe=Probe(
                                state=state,
                                probe=f"claude mcp list: {match[1]}",
                                exit_code=0,
                                read_at=request.now,
                            ),
                        )
                    )
        versions = {
            name: self.runner([name, "--version"], 10) for name in ("codex", "crush")
        }
        engines = []
        for entry in facts.get("engines", []):
            engine_id = str(entry["engine_id"])
            result = versions.get(engine_id)
            probe = (
                help_probe
                if engine_id == "onex-delegate"
                else _probe(
                    request.now,
                    f"{engine_id} --version"
                    if result
                    else f"{engine_id}: health unprobed",
                    result,
                )
            )
            version = (
                result.stdout.strip()
                if result and result.exit_code == 0 and not result.timed_out
                else None
            )
            engines.append(
                Engine(
                    engine_id=engine_id,
                    display_name=str(entry["display_name"]),
                    invocation=str(entry["invocation"]),
                    credential_ref=entry.get("credential_ref"),
                    state=probe.state,
                    probe=probe,
                    version=version,
                )
            )
        runs = collect_receipts(request.registry_root, since, now, probes, request.now)
        usage = collect_ledger_usage(
            request.ledger_path or ledger_path(),
            since,
            now,
            probes,
            request.now,
        )
        probes["delegation_health"] = _probe(
            request.now, "delegation-health verdict unconfigured"
        )
        if request.delegation_health_path is not None:
            health = _json(request.delegation_health_path)
            probes["delegation_health"] = _file_probe(
                request.now,
                "delegation-health verdict file",
                health is not None,
            )
            health_time = _moment((health or {}).get("read_at"))
            if health_time is not None and since <= health_time <= now:
                for index, engine in enumerate(engines):
                    verdict = (
                        (health or {}).get("engines", {}).get(engine.engine_id, {})
                    )
                    state = verdict.get("state")
                    if state in {s.value for s in ProbeState}:
                        probe = Probe(
                            state=ProbeState(state),
                            probe="delegation-health verdict file",
                            exit_code=verdict.get("exit_code"),
                            read_at=health_time.isoformat(),
                        )
                        engines[index] = replace(engine, state=probe.state, probe=probe)
        return ModelCapabilityCatalogCollectToolsResult(
            correlation_id=request.correlation_id,
            catalog=Catalog(
                schema=SCHEMA_ID,
                generated_at=request.now,
                launchd_jobs=tuple(jobs),
                plugins=tuple(plugins),
                skills=tuple(skills),
                credential_refs=refs,
                clis=tuple(clis),
                mcp_servers=tuple(servers),
                engines=tuple(engines),
                tool_probes=probes,
                delegation_runs=runs,
                ledger_usage=usage,
            ),
        )
