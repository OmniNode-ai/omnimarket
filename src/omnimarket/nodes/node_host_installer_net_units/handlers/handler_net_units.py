# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the declared root-owned files of a Linux pool host, byte for byte, and take them away again.

The units are data: an overlay directory holds ``units.yaml`` and the payload files. A unit names the hosts it may
install on, each file's destination, mode and sha256, and its kind. This handler never edits a payload: the bytes it
writes are the payload's bytes (with the host interface filled in where the unit says ``render: interface``), and a
rendered byte string whose sha256 differs from the declared pin is refused before anything is written.

``systemd-system-service``: script and unit files, ``systemctl daemon-reload`` when a unit file changed, then enable and
restart. ``sshd-dropin``: the file is placed, ``sshd -t`` validates the whole configuration, and a failed check puts the
previous bytes (or absence) back before anything is reloaded; the reload is ``systemctl reload``, never a restart, so
the sessions already open stay. A request whose installed files already match writes and reloads nothing.

``render: values`` fills every ``__NAME__`` placeholder in a payload from the entry's ``values`` map; a placeholder with
no value, or a value with no placeholder, is refused. The ``__GPU_MEMORY_UTILIZATION__`` placeholder is computed from the
unit's ``gpu_budget`` measurements. A unit that declares ``serving: true`` must have a ``Restart=`` line in its systemd
unit file whose value is in the contract's ``restart_policies``; this check runs before the sha256 pin check.
A file entry may carry its payload inline via ``content`` instead of a ``source`` file name; the bytes are the UTF-8
encoding of that string and the same rendering, pin and serving checks apply.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import os
import platform
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.models.model_net_units_overlay import (
    ModelNetUnitsOverlay,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelGpuBudget,
    ModelNetUnitsRequest,
    ModelNetUnitsResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]
INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    document = yaml.safe_load(
        (contract_path or NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return dict(document)


class RefusalError(Exception):
    """A request this node declines, with the typed reason the result carries."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class FilePlan:
    destination: str
    path: Path
    mode: int
    data: bytes = b""
    installed: bytes | None = None
    installed_mode: int | None = None
    pinned: bool = False
    rendered: bool = False
    is_unit_file: bool = False

    @property
    def state(self) -> str:
        if self.installed is None:
            return "absent"
        if self.installed != self.data:
            return "differs"
        if self.installed_mode != self.mode:
            return "mode"
        return "identical"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class UnitPlan:
    name: str
    kind: str
    service: str
    files: list[FilePlan] = field(default_factory=list)
    interface: str = ""
    rendered: str = ""
    serving: bool = False
    gpu_budget: dict[str, Any] | None = None
    gpu_fraction: str = ""


class HandlerNetUnits:
    """``handle(request) -> result``. ``run`` runs one command; tests inject it, the host side and the platform."""

    def __init__(
        self,
        run: Runner = run_command,
        system: Callable[[], str] = platform.system,
        sudo: list[str] | None = None,
        fs_root: Path = Path("/"),
        contract: dict[str, Any] | None = None,
    ) -> None:
        self.run = run
        self.system = system
        self.sudo = (
            sudo if sudo is not None else ([] if os.geteuid() == 0 else ["sudo", "-n"])
        )
        self.fs_root = fs_root
        self.contract = contract if contract is not None else load_contract()

    # ------------------------------------------------------------------ entry

    def handle(self, request: ModelNetUnitsRequest) -> ModelNetUnitsResult:
        try:
            if self.system() != "Linux":
                raise RefusalError(
                    "platform-not-linux",
                    f"this node acts on Linux hosts only; this host is {self.system()}",
                )
            unit = self.declared_unit(request)
            plan = self.plan(request, unit)
            if request.mode == "status":
                return self.status(request, plan)
            if request.mode == "dry-run":
                return self.dry_run(request, plan)
            if request.mode == "uninstall":
                return self.uninstall(request, plan)
            return self.install(request, plan)
        except RefusalError as refusal:
            return self.result(
                request,
                2,
                status="refused",
                reason=refusal.reason,
                error=f"{refusal}\n",
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return self.result(request, 1, error=f"{exc}\n")

    # ------------------------------------------------------------------ declaration

    def declared_unit(self, request: ModelNetUnitsRequest) -> dict[str, Any]:
        overlay = Path(request.overlay_root)
        name = request.units_file or str(self.contract["overlay"]["units_file"])
        if "/" in name or name in ("..", "."):
            raise RefusalError(
                "units-file-not-allowed",
                f"units_file {name!r} is not a plain file name",
            )
        units_file = overlay / name
        try:
            document = yaml.safe_load(units_file.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise RefusalError("overlay-unreadable", f"{units_file}: {exc}") from exc
        if document.get("schema") != self.contract["overlay"]["schema"]:
            raise RefusalError(
                "overlay-schema",
                f"{units_file}: schema must be {self.contract['overlay']['schema']}",
            )
        try:
            ModelNetUnitsOverlay.model_validate(document)
        except ValidationError as exc:
            paths = [
                ".".join(str(p) for p in err["loc"])
                for err in exc.errors(include_input=False)
            ]
            raise RefusalError("overlay-invalid", "; ".join(paths)) from exc
        unit = (document.get("units") or {}).get(request.unit)
        if unit is None:
            raise RefusalError(
                "unit-not-declared", f"{request.unit} is not declared in {units_file}"
            )
        if str(unit.get("platform")) != "linux":
            raise RefusalError(
                "platform-not-linux",
                f"{request.unit} is not declared for platform linux",
            )
        if request.host_name not in [str(h) for h in unit.get("hosts") or []]:
            raise RefusalError(
                "host-not-declared",
                f"{request.unit} does not name host {request.host_name!r}; it installs only on {unit.get('hosts')}",
            )
        return dict(unit)

    def plan(self, request: ModelNetUnitsRequest, unit: dict[str, Any]) -> UnitPlan:
        kind = str(unit.get("kind"))
        spec = (self.contract.get("unit_kinds") or {}).get(kind)
        if spec is None:
            raise RefusalError(
                "kind-not-supported", f"{request.unit} has kind {kind!r}"
            )
        service = str(unit.get("service") or "")
        if spec.get("needs_service") and not service:
            raise RefusalError(
                "service-not-declared", f"{request.unit} declares no service"
            )
        plan = UnitPlan(name=request.unit, kind=kind, service=service)
        plan.serving = bool(unit.get("serving"))
        plan.gpu_budget = unit.get("gpu_budget")
        for entry in unit.get("files") or []:
            plan.files.append(self.file_plan(request, entry, spec, plan))
        if not plan.files:
            raise RefusalError("no-files", f"{request.unit} declares no files")
        return plan

    def file_plan(
        self,
        request: ModelNetUnitsRequest,
        entry: dict[str, Any],
        spec: dict[str, Any],
        plan: UnitPlan,
    ) -> FilePlan:
        destination = str(entry["destination"])
        roots = [str(r) for r in spec["destination_roots"]]
        suffix = str(spec.get("destination_suffix") or "")
        clean = os.path.normpath(destination) == destination
        if (
            not clean
            or not any(destination.startswith(r) for r in roots)
            or "/" in destination[len(max(roots, key=destination.startswith)) :]
            or (suffix and not destination.endswith(suffix))
        ):
            raise RefusalError(
                "destination-not-allowed",
                f"{destination} is not a file directly under {roots} for kind {plan.kind}",
            )
        path = self.fs_root / destination.lstrip("/")
        mode = int(str(entry["mode"]), 8)
        item = FilePlan(destination=destination, path=path, mode=mode)
        item.is_unit_file = destination.startswith("/etc/systemd/system/")
        if request.mode != "uninstall":
            item.data = self.payload(request, entry, plan, item)
        try:
            item.installed = path.read_bytes()
            item.installed_mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            pass
        return item

    def payload(
        self,
        request: ModelNetUnitsRequest,
        entry: dict[str, Any],
        plan: UnitPlan,
        item: FilePlan,
    ) -> bytes:
        source = str(entry.get("source") or "")
        content = str(entry.get("content") or "")
        if source and content:
            raise RefusalError(
                "payload-source-ambiguous",
                f"{item.destination} declares both source and content",
            )
        if not source and not content:
            raise RefusalError(
                "payload-source-missing",
                f"{item.destination} declares neither source nor content",
            )
        if content:
            source = "inline content"
            data = content.encode("utf-8")
        else:
            if "/" in source or source in ("", ".", ".."):
                raise RefusalError(
                    "source-not-allowed",
                    f"payload {source!r} is not a plain file name",
                )
            try:
                data = (Path(request.overlay_root) / source).read_bytes()
            except OSError as exc:
                raise RefusalError("payload-unreadable", f"{source}: {exc}") from exc
        if entry.get("encoding"):
            if entry["encoding"] != "base64":
                raise RefusalError(
                    "encoding-not-supported",
                    f"{source} declares encoding {entry['encoding']!r}",
                )
            try:
                data = base64.b64decode(data, validate=False)
            except (binascii.Error, ValueError) as exc:
                raise RefusalError("payload-undecodable", f"{source}: {exc}") from exc
        render = entry.get("render")
        if render:
            rule = (self.contract.get("render") or {}).get(str(render))
            if rule is None:
                raise RefusalError(
                    "render-not-supported", f"{source} asks for render {render!r}"
                )
            if str(render) == "values":
                data = self.render_values(source, entry, plan, item, data, rule)
            else:
                placeholder = str(rule["placeholder"]).encode()
                if placeholder not in data:
                    raise RefusalError(
                        "template-lacks-placeholder",
                        f"{source} has no {rule['placeholder']}",
                    )
                if not plan.interface:
                    plan.interface = self.host_interface()
                data = data.replace(placeholder, plan.interface.encode())
                plan.rendered = data.decode("utf-8")
                item.rendered = True
        if plan.serving and item.is_unit_file:
            self.check_serving_restart(item.destination, data)
        pin = entry.get("sha256") or (entry.get("sha256_by_host") or {}).get(
            request.host_name
        )
        if pin:
            item.pinned = True
            if hashlib.sha256(data).hexdigest() != str(pin):
                raise RefusalError(
                    "digest-mismatch",
                    f"{item.destination}: rendered bytes hash to {hashlib.sha256(data).hexdigest()}, the declared pin is {pin}",
                )
        return data

    def render_values(
        self,
        source: str,
        entry: dict[str, Any],
        plan: UnitPlan,
        item: FilePlan,
        data: bytes,
        rule: dict[str, Any],
    ) -> bytes:
        values = entry.get("values") or {}
        if not isinstance(values, dict):
            raise RefusalError(
                "values-not-mapping", f"{source} declares values that are not a mapping"
            )
        pattern = re.compile(str(rule["placeholder_pattern"]))
        gpu_placeholder = str(rule["gpu_budget_placeholder"])
        found = set(pattern.findall(data.decode("utf-8")))
        declared = {f"__{name}__" for name in values}
        missing = found - declared - {gpu_placeholder}
        if missing:
            raise RefusalError(
                "placeholder-without-value",
                f"{source} has placeholders without values: {sorted(missing)}",
            )
        unused = declared - found
        if unused:
            raise RefusalError(
                "value-without-placeholder",
                f"{source} has values without placeholders: {sorted(unused)}",
            )
        text = data.decode("utf-8")
        if gpu_placeholder in found:
            if plan.gpu_budget is None:
                raise RefusalError(
                    "gpu-budget-not-declared",
                    f"{source} uses {gpu_placeholder} but the unit declares no gpu_budget",
                )
            fraction = self.gpu_fraction(plan.gpu_budget)
            plan.gpu_fraction = fraction
            text = text.replace(gpu_placeholder, fraction)
        for name, value in values.items():
            text = text.replace(f"__{name}__", str(value))
        remaining = pattern.findall(text)
        if remaining:
            raise RefusalError(
                "placeholder-remaining",
                f"{source} still has placeholders after rendering: {sorted(set(remaining))}",
            )
        plan.rendered = text
        item.rendered = True
        return text.encode("utf-8")

    def check_serving_restart(self, destination: str, data: bytes) -> None:
        policies = [str(p) for p in self.contract["serving_unit"]["restart_policies"]]
        lines = data.decode("utf-8").splitlines()
        restart_lines = [line for line in lines if line.startswith("Restart=")]
        if not restart_lines:
            raise RefusalError(
                "serving-restart-policy",
                f"{destination} is a serving unit file with no Restart= line; allowed policies: {policies}",
            )
        for line in restart_lines:
            value = line.split("=", 1)[1].strip()
            if value not in policies:
                raise RefusalError(
                    "serving-restart-policy",
                    f"{destination} has Restart={value} which is not in {policies}; found line: {line}",
                )

    def gpu_fraction(self, budget: dict[str, Any]) -> str:
        try:
            model = ModelGpuBudget(**budget)
        except (TypeError, ValidationError) as exc:
            raise RefusalError(
                "gpu-budget-incomplete", f"gpu_budget is incomplete or invalid: {exc}"
            ) from exc
        decimals = int(self.contract["serving_unit"]["gpu_fraction_decimals"])
        overhead = (
            model.engine_resident_gib - model.engine_fraction_measured * model.total_gib
        )
        ceiling = (
            model.total_gib - model.co_resident_gib - overhead - model.margin_gib
        ) / model.total_gib
        fraction = math.floor(ceiling * 10**decimals) / 10**decimals
        if fraction <= 0:
            raise RefusalError(
                "gpu-budget-no-room",
                f"the gpu budget leaves no room for the engine (ceiling {ceiling:.4f})",
            )
        kv_tokens = (
            model.kv_cache_tokens_measured
            - (model.engine_fraction_measured - fraction)
            * model.total_gib
            * 2**30
            / model.kv_bytes_per_token
        )
        if kv_tokens < model.max_model_len:
            raise RefusalError(
                "gpu-budget-kv-floor",
                f"kv cache holds {kv_tokens:.0f} tokens but max_model_len is {model.max_model_len}",
            )
        return f"{fraction:.{decimals}f}"

    def host_interface(self) -> str:
        """The interface that carries the lowest-metric IPv4 default route, which must carry an IPv4 address."""
        routes = self.run(["ip", "-4", "route", "show", "default"])
        found: list[tuple[int, str]] = []
        for line in routes.stdout.splitlines():
            words = line.split()
            if not words or words[0] != "default" or "dev" not in words:
                continue
            metric = int(words[words.index("metric") + 1]) if "metric" in words else 0
            found.append((metric, words[words.index("dev") + 1]))
        if not found:
            raise RefusalError(
                "no-default-route",
                "the host has no IPv4 default route to read an interface from",
            )
        name = min(found, key=lambda pair: pair[0])[1]
        if not INTERFACE_NAME.match(name):
            raise RefusalError(
                "interface-name-invalid", f"default route names interface {name!r}"
            )
        addresses = self.run(["ip", "-4", "-o", "addr", "show", "dev", name])
        if " inet " not in addresses.stdout:
            raise RefusalError(
                "interface-has-no-ipv4",
                f"the default-route interface {name} carries no IPv4 address",
            )
        return name

    # ------------------------------------------------------------------ modes

    def dry_run(
        self, request: ModelNetUnitsRequest, plan: UnitPlan
    ) -> ModelNetUnitsResult:
        lines, changed = self.describe(plan)
        wake = self.needs_start(plan)
        if wake:
            lines.append(f"would start {plan.service}")
        changed = changed or wake
        if not changed:
            lines.append(f"no change: {plan.name} already matches its declaration")
        return self.result(
            request, 0, out="\n".join(lines) + "\n", plan=plan, changed=changed
        )

    def describe(self, plan: UnitPlan) -> tuple[list[str], bool]:
        lines, changed = [], False
        for item in plan.files:
            if item.state == "identical":
                lines.append(f"unchanged {item.destination}")
            else:
                changed = True
                lines.append(
                    f"would write {item.destination} ({item.state}, sha256 {item.digest})"
                )
        return lines, changed

    def status(
        self, request: ModelNetUnitsRequest, plan: UnitPlan
    ) -> ModelNetUnitsResult:
        out, failed = "", 0
        for item in plan.files:
            label = {
                "identical": "identical",
                "absent": "absent",
                "differs": "differs",
                "mode": "mode differs",
            }[item.state]
            out += f"{label} {item.destination}\n"
            failed |= item.state != "identical"
        if plan.service:
            active = self.is_active(plan.service)
            out += f"{plan.service} {'active' if active else 'NOT RUNNING: inactive'}\n"
            failed |= not active
        else:
            check = self.run([*self.sudo, str(self.contract["sshd"]["binary"]), "-t"])
            out += (
                "sshd -t ok\n"
                if check.returncode == 0
                else f"sshd -t failed: {check.stderr.strip()}\n"
            )
            failed |= check.returncode != 0
        return self.result(request, int(bool(failed)), out=out, plan=plan)

    def install(
        self, request: ModelNetUnitsRequest, plan: UnitPlan
    ) -> ModelNetUnitsResult:
        todo = [item for item in plan.files if item.state != "identical"]
        if plan.service:
            return self.install_service(request, plan, todo)
        return self.install_dropins(request, plan, todo)

    def install_service(
        self, request: ModelNetUnitsRequest, plan: UnitPlan, todo: list[FilePlan]
    ) -> ModelNetUnitsResult:
        out = ""
        if not todo and self.is_active(plan.service):
            return self.result(
                request,
                0,
                out=f"no change: {plan.name} already matches its declaration\n",
                plan=plan,
            )
        for item in todo:
            self.write(item.path, item.data, item.mode)
            out += f"wrote {item.destination} (sha256 {item.digest})\n"
        if any(item.is_unit_file for item in todo):
            self.systemctl("daemon-reload")
        self.systemctl("enable", plan.service)
        self.systemctl("restart" if todo else "start", plan.service)
        out += f"{'restarted' if todo else 'started'} {plan.service}\n"
        if not self.is_active(plan.service):
            return self.result(
                request,
                1,
                out=out,
                error=f"{plan.service} is not active after the install\n",
                plan=plan,
                changed=True,
            )
        return self.result(
            request, 0, out=out + f"{plan.service} active\n", plan=plan, changed=True
        )

    def install_dropins(
        self, request: ModelNetUnitsRequest, plan: UnitPlan, todo: list[FilePlan]
    ) -> ModelNetUnitsResult:
        if not todo:
            return self.result(
                request,
                0,
                out=f"no change: {plan.name} already matches its declaration\n",
                plan=plan,
            )
        out = ""
        for item in todo:
            self.write(item.path, item.data, item.mode)
            out += f"wrote {item.destination} (sha256 {item.digest})\n"
        failure = self.validate_sshd(plan, todo)
        if failure:
            return self.result(request, 1, out=out, error=failure, plan=plan)
        return self.reload_sshd(request, plan, out)

    def uninstall(
        self, request: ModelNetUnitsRequest, plan: UnitPlan
    ) -> ModelNetUnitsResult:
        present = [item for item in plan.files if item.installed is not None]
        out = ""
        if plan.service:
            if present:
                self.systemctl("disable", "--now", plan.service)
            for item in present:
                self.remove(item.path)
                out += f"removed {item.destination}\n"
            if any(item.is_unit_file for item in present):
                self.systemctl("daemon-reload")
        else:
            if not present:
                return self.result(
                    request,
                    0,
                    out=f"no change: {plan.name} has no installed file\n",
                    plan=plan,
                )
            for item in present:
                self.remove(item.path)
                out += f"removed {item.destination}\n"
            failure = self.validate_sshd(plan, present)
            if failure:
                return self.result(request, 1, out=out, error=failure, plan=plan)
            return self.reload_sshd(request, plan, out)
        if not present:
            out = f"no change: {plan.name} has no installed file\n"
        return self.result(request, 0, out=out, plan=plan, changed=bool(present))

    # ------------------------------------------------------------------ sshd

    def validate_sshd(self, plan: UnitPlan, touched: list[FilePlan]) -> str:
        """Run ``sshd -t``; on a failure put every touched file back as it was and say nothing was reloaded."""
        check = self.run([*self.sudo, str(self.contract["sshd"]["binary"]), "-t"])
        if check.returncode == 0:
            return ""
        for item in touched:
            if item.installed is None:
                self.remove(item.path)
            else:
                self.write(item.path, item.installed, item.installed_mode or item.mode)
        names = ", ".join(item.destination for item in touched)
        return (
            f"sshd -t failed (exit {check.returncode}): {(check.stderr or check.stdout).strip()}\n"
            f"restored {names} to what it was; sshd was not reloaded\n"
        )

    def reload_sshd(
        self, request: ModelNetUnitsRequest, plan: UnitPlan, out: str
    ) -> ModelNetUnitsResult:
        service = next(
            (s for s in self.contract["sshd"]["services"] if self.is_active(str(s))), ""
        )
        if not service:
            return self.result(
                request,
                1,
                out=out,
                error=f"sshd -t passed but none of {self.contract['sshd']['services']} is active to reload\n",
                plan=plan,
                changed=True,
            )
        reloaded = self.systemctl("reload", str(service))
        if reloaded.returncode:
            return self.result(
                request,
                reloaded.returncode,
                out=out,
                error=reloaded.stderr,
                plan=plan,
                changed=True,
            )
        out += f"sshd -t ok; reloaded {service} (sessions already open stay)\n"
        if not self.is_active(str(service)):
            return self.result(
                request,
                1,
                out=out,
                error=f"{service} is not active after the reload\n",
                plan=plan,
                changed=True,
            )
        return self.result(request, 0, out=out, plan=plan, changed=True)

    # ------------------------------------------------------------------ host side

    def systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        done = self.run([*self.sudo, "systemctl", *args])
        if done.returncode and args[0] in (
            "daemon-reload",
            "enable",
            "start",
            "restart",
        ):
            raise OSError(
                f"systemctl {' '.join(args)} failed ({done.returncode}): {done.stderr.strip()}"
            )
        return done

    def is_active(self, service: str) -> bool:
        done = self.run(["systemctl", "is-active", service])
        return done.returncode == 0 and done.stdout.strip() == "active"

    def needs_start(self, plan: UnitPlan) -> bool:
        return (
            bool(plan.service)
            and all(i.state == "identical" for i in plan.files)
            and not self.is_active(plan.service)
        )

    def write(self, path: Path, data: bytes, mode: int) -> None:
        if self.sudo:
            with tempfile.TemporaryDirectory(prefix="net-units-") as scratch:
                staged = Path(scratch) / "payload"
                staged.write_bytes(data)
                done = self.run(
                    [
                        *self.sudo,
                        "install",
                        "-m",
                        f"{mode:04o}",
                        "-o",
                        "root",
                        "-g",
                        "root",
                        str(staged),
                        str(path),
                    ]
                )
            if done.returncode:
                raise OSError(
                    f"install {path} failed ({done.returncode}): {done.stderr.strip()}"
                )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(path.name + ".new")
        staged.write_bytes(data)
        staged.chmod(mode)
        if os.geteuid() == 0:
            os.chown(staged, 0, 0)
        os.replace(staged, path)

    def remove(self, path: Path) -> None:
        if self.sudo:
            done = self.run([*self.sudo, "rm", "-f", str(path)])
            if done.returncode:
                raise OSError(
                    f"rm {path} failed ({done.returncode}): {done.stderr.strip()}"
                )
            return
        path.unlink(missing_ok=True)

    @staticmethod
    def result(
        request: ModelNetUnitsRequest,
        code: int,
        *,
        status: str = "",
        reason: str = "",
        out: str = "",
        error: str = "",
        plan: UnitPlan | None = None,
        changed: bool = False,
    ) -> ModelNetUnitsResult:
        return ModelNetUnitsResult(
            unit=request.unit,
            mode=request.mode,
            status=status or ("failure" if code else "success"),
            exit_code=code,
            reason=reason,
            stdout=out,
            stderr=error,
            changed=changed,
            rendered=plan.rendered if plan else "",
            interface=plan.interface if plan else "",
            digests={i.destination: i.digest for i in plan.files if i.data}
            if plan
            else {},
            gpu_memory_utilization=plan.gpu_fraction if plan else "",
        )
