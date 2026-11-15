# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the canonical clone sync agent of a host (launchd agent or systemd user timer).

Moved from the private host installer's ``canonical-clone-sync`` units. The agent runs omniclaude's sync engine through
omnimarket's dirty-converge node from the registry's dispatch venv. Each platform gives the tick, in its own
environment, ``OMNI_HOME``, ``ONEX_LEDGER_PATH`` (no default, so a converge row reaches the writer and not an
untracked pending file) and each key of the unit's declared ``environment`` with ``{home}`` and ``{registry_root}`` filled:
that carries ``ONEX_SKILL_OVERLAY_ROOTS``, so the tick finds the lab alarm delivery overlay.

The tick runs from the registry root, because the delivery overlay names its alert-channel library and its state file
relative to it: a unit that ran from the home directory delivered no alarm. A clone that holds local work is refused
and alarmed, never reset. The systemd unit writes to the journal, so
``journalctl --user -u onex-canonical-clone-sync.service`` shows each REFUSED line with the outcome of its alarm.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

from omnimarket.nodes.node_host_installer_clone_sync_units.models.model_clone_sync_units import (
    ModelCloneSyncUnitsRequest,
    ModelCloneSyncUnitsResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]
IMPORT_PROBE = "import importlib, sys; importlib.import_module(sys.argv[1])"
LOG_STEM = "canonical-clone-sync"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def load_units() -> dict[str, dict[str, Any]]:
    declared = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    return dict(declared["units"])


def agent_environment(
    request: ModelCloneSyncUnitsRequest, unit: dict[str, Any]
) -> dict[str, str]:
    """The environment both platforms give the engine; every value is declared or derived."""
    state = request.onex_state_dir or f"{request.registry_root}/.onex_state"
    env = {
        "HOME": request.home,
        "OMNI_HOME": request.registry_root,
        "ONEX_STATE_DIR": state,
        "ONEX_LEDGER_PATH": request.ledger_path,
        "PATH": f"{request.home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    for name, template in (unit.get("environment") or {}).items():
        env[str(name)] = (
            str(template)
            .replace("{home}", request.home)
            .replace("{registry_root}", request.registry_root)
        )
    return env


def render_plist(
    request: ModelCloneSyncUnitsRequest, unit: dict[str, Any], python: Path, state: Path
) -> str:
    env = agent_environment(request, unit)
    entries = "".join(
        f"    <key>{escape(k)}</key><string>{escape(v)}</string>\n"
        for k, v in env.items()
    )
    log = escape(str(state / LOG_STEM))
    schedule = unit["schedule"]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<!-- Canonical clone sync, rendered by node_host_installer_clone_sync_units.\n"
        "     The ledger path is declared so a converge row reaches the ledger writer. -->\n"
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key><string>{escape(str(unit['label']))}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        f"    <string>{escape(str(python))}</string>\n"
        f"    <string>-m</string>\n    <string>{escape(str(unit['module']))}</string>\n"
        "  </array>\n"
        f"  <key>StartInterval</key><integer>{int(schedule['start_interval'])}</integer>\n"
        f"  <key>RunAtLoad</key>{'<true/>' if schedule['run_at_load'] else '<false/>'}\n"
        "  <key>KeepAlive</key><false/>\n"
        f"  <key>EnvironmentVariables</key>\n  <dict>\n{entries}  </dict>\n"
        f"  <key>WorkingDirectory</key><string>{escape(request.registry_root)}</string>\n"
        f"  <key>StandardOutPath</key><string>{log}.out.log</string>\n"
        f"  <key>StandardErrorPath</key><string>{log}.err.log</string>\n"
        "</dict>\n</plist>\n"
    )


