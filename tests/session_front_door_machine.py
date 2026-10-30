# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A member's machine and the operator's, in a temporary directory, for the front door tests.

``machine`` builds one registry root with its registry ledger, a member's local bus mirror outside it
(with its ``archive/`` directory and an executable append command on ``PATH``), the fixture team
identity declaration and the profile-tagged session_preflight overlay, plus the stand-in preflight
runner and ``onex-ledger``. ``environment`` is a member's: the mirror, the bus transport, the declared
host and the member's bound principal, with ``ONEX_REMOTE_LANE`` unset.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_session_front_door_compute import HandlerSessionFrontDoor
from omnimarket.nodes.node_session_front_door_compute.models import (
    ModelSessionFrontDoorRequest,
    ModelSessionFrontDoorResult,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "session_front_door"
IDENTITY = REPO / "tests" / "fixtures" / "team_identity" / "valid.yaml"
OVERLAY = FIXTURES / "session_preflight" / "overlay.yaml"

MEMBER_A = [
    {"source": "github_auth", "value": "fixture-member-a-gh"},
    {"source": "broker_binding", "value": "fixture-member-a-dev"},
]
OPERATOR = [
    {"source": "github_auth", "value": "fixture-operator-gh"},
    {"source": "broker_binding", "value": "fixture-lab-login"},
]

TEAM_REQUIRED = (
    "ledger_path",
    "ledger_basename",
    "ledger_is_registry_ledger",
    "ledger_grammar",
)
OPERATOR_REQUIRED = (*TEAM_REQUIRED, "coordination_host")


@dataclass
class Machine:
    root: Path
    registry_root: Path
    registry_ledger: Path
    mirror: Path
    overlay: Path
    roots: Path
    preflight_log: Path
    ledger_log: Path
    environment: dict[str, str] = field(default_factory=dict)

    def request(
        self, operation: str, claims: list[dict[str, str]], **fields: Any
    ) -> ModelSessionFrontDoorRequest:
        body: dict[str, Any] = {
            "operation": operation,
            "identity_claims": claims,
            "overlay_roots": [str(self.roots)],
            "environment": self.environment,
            "sessions_root": str(self.registry_root / ".claude_scratch" / "sessions"),
        }
        body.update(fields)
        return ModelSessionFrontDoorRequest.model_validate(body)

    def run(
        self, operation: str, claims: list[dict[str, str]], **fields: Any
    ) -> ModelSessionFrontDoorResult:
        return HandlerSessionFrontDoor().handle(
            self.request(operation, claims, **fields)
        )

    def start(
        self, claims: list[dict[str, str]], **spec: Any
    ) -> ModelSessionFrontDoorResult:
        body: dict[str, Any] = {
            "lane": "sess-front",
            "ticket": "OMN-9000001",
            "est_hours": 4,
            "displaces": "nothing queued",
            "actor": "claude:fixture:foreground",
            "model": "fixture-model",
            "scope": "fixture session",
            "preflight_script": str(FIXTURES / "fake_session_preflight.py"),
            "preflight_overlay": str(self.overlay),
            "python": sys.executable,
            "ledger_command": [
                sys.executable,
                "-S",
                str(FIXTURES / "fake_onex_ledger.py"),
            ],
        }
        body.update(spec)
        return self.run("start", claims, start=body)

    def preflight_calls(self) -> list[list[str]]:
        if not self.preflight_log.is_file():
            return []
        return [
            json.loads(line) for line in self.preflight_log.read_text().splitlines()
        ]

    def ledger_calls(self) -> list[dict[str, Any]]:
        if not self.ledger_log.is_file():
            return []
        return [json.loads(line) for line in self.ledger_log.read_text().splitlines()]

    def session_env(self, lane: str = "sess-front") -> dict[str, str]:
        path = (
            self.registry_root / ".claude_scratch" / "sessions" / lane / "session.env"
        )
        return dict(line.split("=", 1) for line in path.read_text().splitlines())

    def receipt(self, profile: str, /, **overrides: Any) -> Path:
        """A receipt in the runner's shape: every check of ``profile`` ran and passed."""
        import hashlib

        import yaml

        raw = self.overlay.read_bytes()
        checks = yaml.safe_load(raw)["checks"]
        run = [c for c in checks if c.get("profile", profile) == profile]
        failed = set(overrides.pop("failed", ()))
        dropped = set(overrides.pop("dropped", ()))
        results = [
            {
                "check_id": c["check_id"],
                "title": c["title"],
                "kind": c["kind"],
                "severity": c.get("severity", "blocker"),
                "fix": c["fix"],
                "ok": c["check_id"] not in failed,
                "detail": "",
            }
            for c in run
            if c["check_id"] not in dropped
        ]
        blocked = any(not r["ok"] and r["severity"] == "blocker" for r in results)
        payload: dict[str, Any] = {
            "intent": "tick",
            "verdict": "BLOCKED" if blocked else "OK",
            "passed": sum(1 for r in results if r["ok"]),
            "total": len(results),
            "checks": results,
            "profile": profile,
            "overlay_path": str(self.overlay),
            "overlay_sha256": hashlib.sha256(raw).hexdigest(),
            "included_check_ids": [r["check_id"] for r in results],
            "skipped_check_ids": [c["check_id"] for c in checks if c not in run],
            "passed_check_ids": [r["check_id"] for r in results if r["ok"]],
            "failed_check_ids": [r["check_id"] for r in results if not r["ok"]],
        }
        payload.update(overrides)
        if payload.get("profile") is None:
            payload.pop("profile")
        path = (
            self.root
            / f"receipt-{profile}-{len(list(self.root.glob('receipt-*')))}.json"
        )
        path.write_text(json.dumps(payload))
        return path


def build_machine(tmp_path: Path) -> Machine:
    registry_root = tmp_path / "home"
    registry = registry_root / "docs" / "tracking" / "ROLLING_WORK_LEDGER.md"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        "2026-10-29T08:00:00Z | STATUS | lane=merge-drain-fixture | drain tick |\n"
    )
    mirror = tmp_path / "bus-mirror" / "ledger" / "ROLLING_WORK_LEDGER.md"
    (mirror.parent / "archive").mkdir(parents=True)
    mirror.write_text("2026-10-29T08:00:00Z | STATUS | lane=member-only | mirror |\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    append = bin_dir / "fixture-bus-append"
    append.write_text("#!/bin/sh\nexit 0\n")
    append.chmod(0o755)
    overlay = tmp_path / "overlays" / "session_preflight" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    shutil.copyfile(OVERLAY, overlay)
    roots = tmp_path / "roots"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY, roots / "team_identity" / "overlay.yaml")
    m = Machine(
        root=tmp_path,
        registry_root=registry_root,
        registry_ledger=registry,
        mirror=mirror,
        overlay=overlay,
        roots=roots,
        preflight_log=tmp_path / "preflight.log",
        ledger_log=tmp_path / "ledger.log",
    )
    m.environment = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "OMNI_HOME": str(registry_root),
        "ONEX_LEDGER_PATH": str(mirror),
        "ONEX_LEDGER_WRITE_VIA": "bus",
        "ONEX_LEDGER_HOST_NAME": "fixture-member-a-mac",
        "ONEX_LEDGER_BUS_APPEND_COMMAND": f"{append.name} --principal fixture-member-a-dev",
        "ONEX_WORK_LEDGER_PRINCIPAL": "fixture-member-a-dev",
        "FAKE_PREFLIGHT_LOG": str(m.preflight_log),
        "FAKE_LEDGER_LOG": str(m.ledger_log),
    }
    return m


def operator_environment(m: Machine) -> dict[str, str]:
    """The operator's machine: the registry ledger itself, the overnight drive, no bus writer."""
    env = {
        k: v
        for k, v in m.environment.items()
        if not k.startswith("ONEX_LEDGER_") and k != "ONEX_WORK_LEDGER_PRINCIPAL"
    }
    env["ONEX_LEDGER_PATH"] = str(m.registry_ledger)
    env["ONEX_WORK_LEDGER_PRINCIPAL"] = "fixture-lab-login"
    env["FIXTURE_OVERNIGHT_DRIVE"] = "1"
    return env
