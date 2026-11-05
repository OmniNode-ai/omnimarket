# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install a copied launchd payload and its matching package tree from one source.

The unit manifest is contract data. In particular, scripts and ``src`` come from
one plugin root; a copied script must never borrow a registry clone's packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelCopiedUnitPayload,
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class HandlerCopiedUnits:
    """Stage and smoke the complete bundle before changing a loaded agent."""

    def __init__(self, run: Runner, unit: dict[str, Any]) -> None:
        self.run = run
        self.unit = unit

    def handle(self, request: ModelRegistryUnitsRequest) -> ModelRegistryUnitsResult:
        unit = self.unit
        label = str(unit["label"])
        home = Path(request.home)
        install = home / str(unit["install_directory"])
        state = Path(request.registry_root) / str(unit["state_directory"])
        plist = home / "Library/LaunchAgents" / f"{label}.plist"
        rendered = ""
        digest = ""

        def result(
            code: int = 0, out: str = "", err: str = ""
        ) -> ModelRegistryUnitsResult:
            return ModelRegistryUnitsResult(
                unit=request.unit,
                mode=request.mode,
                status="failure" if code else "success",
                exit_code=code,
                stdout=out,
                stderr=err,
                rendered=rendered,
                project=request.source_root,
                bundle_sha256=digest,
                project_is_canonical=False,
            )

        def checked(argv: list[str]) -> subprocess.CompletedProcess[str]:
            done = self.run(argv)
            if done.returncode:
                raise ValueError(f"{argv[0]} exited {done.returncode}: {done.stderr}")
            return done

        try:
            domain = f"gui/{checked(['id', '-u']).stdout.strip()}"
            target = f"{domain}/{label}"
            if request.mode == "kickstart":
                checked(["launchctl", "kickstart", target])
                return result(out=f"kickstarted {label}\n")
            if request.mode == "uninstall":
                self.run(["launchctl", "bootout", target])
                plist.unlink(missing_ok=True)
                return result(
                    out=f"removed {label}; copied payload and state retained\n"
                )
            if request.mode == "status":
                checked(["launchctl", "print", target])
                log = state / "runs.jsonl"
                rows = log.read_text(encoding="utf-8").splitlines()
                if not rows or time.time() - log.stat().st_mtime > 1800:
                    raise ValueError("runs.jsonl is empty or older than 30 minutes")
                return result(out=f"LOADED {label}\n{rows[-1]}\n")
            overlay_path = Path(__file__).resolve().parents[1] / self.relative(
                str(unit["payload_overlay"])
            )
            payload = ModelCopiedUnitPayload.model_validate(
                yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            )
            source = Path(request.source_root)
            if not request.source_root or not source.is_absolute():
                raise ValueError(
                    "source_root must name the absolute plugin root, with skills and src"
                )
            python = next(
                (
                    str(p)
                    for p in unit["interpreter_candidates"]
                    if os.access(p, os.X_OK)
                ),
                "",
            )
            if not python:
                raise ValueError("no declared executable interpreter exists")
            with tempfile.TemporaryDirectory(prefix="copied-unit-") as scratch:
                staged = Path(scratch) / "payload"
                staged.mkdir()
                for name in payload.copied_files:
                    relative = self.relative(str(name))
                    origin = source / relative
                    destination = staged / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(origin, destination)
                for name in payload.copied_trees:
                    relative = self.relative(str(name))
                    shutil.copytree(
                        source / relative,
                        staged / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
                    )
                script = self.relative(str(unit["script"]))
                # The same layout is tested here and installed below. Clean PYTHONPATH
                # in the command adapter; never point it at another checkout.
                checked([python, "-B", str(staged / script), "--help"])
                sha = hashlib.sha256()
                for path in sorted(p for p in staged.rglob("*") if p.is_file()):
                    sha.update(path.relative_to(staged).as_posix().encode() + b"\0")
                    sha.update(hashlib.sha256(path.read_bytes()).digest())
                digest = sha.hexdigest()
                schedule = unit["schedule"]
                ledger = request.ledger_path or str(
                    Path(request.registry_root) / "docs/tracking/ROLLING_WORK_LEDGER.md"
                )
                arguments = [
                    str(arg).format(state=str(state), ledger=ledger)
                    for arg in payload.arguments
                ]
                user = checked(["id", "-un"]).stdout.strip()
                rendered = plistlib.dumps(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            python,
                            "-B",
                            str(install / script),
                            *arguments,
                        ],
                        "WorkingDirectory": request.registry_root,
                        "ProcessType": "Background",
                        "StartInterval": schedule["start_interval"],
                        "RunAtLoad": schedule["run_at_load"],
                        "EnvironmentVariables": {
                            "HOME": str(home),
                            "OMNI_HOME": request.registry_root,
                            "USER": user,
                            "LOGNAME": user,
                            "ONEX_LEDGER_PATH": ledger,
                            "ONEX_STATE_DIR": str(
                                Path(request.registry_root) / ".onex_state"
                            ),
                            "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                        },
                        "StandardOutPath": str(state / f"{request.unit}.out.log"),
                        "StandardErrorPath": str(state / f"{request.unit}.err.log"),
                    },
                    sort_keys=False,
                ).decode()
                candidate = Path(scratch) / "agent.plist"
                candidate.write_text(rendered, encoding="utf-8")
                checked(["plutil", "-lint", str(candidate)])
                if request.mode == "dry-run":
                    return result(out=rendered)
                self.activate(
                    staged, install, candidate, plist, state, domain, target, checked
                )
                (state / "install.json").write_text(
                    json.dumps(
                        {"source_root": request.source_root, "bundle_sha256": digest}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return result(out=f"installed {label}; bundle_sha256={digest}\n")
        except (OSError, ValueError, shutil.Error) as exc:
            return result(1, err=f"{exc}\n")

    @staticmethod
    def relative(name: str) -> Path:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"payload path must stay relative: {name}")
        return path

    def activate(
        self,
        staged: Path,
        install: Path,
        candidate: Path,
        plist: Path,
        state: Path,
        domain: str,
        target: str,
        checked: Runner,
    ) -> None:
        """Keep the old payload and plist for rollback if bootstrap refuses."""
        install.parent.mkdir(parents=True, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".copied-unit-", dir=install.parent
        ) as scratch:
            swap = Path(scratch)
            ready = swap / "ready"
            shutil.copytree(staged, ready)
            previous = swap / "previous"
            old_plist = plist.read_bytes() if plist.exists() else None
            loaded = self.run(["launchctl", "print", target]).returncode == 0
            if loaded:
                checked(["launchctl", "bootout", target])
            replaced = False
            try:
                if install.exists():
                    install.rename(previous)
                ready.rename(install)
                replaced = True
                shutil.copy2(candidate, plist)
                checked(["launchctl", "bootstrap", domain, str(plist)])
            except (OSError, ValueError):
                if replaced:
                    shutil.rmtree(install)
                if previous.exists():
                    previous.rename(install)
                if old_plist is None:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_bytes(old_plist)
                    if loaded:
                        checked(["launchctl", "bootstrap", domain, str(plist)])
                raise