def render_systemd(
    request: ModelCloneSyncUnitsRequest, unit: dict[str, Any], python: Path
) -> tuple[str, str]:
    env = agent_environment(request, unit)
    name = str(unit["unit_name"])
    service = (
        "# Canonical clone sync, rendered by node_host_installer_clone_sync_units (unit\n"
        f"# {request.unit}). Exit 3 is the engine's refusal bound, not a crash.\n"
        "[Unit]\nDescription=ONEX canonical clone sync (converges a long-refused dirty clone)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={int(unit['timeout_seconds'])}\n"
        "SuccessExitStatus=3\n"
        + "".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in env.items())
        + f"WorkingDirectory={request.registry_root}\n"
        f"ExecStart={shlex.quote(str(python))} -m {unit['module']}\n"
        "StandardOutput=journal\nStandardError=journal\n"
        f"SyslogIdentifier={name}\n"
    )
    seconds = int(unit["schedule"]["on_unit_active_seconds"])
    timer = (
        f"# {unit['schedule']['description']}\n"
        "[Unit]\nDescription=ONEX canonical clone sync tick\n"
        f"\n[Timer]\nOnBootSec=60\nOnUnitActiveSec={seconds}\nUnit={name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class HandlerCloneSyncUnits:
    """``handle(request) -> result``: install, inspect or remove the clone sync agent of one host."""

    def __init__(self, runner: Runner = run_command) -> None:
        self._command = runner

    def handle(self, request: ModelCloneSyncUnitsRequest) -> ModelCloneSyncUnitsResult:
        unit = load_units()[request.unit]
        home, root = Path(request.home), Path(request.registry_root)
        state = root / str(unit["state_directory"])
        python = root / str(unit["interpreter"])
        launchd = unit["platform"] == "launchd"
        out: list[str] = []
        rendered: dict[str, str] = {}

        def result(
            code: int = 0, reason: str = "", changed: bool = False, stderr: str = ""
        ) -> ModelCloneSyncUnitsResult:
            return ModelCloneSyncUnitsResult(
                unit=request.unit,
                mode=request.mode,
                status="refused" if reason else ("failure" if code else "success"),
                exit_code=code,
                reason=reason,
                stdout="".join(out),
                stderr=stderr or reason,
                changed=changed,
                rendered=rendered,
            )

        if launchd:
            label = str(unit["label"])
            plist = home / "Library/LaunchAgents" / f"{label}.plist"
            target = f"gui/{self._command(['id', '-u']).stdout.strip()}/{label}"
            domain = target.rsplit("/", 1)[0]
        else:
            name = str(unit["unit_name"])
            service_name, timer_name = f"{name}.service", f"{name}.timer"
            unit_dir = home / ".config/systemd/user"

        if request.mode == "status":
            if launchd:
                loaded = self._command(["launchctl", "print", target])
                out.append(
                    f"{'NOT LOADED' if loaded.returncode else 'LOADED'} {label}\n"
                )
                for line in loaded.stdout.splitlines():
                    if re.match(r"^\s+(runs|last exit code|state) ", line):
                        out.append(line + "\n")
                return result(1 if loaded.returncode else 0)
            active = self._command(["systemctl", "--user", "is-active", timer_name])
            out.append(
                f"{'LOADED' if active.returncode == 0 else 'NOT LOADED'} {timer_name}\n"
            )
            journal = self._command(
                ["journalctl", "--user", "-u", service_name, "-n", "5", "--no-pager"]
            )
            out.append(
                f"--- journalctl --user -u {service_name}\n{journal.stdout or '(absent)\n'}"
            )
            return result(1 if active.returncode else 0)

        if request.mode == "uninstall":
            if launchd:
                self._command(["launchctl", "bootout", target])
                plist.unlink(missing_ok=True)
                out.append(
                    f"removed {label} (the logs under {state} are left in place)\n"
                )
            else:
                self._command(["systemctl", "--user", "disable", "--now", timer_name])
                (unit_dir / service_name).unlink(missing_ok=True)
                (unit_dir / timer_name).unlink(missing_ok=True)
                self._command(["systemctl", "--user", "daemon-reload"])
                out.append(
                    f"removed {timer_name} and {service_name} "
                    f"(the journal and {state} are left in place)\n"
                )
            return result(changed=True)

        if request.mode == "run-now":
            started = (
                self._command(["launchctl", "kickstart", "-k", target])
                if launchd
                else self._command(["systemctl", "--user", "start", service_name])
            )
            out.append(started.stdout)
            return result(started.returncode, stderr=started.stderr)

        engine = root / str(unit["engine"])
        if not engine.is_file():
            return result(
                1,
                f"the engine {engine} is absent: the registry has no omniclaude clone",
            )
        if request.mode == "install":
            probe = self._command(
                [str(python), "-I", "-c", IMPORT_PROBE, str(unit["module"])]
            )
            if probe.returncode:
                return result(
                    1,
                    f"{unit['module']} does not import under {python}:\n{probe.stderr[-600:]}",
                )

        if launchd:
            text = render_plist(request, unit, python, state)
            rendered[f"{label}.plist"] = text
            if request.mode == "dry-run":
                out.append(text)
                return result()
            state.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="clone-sync-units-") as scratch:
                staged = Path(scratch) / f"{label}.plist"
                staged.write_text(text, encoding="utf-8")
                lint = self._command(["plutil", "-lint", str(staged)])
                if lint.returncode:
                    return result(1, lint.stdout + lint.stderr)
            self._command(["launchctl", "bootout", target])
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(text, encoding="utf-8")
            plist.chmod(0o600)
            booted = self._command(["launchctl", "bootstrap", domain, str(plist)])
            out.append(booted.stdout)
            return result(booted.returncode, changed=True, stderr=booted.stderr)

        service, timer = render_systemd(request, unit, python)
        rendered[service_name] = service
        rendered[timer_name] = timer
        if request.mode == "dry-run":
            out.append(service + timer)
            return result()
        unit_dir.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        (unit_dir / service_name).write_text(service, encoding="utf-8")
        (unit_dir / timer_name).write_text(timer, encoding="utf-8")
        for argv in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", timer_name],
        ):
            done = self._command(argv)
            if done.returncode:
                return result(done.returncode, changed=True, stderr=done.stderr)
        out.append(f"installed {timer_name}: {unit['schedule']['description']}\n")
        return result(changed=True)
