# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C17 collector: the guide's own install line run in a clean tool environment, then measured (OMN-26044).

``CleanInstallCustomerMachine`` is a customer machine whose ``collect_surface`` is real and whose delegation and routing
collectors are still absent (C13 and C14 keep reading PRECONDITION-ABSENT). A fresh tool environment is a temporary
directory: its own home, uv tool directory and tool bin directory, a PATH holding only ``uv`` and the system directories, and
none of the registry-root variables, so the install lands where a customer's would and nothing the host carries stands in
for it. The measuring half runs inside that environment (``clean_machine_driver``); this module only reads the guide and the
registry, runs the commands and builds the observation the pure grader reads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers import (
    clean_dashboard_driver,
    clean_machine_driver,
    clean_machine_routing_driver,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_customer_guide import (
    InstallLine,
    audit_customer_surface,
    guide_leads_with_local,
    install_lines,
    interpreter_matches,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.observe_clean_machine_session import (
    delegate_observation_from_document,
    routing_from_document,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    ModelBrokenCommandCase,
    ModelDashboardObservation,
    ModelDelegateObservation,
    ModelRoutingCollection,
    ModelSurfaceObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

GUIDE_REPO = "knowledge_base"
GUIDE_REF = "origin/main"
GUIDE_PATH = "guides/onex-plugin-quickstart.md"
REGISTRY_REPO = "omniclaude"
REGISTRY_REF = "origin/dev"
REGISTRY_PATH = "scripts/skill_monorepo_ref_patterns.json"
TOOL_PACKAGES = ("omnibase-core", "omnibase-infra", "omnimarket")
SYSTEM_PATH = ("/usr/bin", "/bin")
#: The only host variables a clean environment keeps: how to reach the package index through a proxy or a private CA.
PASSTHROUGH_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
INSTALL_TIMEOUT_S = 900
DRIVER_TIMEOUT_S = 900
SOURCE_TIMEOUT_S = 60
DELEGATE_TIMEOUT_S = 600
#: The prompt and class of the one real delegation: a class the shipped local rungs name, answered by the local model.
DELEGATE_PROMPT = "explain what a calendar app needs"
DELEGATE_TASK_TYPE = "document"


@dataclass(frozen=True)
class CommandResult:
    rc: int | None
    stdout: str
    stderr: str


class ProtocolCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
        stdin: str | None = None,
    ) -> CommandResult: ...


class SubprocessCommandHandler:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
        stdin: str | None = None,
    ) -> CommandResult:
        try:
            done = subprocess.run(
                list(argv),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=dict(env),
                cwd=cwd,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(None, "", f"{type(exc).__name__}: {exc}")
        return CommandResult(done.returncode, done.stdout, done.stderr)


class ProtocolSurfaceSources(Protocol):
    """Where the guide and the internal-only pattern registry are read from. Raises ``CollectorAbsentError`` when unreadable."""

    def guide(self) -> tuple[str, str]: ...

    def registry(self) -> Mapping[str, Any]: ...


class GitCloneSurfaceSources:
    """Reads both from the registry root's clones at their landing branch: files, never a working tree, never GitHub."""

    def __init__(self, registry_root: Path) -> None:
        self._root = registry_root

    def _show(self, name: str, repo: str, ref: str, path: str) -> str:
        try:
            done = subprocess.run(
                ["git", "-C", str(self._root / repo), "show", f"{ref}:{path}"],
                capture_output=True,
                text=True,
                timeout=SOURCE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollectorAbsentError(name, f"{type(exc).__name__}: {exc}") from exc
        if done.returncode != 0 or not done.stdout:
            raise CollectorAbsentError(
                name,
                f"git show {ref}:{path} in {repo} exited {done.returncode}: {done.stderr.strip()[-300:]}",
            )
        return done.stdout

    def guide(self) -> tuple[str, str]:
        return GUIDE_PATH, self._show("guide_source", GUIDE_REPO, GUIDE_REF, GUIDE_PATH)

    def registry(self) -> Mapping[str, Any]:
        text = self._show(
            "pattern_registry", REGISTRY_REPO, REGISTRY_REF, REGISTRY_PATH
        )
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise CollectorAbsentError("pattern_registry", f"not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CollectorAbsentError("pattern_registry", "not a JSON object")
        return parsed


def _output(result: Mapping[str, Any]) -> str:
    return f"{result.get('stdout') or ''}{result.get('stderr') or ''}"


def _rc(result: Mapping[str, Any] | None) -> int | None:
    rc = (result or {}).get("rc")
    return rc if isinstance(rc, int) else None


def observation_from_driver(
    *,
    line: InstallLine,
    install_rc: int | None,
    document: Mapping[str, Any],
    guide_name: str,
    guide_text: str,
    registry: Mapping[str, Any],
) -> ModelSurfaceObservation:
    """What the clean-machine run of C17 showed, from the driver's document and the two sources. Pure."""
    interp = document.get("interpreter") or {}
    version = (
        (interp.get("pyvenv_cfg") or {}).get("version_info")
        or interp.get("sys_version")
        or ""
    )
    commands = document.get("commands") or {}
    surface = document.get("cli_surface") or {}
    walked = surface.get("commands") or []
    findings, controls_held = audit_customer_surface(
        registry, walked, guide_name, guide_text
    )
    if surface.get("error"):
        findings = (*findings, f"floor|the CLI walk failed: {surface['error']}")
        controls_held = False
    broken = document.get("broken") or {}
    cases = tuple(
        ModelBrokenCommandCase(
            entry_point=str(case["entry_point"]),
            rc=_rc(case.get("result")) if _rc(case.get("result")) is not None else -1,
            output=_output(case.get("result") or {}),
        )
        for case in broken.get("cases") or []
    )
    return ModelSurfaceObservation(
        guides_lead_with_local=guide_leads_with_local(guide_text),
        install_rc=install_rc,
        install_interpreter_matches=interpreter_matches(line, version),
        help_answers=_rc(commands.get("version")) == 0
        and _rc(commands.get("delegate_help")) == 0,
        parameter_findings=findings,
        parameter_controls_caught=controls_held,
        operator_switch_env=broken.get("switch_env") or None,
        broken_ran_with_switch=bool(broken.get("ran_with_switch")),
        broken_before_rc=_rc(broken.get("before")),
        broken_after_rc=_rc(broken.get("after")),
        broken_cases=cases,
    )


class CleanInstallCustomerMachine:
    def __init__(
        self,
        *,
        sources: ProtocolSurfaceSources,
        runner: ProtocolCommandRunner | None = None,
        uv: str | None = None,
        host_path: str | None = None,
        host_env: Mapping[str, str] | None = None,
    ) -> None:
        self._sources = sources
        self._runner = runner or SubprocessCommandHandler()
        self._uv = uv
        self._host_path = host_path
        process_env = os.environ  # ONEX_FLAG_EXEMPT: host environment of the run
        self._host_env = process_env if host_env is None else host_env
        self._versions: dict[str, str] = {}
        self._sessions: dict[tuple[str, str, int], Mapping[str, Any]] = {}

    def run_delegation(
        self, check_id: CheckId, responder: ModelChainResolution
    ) -> ModelDelegateObservation:
        return delegate_observation_from_document(
            self._session(responder), responder_base=responder.base_url or ""
        )

    def collect_routing(
        self, responder: ModelChainResolution
    ) -> ModelRoutingCollection:
        return routing_from_document(self._session(responder))

    def _session(self, responder: ModelChainResolution) -> Mapping[str, Any]:
        """One clean install, one real delegation and one routing walk per responder, shared by C13 and C14.

        Every host, model and window handed to the machine is the responder the settings row's chain resolved; nothing is
        read from this host's own environment. An install that did not complete is a document that says so, which grades FAIL.
        """
        if not responder.base_url or not responder.served_model or not responder.window:
            raise CollectorAbsentError(
                "customer_machine",
                "the responder chain resolved no local responder with a served model and a window",
            )
        key = (responder.base_url, responder.served_model, responder.window)
        if key not in self._sessions:
            self._sessions[key] = self._measure_session(
                responder.base_url, responder.served_model, responder.window
            )
        return self._sessions[key]

    def _measure_session(
        self, base_url: str, served_model: str, window: int
    ) -> Mapping[str, Any]:
        guide_name, guide_text = self._sources.guide()
        del guide_name
        lines = install_lines(guide_text)
        usable = lines[0] if len(lines) == 1 else None
        if usable is None or usable.argv is None:
            why = (
                usable.refusal
                if usable is not None
                else f"the guide carries {len(lines)} install lines, not one"
            )
            return {
                "install_failed": f"the guide's own line is not a runnable install: {why}"
            }
        uv = self._find_uv()
        with tempfile.TemporaryDirectory(prefix="c13-clean-") as tmp:
            root = Path(tmp)
            env = self._clean_env(root, uv)
            if shutil.which("onex", path=env["PATH"]):
                raise CollectorAbsentError(
                    "clean_machine", "an onex already resolves on the clean PATH"
                )
            install = self._runner.run(
                usable.argv, env=env, cwd=root, timeout_s=INSTALL_TIMEOUT_S
            )
            if install.rc != 0:
                return {
                    "install_failed": f"the install exited {install.rc}: {install.stderr.strip()[-300:]}"
                }
            # A customer's install puts its own bin directory on PATH (uv tool update-shell); nothing else of the host does.
            session_env = {
                **env,
                "PATH": os.pathsep.join([str(root / "bin"), env["PATH"]]),
            }
            python = root / "tool" / (usable.package or "") / "bin" / "python"
            env_file = root / "session_env.json"
            env_file.write_text(json.dumps(dict(session_env)), encoding="utf-8")
            env_file.chmod(0o600)
            drove = self._runner.run(
                [
                    str(python),
                    "-I",
                    "-",
                    "--onex",
                    str(root / "bin" / "onex"),
                    "--root",
                    str(root),
                    "--env-file",
                    str(env_file),
                    "--base-url",
                    base_url,
                    "--served-model",
                    served_model,
                    "--window",
                    str(window),
                    "--prompt",
                    DELEGATE_PROMPT,
                    "--task-type",
                    DELEGATE_TASK_TYPE,
                    "--timeout",
                    str(DELEGATE_TIMEOUT_S),
                    "--packages",
                    *TOOL_PACKAGES,
                ],
                env=session_env,
                cwd=root,
                timeout_s=DRIVER_TIMEOUT_S,
                stdin=Path(clean_machine_routing_driver.__file__).read_text(
                    encoding="utf-8"
                )
                + "\nimport sys as _sys\n_sys.exit(drive(_sys.argv[1:]))\n",
            )
        try:
            document = json.loads(drove.stdout)
        except ValueError as exc:
            raise CollectorAbsentError(
                "clean_machine_driver",
                f"the in-machine driver produced no record (rc {drove.rc}): {drove.stderr.strip()[-500:]} ({exc})",
            ) from exc
        if not isinstance(document, dict):
            raise CollectorAbsentError(
                "clean_machine_driver",
                "the in-machine driver's record is not an object",
            )
        self._versions = {
            str(k): str(v) for k, v in (document.get("versions") or {}).items()
        }
        return document

    def versions(self) -> dict[str, str]:
        return dict(self._versions)

    def _find_uv(self) -> str:
        found = self._uv or shutil.which("uv", path=self._host_path)
        if not found:
            raise CollectorAbsentError(
                "uv", "uv, the guide's one prerequisite, is not on this host"
            )
        return found

    def _clean_env(self, root: Path, uv: str) -> dict[str, str]:
        bin_dir = root / "uv-bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "uv").symlink_to(uv)
        env = {k: self._host_env[k] for k in PASSTHROUGH_ENV if k in self._host_env}
        env.update(
            {
                "HOME": str(root / "home"),
                "UV_TOOL_DIR": str(root / "tool"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
                "PATH": os.pathsep.join([str(bin_dir), *SYSTEM_PATH]),
                "LANG": "C.UTF-8",
            }
        )
        (root / "home").mkdir()
        return env

    def collect_surface(self) -> ModelSurfaceObservation:
        guide_name, guide_text = self._sources.guide()
        registry = self._sources.registry()
        lines = install_lines(guide_text)
        leads = guide_leads_with_local(guide_text)
        usable = lines[0] if len(lines) == 1 else None
        if usable is None or usable.argv is None:
            # The guide's own line is what is graded: no single runnable line is an install that did not happen, never an
            # absent collector.
            why = (
                usable.refusal
                if usable is not None
                else f"the guide carries {len(lines)} install lines, not one"
            )
            return ModelSurfaceObservation(
                guides_lead_with_local=leads,
                parameter_findings=(f"floor|{why}",),
            )
        uv = self._find_uv()
        with tempfile.TemporaryDirectory(prefix="c17-clean-") as tmp:
            root = Path(tmp)
            env = self._clean_env(root, uv)
            if shutil.which("onex", path=env["PATH"]):
                raise CollectorAbsentError(
                    "clean_machine", "an onex already resolves on the clean PATH"
                )
            install = self._runner.run(
                usable.argv, env=env, cwd=root, timeout_s=INSTALL_TIMEOUT_S
            )
            if install.rc != 0:
                return ModelSurfaceObservation(
                    guides_lead_with_local=leads,
                    install_rc=install.rc,
                    parameter_findings=(
                        f"floor|the install exited {install.rc}: {install.stderr.strip()[-300:]}",
                    ),
                )
            python = root / "tool" / (usable.package or "") / "bin" / "python"
            drove = self._runner.run(
                [
                    str(python),
                    "-",
                    "--onex",
                    str(root / "bin" / "onex"),
                    "--packages",
                    *TOOL_PACKAGES,
                ],
                env=env,
                cwd=root,
                timeout_s=DRIVER_TIMEOUT_S,
                stdin=Path(clean_machine_driver.__file__).read_text(encoding="utf-8"),
            )
        try:
            document = json.loads(drove.stdout)
        except ValueError as exc:
            raise CollectorAbsentError(
                "clean_machine_driver",
                f"the in-machine driver produced no record (rc {drove.rc}): {drove.stderr.strip()[-500:]} ({exc})",
            ) from exc
        self._versions = {
            str(k): str(v) for k, v in (document.get("versions") or {}).items()
        }
        return observation_from_driver(
            line=usable,
            install_rc=install.rc,
            document=document,
            guide_name=guide_name,
            guide_text=guide_text,
            registry=registry,
        )

    def collect_dashboard(
        self, responder: ModelChainResolution
    ) -> ModelDashboardObservation:
        """C30: the customer path to the dashboard on a clean profile, measured. No token value leaves the machine."""
        if not responder.base_url or not responder.served_model:
            raise CollectorAbsentError(
                "dashboard_model", "no local responder to set the clean profile up with"
            )
        guide_name, guide_text = self._sources.guide()
        lines = install_lines(guide_text)
        usable = lines[0] if len(lines) == 1 else None
        if usable is None or usable.argv is None:
            raise CollectorAbsentError(
                "install_line",
                f"{guide_name} carries no single runnable install line for the dashboard path",
            )
        uv = self._find_uv()
        with tempfile.TemporaryDirectory(prefix="c30-clean-") as tmp:
            root = Path(tmp)
            env = self._clean_env(root, uv)
            if shutil.which("onex", path=env["PATH"]):
                raise CollectorAbsentError(
                    "clean_machine", "an onex already resolves on the clean PATH"
                )
            install = self._runner.run(
                usable.argv, env=env, cwd=root, timeout_s=INSTALL_TIMEOUT_S
            )
            if install.rc != 0:
                return ModelDashboardObservation(install_rc=install.rc)
            python = root / "tool" / (usable.package or "") / "bin" / "python"
            drove = self._runner.run(
                [
                    str(python),
                    "-",
                    "--onex",
                    str(root / "bin" / "onex"),
                    "--url",
                    responder.base_url,
                    "--model",
                    responder.served_model,
                ],
                env=env,
                cwd=root,
                timeout_s=DRIVER_TIMEOUT_S,
                stdin=Path(clean_dashboard_driver.__file__).read_text(encoding="utf-8"),
            )
        try:
            document = json.loads(drove.stdout)
        except ValueError as exc:
            raise CollectorAbsentError(
                "clean_dashboard_driver",
                f"the in-machine driver produced no record (rc {drove.rc}): {drove.stderr.strip()[-500:]} ({exc})",
            ) from exc
        return ModelDashboardObservation(install_rc=install.rc, **document)
