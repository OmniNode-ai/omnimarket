# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for the capability catalog (schema ``omni.capability-catalog/1``).

Every live-probed field carries its own tri-state (:class:`ProbeState`), never
just a boolean, plus the probe command, its exit code and when it was read
(``unknown`` never renders as up or free). Static facts (how to invoke an engine, which tool superseded which)
live beside the code in ``static_facts.yaml`` and are loaded separately by
:mod:`.render`, not carried on these dataclasses.

No dataclass here ever carries a credential's cleartext — only a ``secret_ref`` name. A
package-wide test asserts that the credential table's cleartext column name (see
``static_facts.yaml``'s config-store disambiguation entry) appears in no SQL string anywhere in
this package, which is the collectors' job to keep true; this module simply never gives
them a field to put one in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_ID = "omni.capability-catalog/1"


class ProbeState(StrEnum):
    """The tri-state every live fact is recorded in. UNKNOWN never renders as OK."""

    OK = "ok"
    DOWN = "down"
    UNKNOWN = "unknown"


class LaneStatus(StrEnum):
    """A runtime lane's placement state. FREE requires all three of the lab collector's checks to pass."""

    FREE = "free"
    BUSY = "busy"
    UNKNOWN = "unknown"
    OUT_OF_SERVICE = "out_of_service"


@dataclass(frozen=True, slots=True)
class Probe:
    """How a live field was read, so a reader can tell a stale probe from a fresh one."""

    state: ProbeState
    probe: str
    exit_code: int | None
    read_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "probe": self.probe,
            "exit_code": self.exit_code,
            "read_at": self.read_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Probe:
        return Probe(
            state=ProbeState(data["state"]),
            probe=str(data["probe"]),
            exit_code=data.get("exit_code"),
            read_at=str(data["read_at"]),
        )

    @staticmethod
    def unknown(probe: str, read_at: str, reason: str = "") -> Probe:
        del reason  # documented in the caller's own record, not this constructor
        return Probe(
            state=ProbeState.UNKNOWN, probe=probe, exit_code=None, read_at=read_at
        )


@dataclass(frozen=True, slots=True)
class Host:
    host_id: str
    display_name: str
    state: ProbeState
    probe: Probe
    out_of_service_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "display_name": self.display_name,
            "state": self.state.value,
            "probe": self.probe.to_dict(),
            "out_of_service_reason": self.out_of_service_reason,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Host:
        return Host(
            host_id=str(data["host_id"]),
            display_name=str(data["display_name"]),
            state=ProbeState(data["state"]),
            probe=Probe.from_dict(data["probe"]),
            out_of_service_reason=data.get("out_of_service_reason"),
        )


@dataclass(frozen=True, slots=True)
class Lane:
    lane_id: str
    host_id: str
    compose_project: str | None
    status: LaneStatus
    probe: Probe
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "host_id": self.host_id,
            "compose_project": self.compose_project,
            "status": self.status.value,
            "probe": self.probe.to_dict(),
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Lane:
        return Lane(
            lane_id=str(data["lane_id"]),
            host_id=str(data["host_id"]),
            compose_project=data.get("compose_project"),
            status=LaneStatus(data["status"]),
            probe=Probe.from_dict(data["probe"]),
            note=data.get("note"),
        )


@dataclass(frozen=True, slots=True)
class Engine:
    """A delegate engine or coding agent (Codex, onex delegate, GLM, Jev, crush, ...)."""

    engine_id: str
    display_name: str
    invocation: str
    credential_ref: str | None
    state: ProbeState
    probe: Probe
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "display_name": self.display_name,
            "invocation": self.invocation,
            "credential_ref": self.credential_ref,
            "state": self.state.value,
            "probe": self.probe.to_dict(),
            **({"version": self.version} if self.version is not None else {}),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Engine:
        return Engine(
            engine_id=str(data["engine_id"]),
            display_name=str(data["display_name"]),
            invocation=str(data["invocation"]),
            credential_ref=data.get("credential_ref"),
            state=ProbeState(data["state"]),
            probe=Probe.from_dict(data["probe"]),
            version=data.get("version"),
        )


@dataclass(frozen=True, slots=True)
class ModelServer:
    """A live inference server on a lab host (a Qwen/embeddings endpoint), not a
    delegate CLI engine -- see :class:`Engine` for those."""

    name: str
    host_id: str
    url: str
    state: ProbeState
    probe: Probe

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host_id": self.host_id,
            "url": self.url,
            "state": self.state.value,
            "probe": self.probe.to_dict(),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ModelServer:
        return ModelServer(
            name=str(data["name"]),
            host_id=str(data["host_id"]),
            url=str(data["url"]),
            state=ProbeState(data["state"]),
            probe=Probe.from_dict(data["probe"]),
        )


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """A name, never a value. See the module docstring and AC4."""

    secret_ref: str
    provider: str

    def to_dict(self) -> dict[str, Any]:
        return {"secret_ref": self.secret_ref, "provider": self.provider}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CredentialRef:
        return CredentialRef(
            secret_ref=str(data["secret_ref"]), provider=str(data["provider"])
        )


@dataclass(frozen=True, slots=True)
class LaunchdJob:
    label: str
    state: str  # "loaded-ok" | "loaded-failing" | "dead"
    last_exit_code: int | None
    successor: str | None
    probe: Probe

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "state": self.state,
            "last_exit_code": self.last_exit_code,
            "successor": self.successor,
            "probe": self.probe.to_dict(),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> LaunchdJob:
        return LaunchdJob(
            label=str(data["label"]),
            state=str(data["state"]),
            last_exit_code=data.get("last_exit_code"),
            successor=data.get("successor"),
            probe=Probe.from_dict(data["probe"]),
        )


