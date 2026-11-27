# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runs the customer path in a clean container: the steps declared in contract.yaml, in order, each read as passed, failed or not run.

The gateway URL comes from ``ONEX_API_BASE_URL``, the key from the owner-only file ``ONEX_API_KEY_FILE`` names and the lab
addresses that must stay unreachable from ``ONEX_CUSTOMER_PATH_LAB_PROBE_TARGETS``; there is no default for any of them. The key
reaches the container on stdin only, and every output tail has it scrubbed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shlex
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import yaml

from omnimarket.nodes.node_customer_path_container_test_effect.adapters import (
    DockerContainerRuntime,
)
from omnimarket.nodes.node_customer_path_container_test_effect.models.model_customer_path_container_test import (
    EnumCustomerPathOutcome,
    EnumStepStatus,
    ModelCustomerPathCommand,
    ModelCustomerPathResult,
    ModelExecResult,
    ModelStepReading,
)

logger = logging.getLogger(__name__)

URL_SETTING: Final = "ONEX_API_BASE_URL"
KEY_FILE_SETTING: Final = "ONEX_API_KEY_FILE"
PROBE_SETTING: Final = "ONEX_CUSTOMER_PATH_LAB_PROBE_TARGETS"
TENANT_SETTING: Final = "ONEX_CUSTOMER_PATH_TENANT_SLUG"
BLOCKED_SETTING: Final = "ONEX_CUSTOMER_PATH_BLOCKED_RANGES"
UV_URL_SETTING: Final = "ONEX_CUSTOMER_PATH_UV_INSTALL_URL"
PROVIDER_KEY_FILE_SETTING: Final = "ONEX_CUSTOMER_PATH_PROVIDER_KEY_FILE"
CONTRACT: Final = Path(__file__).resolve().parents[1] / "contract.yaml"
TAIL: Final = 400
KEY_PATH_IN_CONTAINER: Final = "gateway.key"


class ProtocolContainerRuntime(Protocol):
    def start(self, image: str, ttl_s: int) -> str: ...

    def exec(
        self,
        container: str,
        argv: list[str],
        *,
        user: str,
        stdin: str | None,
        timeout_s: float,
    ) -> ModelExecResult: ...

    def copy_in(
        self, container: str, source: str, destination: str
    ) -> ModelExecResult: ...

    def remove(self, container: str) -> None: ...