@dataclass(frozen=True, slots=True)
class Plugin:
    name: str
    installed_version: str
    available_version: str | None
    probe: Probe
    update_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installed_version": self.installed_version,
            "available_version": self.available_version,
            "probe": self.probe.to_dict(),
            **({"update_command": self.update_command} if self.update_command else {}),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Plugin:
        return Plugin(
            name=str(data["name"]),
            installed_version=str(data["installed_version"]),
            available_version=data.get("available_version"),
            probe=Probe.from_dict(data["probe"]),
            update_command=data.get("update_command"),
        )


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    plugin: str
    front_door: bool
    last_used_at: str | None
    origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "plugin": self.plugin,
            "front_door": self.front_door,
            "last_used_at": self.last_used_at,
            **({"origin": self.origin} if self.origin else {}),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Skill:
        return Skill(
            name=str(data["name"]),
            plugin=str(data["plugin"]),
            front_door=bool(data.get("front_door", False)),
            last_used_at=data.get("last_used_at"),
            origin=data.get("origin"),
        )


@dataclass(frozen=True, slots=True)
class CLITool:
    name: str
    probe: Probe

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "probe": self.probe.to_dict()}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CLITool:
        return CLITool(name=str(data["name"]), probe=Probe.from_dict(data["probe"]))


@dataclass(frozen=True, slots=True)
class MCPServer:
    name: str
    probe: Probe

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "probe": self.probe.to_dict()}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> MCPServer:
        return MCPServer(name=str(data["name"]), probe=Probe.from_dict(data["probe"]))


@dataclass(frozen=True, slots=True)
class DocReference:
    title: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "path": self.path}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> DocReference:
        return DocReference(title=str(data["title"]), path=str(data["path"]))


@dataclass(frozen=True, slots=True)
class Catalog:
    schema: str
    generated_at: str
    hosts: tuple[Host, ...] = field(default_factory=tuple)
    lanes: tuple[Lane, ...] = field(default_factory=tuple)
    model_servers: tuple[ModelServer, ...] = field(default_factory=tuple)
    engines: tuple[Engine, ...] = field(default_factory=tuple)
    credential_refs: tuple[CredentialRef, ...] = field(default_factory=tuple)
    launchd_jobs: tuple[LaunchdJob, ...] = field(default_factory=tuple)
    plugins: tuple[Plugin, ...] = field(default_factory=tuple)
    skills: tuple[Skill, ...] = field(default_factory=tuple)
    clis: tuple[CLITool, ...] = field(default_factory=tuple)
    mcp_servers: tuple[MCPServer, ...] = field(default_factory=tuple)
    doc_references: tuple[DocReference, ...] = field(default_factory=tuple)
    tool_probes: Mapping[str, Probe] = field(default_factory=dict)
    delegation_runs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    ledger_usage: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "hosts": [h.to_dict() for h in self.hosts],
            "lanes": [lane.to_dict() for lane in self.lanes],
            "model_servers": [m.to_dict() for m in self.model_servers],
            "engines": [e.to_dict() for e in self.engines],
            "credential_refs": [c.to_dict() for c in self.credential_refs],
            "launchd_jobs": [j.to_dict() for j in self.launchd_jobs],
            "plugins": [p.to_dict() for p in self.plugins],
            "skills": [s.to_dict() for s in self.skills],
            "clis": [c.to_dict() for c in self.clis],
            "mcp_servers": [m.to_dict() for m in self.mcp_servers],
            "doc_references": [d.to_dict() for d in self.doc_references],
            **(
                {"tool_probes": {k: v.to_dict() for k, v in self.tool_probes.items()}}
                if self.tool_probes
                else {}
            ),
            **(
                {"delegation_runs": [dict(r) for r in self.delegation_runs]}
                if self.delegation_runs
                else {}
            ),
            **({"ledger_usage": dict(self.ledger_usage)} if self.ledger_usage else {}),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Catalog:
        return Catalog(
            schema=str(data["schema"]),
            generated_at=str(data["generated_at"]),
            hosts=tuple(Host.from_dict(h) for h in data.get("hosts", [])),
            lanes=tuple(Lane.from_dict(lane) for lane in data.get("lanes", [])),
            model_servers=tuple(
                ModelServer.from_dict(m) for m in data.get("model_servers", [])
            ),
            engines=tuple(Engine.from_dict(e) for e in data.get("engines", [])),
            credential_refs=tuple(
                CredentialRef.from_dict(c) for c in data.get("credential_refs", [])
            ),
            launchd_jobs=tuple(
                LaunchdJob.from_dict(j) for j in data.get("launchd_jobs", [])
            ),
            plugins=tuple(Plugin.from_dict(p) for p in data.get("plugins", [])),
            skills=tuple(Skill.from_dict(s) for s in data.get("skills", [])),
            clis=tuple(CLITool.from_dict(c) for c in data.get("clis", [])),
            mcp_servers=tuple(
                MCPServer.from_dict(m) for m in data.get("mcp_servers", [])
            ),
            doc_references=tuple(
                DocReference.from_dict(d) for d in data.get("doc_references", [])
            ),
            tool_probes={
                k: Probe.from_dict(v) for k, v in data.get("tool_probes", {}).items()
            },
            delegation_runs=tuple(data.get("delegation_runs", [])),
            ledger_usage=data.get("ledger_usage", {}),
        )