def _json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class HandlerCustomerPathContainerTest:
    """Start a container, run each declared step, always remove the container, and report every step."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        runtime: ProtocolContainerRuntime | None = None,
        declaration: Mapping[str, Any] | None = None,
    ) -> None:
        self._environ = environ if environ is not None else os.environ
        self._declaration = (
            declaration
            if declaration is not None
            else yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["customer_path"]
        )
        self._runtime: ProtocolContainerRuntime = runtime or DockerContainerRuntime(
            dns=self._declaration["dns_resolver"]
        )

    @staticmethod
    def _result(
        outcome: EnumCustomerPathOutcome,
        detail: str,
        steps: list[ModelStepReading] | None = None,
        **fields: str | None,
    ) -> ModelCustomerPathResult:
        steps = steps or []
        gaps = [s.guide_gap for s in steps if s.guide_gap]
        logger.info("customer path outcome=%s", outcome.value)
        return ModelCustomerPathResult(
            ok=outcome
            in (
                EnumCustomerPathOutcome.PASSED,
                EnumCustomerPathOutcome.PASSED_STEPS_NOT_RUN,
            ),
            outcome=outcome,
            detail=detail,
            steps=steps,
            guide_gaps=gaps,
            **fields,
        )

    @staticmethod
    def _read_key(path: Path) -> str | None:
        try:
            if path.stat().st_mode & 0o077:
                return None
            return path.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeError):
            return None

    @staticmethod
    def _gateway_ip(base_url: str) -> str | None:
        host = urlsplit(base_url).hostname
        if not host:
            return None
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
        except OSError:
            return None
        return str(infos[0][4][0]) if infos else None

    async def handle(
        self, request: ModelCustomerPathCommand
    ) -> ModelCustomerPathResult:
        base_url = (self._environ.get(URL_SETTING) or "").strip().rstrip("/")
        key_file = (self._environ.get(KEY_FILE_SETTING) or "").strip()
        probes = [
            t.strip()
            for t in (self._environ.get(PROBE_SETTING) or "").split(",")
            if t.strip()
        ]
        tenant_slug = (self._environ.get(TENANT_SETTING) or "").strip()
        blocked = [
            r.strip()
            for r in (self._environ.get(BLOCKED_SETTING) or "").split(",")
            if r.strip()
        ]
        uv_url = (self._environ.get(UV_URL_SETTING) or "").strip()
        provider_file = (self._environ.get(PROVIDER_KEY_FILE_SETTING) or "").strip()
        missing = [
            n
            for n, v in (
                (URL_SETTING, base_url),
                (KEY_FILE_SETTING, key_file),
                (PROBE_SETTING, probes),
                (TENANT_SETTING, tenant_slug),
                (UV_URL_SETTING, uv_url),
                (BLOCKED_SETTING, blocked),
            )
            if not v
        ]
        if missing:
            return self._result(
                EnumCustomerPathOutcome.SETTINGS_ABSENT,
                f"no settings: {' and '.join(missing)} not set (there is no default gateway, key or lab address)",
            )
        key = self._read_key(Path(key_file))
        if key is None:
            return self._result(
                EnumCustomerPathOutcome.KEY_FILE_REFUSED,
                f"the file {KEY_FILE_SETTING} names is missing, empty, unreadable or not owner-only (0600)",
            )
        provider_key: str | None = None
        if provider_file:
            provider_key = self._read_key(Path(provider_file))
            if provider_key is None:
                return self._result(
                    EnumCustomerPathOutcome.KEY_FILE_REFUSED,
                    f"the file {PROVIDER_KEY_FILE_SETTING} names is missing, empty, unreadable or not owner-only (0600)",
                )
        try:
            container = self._runtime.start(request.image, request.container_ttl_s)
        except (RuntimeError, OSError) as exc:
            return self._result(
                EnumCustomerPathOutcome.CONTAINER_START_FAILED, str(exc)[:TAIL]
            )
        try:
            return self._run_steps(
                request,
                container,
                base_url,
                probes,
                key,
                tenant_slug,
                uv_url,
                blocked,
                provider_key,
            )
        finally:
            self._runtime.remove(container)

    def _run_steps(
        self,
        request: ModelCustomerPathCommand,
        container: str,
        base_url: str,
        probes: list[str],
        key: str,
        tenant_slug: str,
        uv_url: str,
        blocked: list[str],
        provider_key: str | None = None,
    ) -> ModelCustomerPathResult:
        user = request.customer_user
        home = f"/home/{user}"
        values = {
            "base_url": base_url,
            "uv_install_url": shlex.quote(uv_url),
            "tenant_slug": shlex.quote(tenant_slug),
            "key_path": f"{home}/{KEY_PATH_IN_CONTAINER}",
            "user": user,
            "home": home,
            "task_type": shlex.quote(request.task_type),
            "prompt_arg": shlex.quote(request.prompt),
        }
        readings: list[ModelStepReading] = []
        extra: dict[str, str | None] = {}

        def scrub(text: str) -> str:
            text = text.replace(key, "<key>")
            if provider_key:
                text = text.replace(provider_key, "<provider-key>")
            return text[-TAIL:]

        def run(
            step: Mapping[str, Any],
            argv: list[str],
            stdin: str | None = None,
            user_override: str | None = None,
        ) -> ModelExecResult:
            who = user_override or ("root" if step.get("as") == "root" else user)
            filled = [a.format(**values) for a in argv]
            return self._runtime.exec(
                container,
                filled,
                user=who,
                stdin=stdin,
                timeout_s=float(step.get("timeout_s", 120)),
            )

        def reading(
            step: Mapping[str, Any],
            ok: bool,
            detail: str,
            done: ModelExecResult | None = None,
        ) -> ModelStepReading:
            return ModelStepReading(
                step_id=step["id"],
                guide_ref=step["guide_ref"],
                status=EnumStepStatus.PASSED if ok else EnumStepStatus.FAILED,
                detail=detail,
                exit_code=done.exit_code if done else None,
                output_tail=scrub((done.stdout + done.stderr) if done else ""),
                guide_gap=step.get("gap"),
            )

        failed = False
        for step in self._declaration["steps"]:
            kind = step.get("kind", "exec")
            if failed:
                readings.append(
                    ModelStepReading(
                        step_id=step["id"],
                        guide_ref=step["guide_ref"],
                        status=EnumStepStatus.NOT_RUN,
                        detail="not reached: an earlier step failed",
                    )
                )
                continue
            if kind == "own_key" and not provider_key:
                readings.append(
                    ModelStepReading(
                        step_id=step["id"],
                        guide_ref=step["guide_ref"],
                        status=EnumStepStatus.NOT_RUN,
                        detail=step["reason"],
                        guide_gap=step.get("gap"),
                    )
                )
                continue
            if kind == "not_run":
                readings.append(
                    ModelStepReading(
                        step_id=step["id"],
                        guide_ref=step["guide_ref"],
                        status=EnumStepStatus.NOT_RUN,
                        detail=step["reason"],
                    )
                )
                continue
            if kind == "copy_source":
                if not request.source_checkout:
                    readings.append(
                        reading(
                            step,
                            False,
                            "no source_checkout named; the delegation step needs the unreleased node",
                        )
                    )
                    failed = True
                    continue
                dest = f"{home}/omnimarket-src"
                done = self._runtime.copy_in(container, request.source_checkout, dest)
                if done.exit_code == 0:
                    done = self._runtime.exec(
                        container,
                        ["chown", "-R", f"{user}:{user}", dest],
                        user="root",
                        stdin=None,
                        timeout_s=120,
                    )
                step_ok = done.exit_code == 0
                readings.append(reading(step, step_ok, "checkout copied in", done))
                failed = not step_ok
                continue
            if kind == "net_block":
                route = self._runtime.exec(
                    container,
                    ["ip", "-4", "route", "show", "default"],
                    user="root",
                    stdin=None,
                    timeout_s=30,
                )
                words = route.stdout.split()
                gw = words[words.index("via") + 1] if "via" in words else None
                gw_ip = self._gateway_ip(base_url)
                cmds = [["ip", "route", "add", "blackhole", r] for r in blocked]
                if gw and gw_ip:
                    cmds.append(["ip", "route", "add", f"{gw_ip}/32", "via", gw])
                last = route
                ok = bool(gw and gw_ip)
                for cmd in cmds if ok else []:
                    last = self._runtime.exec(
                        container, cmd, user="root", stdin=None, timeout_s=30
                    )
                    if last.exit_code != 0:
                        ok = False
                        break
                readings.append(
                    reading(
                        step,
                        ok,
                        "lab ranges blackholed; one /32 route allows the gateway address"
                        if ok
                        else "could not install the routes (no default route, unresolved gateway or ip refused)",
                        last,
                    )
                )
                failed = not ok
                continue
            if kind == "lab_probe":
                bad: list[str] = []
                done = ModelExecResult(exit_code=0)
                for target in probes:
                    done = run(
                        {**step, "id": step["id"]},
                        [a.replace("{target}", target) for a in step["argv"]],
                    )
                    if done.exit_code == 0:
                        bad.append(target)
                readings.append(
                    reading(
                        step,
                        not bad,
                        f"{len(probes)} lab address(es) failed to connect, as required"
                        if not bad
                        else f"{len(bad)} lab address(es) answered from inside the container",
                        done,
                    )
                )
                failed = bool(bad)
                continue
            if kind == "smoke":
                payload = json.dumps(
                    {"prompt": request.prompt, "task_type": request.task_type}
                )
                run(step, ["tee", f"{home}/smoke-input.json"], stdin=payload)
                run(
                    step,
                    ["sh", "-c", f"umask 077; cat > {home}/{KEY_PATH_IN_CONTAINER}"],
                    stdin=key,
                )
                done = run(
                    step,
                    [
                        "sh",
                        "-c",
                        'export PATH="$HOME/.local/bin:$PATH" '
                        f"{URL_SETTING}={shlex.quote(base_url)} "
                        f"{KEY_FILE_SETTING}={shlex.quote(values['key_path'])}; "
                        "onex node node_cloud_smoke_check_effect "
                        f"--input {shlex.quote(home + '/smoke-input.json')} --output receipt",
                    ],
                )
                body = _json_object(done.stdout)
                nested = body.get("result") if isinstance(body, dict) else None
                answer = nested if isinstance(nested, dict) else body
                ok = bool(answer and answer.get("ok") is True)
                detail = (
                    f"smoke check outcome={answer.get('outcome')} model={answer.get('model')}: {str(answer.get('detail'))[:200]}"
                    if answer
                    else "the node printed no result object"
                )
                readings.append(reading(step, ok, detail, done))
                failed = not ok
                continue
            stdin_by_name = {"key": key, "provider_key": provider_key}
            done = run(step, step["argv"], stdin=stdin_by_name.get(step.get("stdin")))
            expect_failure = step.get("expect") == "failure"
            ok = (done.exit_code != 0) if expect_failure else (done.exit_code == 0)
            if kind == "receipt" and ok:
                receipt = _json_object(done.stdout)
                if receipt is None:
                    ok = False
                else:
                    extra["receipt_status"] = _text(receipt.get("status"))
                    extra["receipt_model"] = _text(receipt.get("terminal_model_used"))
                    extra["receipt_provider"] = _text(receipt.get("provider"))
                    tests = [
                        f"{k}={receipt[k]}"
                        for k in sorted(receipt)
                        if "test" in k.lower()
                    ]
                    extra["receipt_tests_run"] = (
                        ", ".join(tests) if tests else "the receipt has no tests field"
                    )
                    ok = extra["receipt_model"] is not None
            readings.append(
                reading(
                    step,
                    ok,
                    "ran as declared" if ok else "did not run as declared",
                    done,
                )
            )
            failed = not ok

        steps_failed = [r for r in readings if r.status is EnumStepStatus.FAILED]
        not_run = [
            r
            for r in readings
            if r.status is EnumStepStatus.NOT_RUN and r.detail.startswith("not reached")
        ]
        if steps_failed or not_run:
            first = steps_failed[0] if steps_failed else not_run[0]
            return self._result(
                EnumCustomerPathOutcome.STEP_FAILED,
                f"{first.step_id}: {first.detail}",
                readings,
                **extra,
            )
        skipped = [r.step_id for r in readings if r.status is EnumStepStatus.NOT_RUN]
        if skipped:
            return self._result(
                EnumCustomerPathOutcome.PASSED_STEPS_NOT_RUN,
                f"every step that ran passed; not run: {', '.join(skipped)}",
                readings,
                **extra,
            )
        return self._result(
            EnumCustomerPathOutcome.PASSED, "every step passed", readings, **extra
        )
