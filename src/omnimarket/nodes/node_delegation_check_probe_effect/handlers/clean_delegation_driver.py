# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The half of the C13 and C14 collector that runs inside the clean tool environment (OMN-26043).

Streamed on stdin to the INSTALLED tool interpreter, like ``clean_machine_driver`` and ``clean_dashboard_driver``: standard
library plus what the install put on the interpreter, nothing from this repository. It runs one ``onex delegate`` through the
customer path (``onex local init``, the guide's option A model declaration, the install's own ``onex``) under an egress
recorder, then walks the installed product's routing. It measures and never grades; the node grades the JSON record it prints.

The egress recorder is a ``sitecustomize`` module on ``PYTHONPATH`` that logs the address of every socket connect of the
process and its children, so the log is read from outside the code that did the work. A deliberate outbound connect through
the same recorder and the same parser is the control: a recorder or parser that cannot see an external connect reads the
control as not classified, and the node reads that as a failed clause, never a clean zero.
"""

from __future__ import annotations

import importlib.metadata as md
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

OUTPUT_CAP = 20000
PROMPT = "Reply with the single word: ready."
EGRESS_LOG_ENV = "ONEX_CHECK_EGRESS_LOG"
RECORDER_HEADER = "# recorder-loaded"
#: A documentation-range address (never routed): the deliberate outbound connect that must be classified external.
CONTROL_HOST = ".".join(("192", "0", "2", "1"))
CONTROL_PORT = 9
CONTROL_TARGET = f"{CONTROL_HOST}:{CONTROL_PORT}"
LOOPBACK = ".".join(("127", "0", "0", "1"))
CONTROL_BACKEND = "c14-control-undeclared-backend"
CONTROL_CODE = (
    "import socket\n"
    "s = socket.socket()\n"
    "s.settimeout(1)\n"
    "try:\n"
    f"    s.connect(({CONTROL_HOST!r}, {CONTROL_PORT}))\n"
    "except OSError:\n"
    "    pass\n"
)
RECORDER_SOURCE = f'''import os, socket
_log = os.environ.get("{EGRESS_LOG_ENV}")
if _log:
    with open(_log, "a") as _f:
        _f.write("{RECORDER_HEADER}\\n")
    _connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _note(address):
        if isinstance(address, tuple) and len(address) >= 2:
            with open(_log, "a") as f:
                f.write(f"{{address[0]}}:{{address[1]}}\\n")

    def connect(self, address):
        _note(address)
        return _connect(self, address)

    def connect_ex(self, address):
        _note(address)
        return _connect_ex(self, address)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
'''
GENERATED_TOKENS = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*(?:generation_tokens|tokens_predicted|completion_tokens|n_decode)[A-Za-z0-9_:]*)"
    r"(?:\{[^}]*\})?\s+(?P<value>[0-9.eE+-]+)\s*$",
    re.MULTILINE,
)


def run(
    argv: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 900,
) -> dict[str, Any]:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=None if env is None else dict(env),
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": None, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}
    return {
        "rc": done.returncode,
        "stdout": done.stdout[-OUTPUT_CAP:],
        "stderr": done.stderr[-OUTPUT_CAP:],
    }


def _hostport(url: str) -> str | None:
    parts = urlsplit(url if "//" in url else f"//{url}")
    return f"{parts.hostname}:{parts.port}" if parts.hostname and parts.port else None


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def classify_connects(log: Sequence[str], responder_url: str) -> tuple[list[str], int]:
    """The connects beyond loopback and the responder, and how many reached the responder. The recorder's header is skipped."""
    responder = _hostport(responder_url)
    external: list[str] = []
    model_connects = 0
    for line in log:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host, _, _port = line.rpartition(":")
        if line == responder:
            model_connects += 1
        elif not _is_loopback(host):
            external.append(line)
    return external, model_connects


def generated_tokens(metrics_text: str) -> float:
    """The sum of the generation counters a responder's ``/metrics`` shows (vLLM and llama.cpp spell them differently).

    llama.cpp prints ``tokens_predicted_total`` to six significant digits, so a short answer moves it by less than its last
    digit; ``n_decode_total`` counts decode calls exactly and moves with every generated token.
    """
    return sum(float(m.group("value")) for m in GENERATED_TOKENS.finditer(metrics_text))


def _json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_run(
    run_dir: Path,
    packages: Sequence[str] = ("omnibase_core", "omnibase_infra", "omnimarket"),
) -> dict[str, Any]:
    """What the run directory's three files say. A file that is absent or does not parse reads as not agreeing."""
    receipt = _json_file(run_dir / "receipt.json")
    run_doc = _json_file(run_dir / "run.json")
    has_result = (run_dir / "result.txt").is_file()
    inner = (receipt or {}).get("receipt") or {}
    result = inner.get("result") or {}
    attempts = result.get("attempts") or []
    tiers = [str(a.get("tier")) for a in attempts if isinstance(a, dict)]
    hops = [
        {
            "tier": str(a.get("tier")),
            "backend": a.get("backend_ref") or a.get("backend"),
            "model": a.get("model_used") or a.get("model"),
            "outcome": a.get("acceptance_decision"),
        }
        for a in attempts
        if isinstance(a, dict)
    ]
    last = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    agree = bool(
        receipt
        and run_doc
        and has_result
        and receipt.get("run_id") == run_doc.get("run_id") == run_dir.name
        and receipt.get("correlation_id") == run_doc.get("correlation_id")
        and inner.get("correlation_id") in (None, receipt.get("correlation_id"))
    )
    # The identity is stamped inside the receipt the run wrote, beside the attempts.
    identity = (inner.get("runtime_identity") or {}).get("packages") or {}
    no_checkout = bool(packages) and all(
        (identity.get(name) or {}).get("source") == "registry" for name in packages
    )
    metrics = result.get("metrics") or {}
    cost = metrics.get("cost_usd")
    return {
        "three_files_agree": agree,
        "receipt_tier": (receipt or {}).get("routing_tier"),
        "receipt_model": (receipt or {}).get("model"),
        "receipt_endpoint": (receipt or {}).get("endpoint"),
        "attempt_tiers": tiers,
        "attempt_hops": hops,
        "accepted_last": last.get("acceptance_decision") == "accept",
        "escalated": bool(result.get("escalation_count")),
        "cost_usd": float(cost) if isinstance(cost, int | float) else 0.0,
        "no_checkout": no_checkout,
    }


def endpoint_state(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=3):
            return "serving"
    except OSError:
        return "unreachable"


def _closed_port_control_holds() -> bool:
    with socket.socket() as listener:
        listener.bind((LOOPBACK, 0))
        listener.listen(1)
        open_port = listener.getsockname()[1]
        serving = endpoint_state(LOOPBACK, open_port) == "serving"
    # The listener is closed now: the same port is the closed-port control.
    return serving and endpoint_state(LOOPBACK, open_port) == "unreachable"


def walk_routing(*, scratch: Path, tiers_path: Path | None = None) -> dict[str, Any]:
    """The routing walk on the installed product: every tier backend binds, and the two controls are classified."""
    import yaml

    from omnimarket.validators.routing_tier_backend_bindability import (
        ROUTING_TIERS_PATH,
        find_unbindable_tier_backends,
    )

    base = tiers_path or ROUTING_TIERS_PATH
    findings = find_unbindable_tier_backends(tiers_path=base)
    unbindable = [
        f"{cls}:{bid}" for cls, ids in sorted(findings.items()) for bid in ids
    ]
    tiers = yaml.safe_load(base.read_text(encoding="utf-8"))
    tiers["tiers"][0].setdefault("models", []).append({"backend_id": CONTROL_BACKEND})
    control = scratch / "control_tiers.yaml"
    control.write_text(yaml.safe_dump(tiers), encoding="utf-8")
    seeded = find_unbindable_tier_backends(tiers_path=control)
    return {
        "unbindable": unbindable,
        "undeclared_control_classified_deleted": CONTROL_BACKEND
        in seeded.get("undeclared", []),
        "closed_port_control_classified_unreachable": _closed_port_control_holds(),
    }


def read_judge(
    base_url: str, model: str, models_read: tuple[int | None, Any]
) -> dict[str, bool]:
    """The reviewer leg on a customer machine is the customer's own model: private, no credential, serving the model."""
    status, body = models_read
    host = urlsplit(base_url).hostname or ""
    try:
        private = not ipaddress.ip_address(host).is_global
    except ValueError:
        private = False
    data = body.get("data") if isinstance(body, dict) else None
    ids = (
        [d.get("id") for d in data if isinstance(d, dict)]
        if isinstance(data, list)
        else []
    )
    return {
        "judge_endpoint_private": private,
        "judge_needs_credential": status != 200,
        "judge_serves_model": status == 200 and model in ids,
    }


def _get(url: str) -> tuple[int | None, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as response:
            return response.status, response.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (OSError, ValueError):
        return None, ""


def _models_read(base_url: str) -> tuple[int | None, Any]:
    status, text = _get(base_url.rstrip("/") + "/v1/models")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


def counter_after(url: str, before: float, *, wait_s: float = 15.0) -> float:
    """The responder's generated-token counter after the run; a server that publishes it a moment after the answer is waited for."""
    deadline = time.monotonic() + wait_s
    while True:
        after = generated_tokens(_get(url.rstrip("/") + "/metrics")[1])
        if after > before or time.monotonic() >= deadline:
            return after
        time.sleep(1.0)


def declare_model(base_url: str, model: str) -> None:
    """The guide's option A: the model server's complete chat-completions address, for both local rungs."""
    endpoint = base_url.rstrip("/") + "/v1/chat/completions"
    lines = ["backends:"]
    for backend in ("local-coder", "local-heavy-reasoning"):
        lines += [
            f"  - backend_id: {backend}",
            f"    endpoint_url: {json.dumps(endpoint)}",
            f"    model_name: {json.dumps(model)}",
        ]
    target = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def installed_versions(names: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            continue
    return out


def read_arguments(argv: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    key: str | None = None
    for token in argv:
        if token in ("--onex", "--url", "--model"):
            key, out[token[2:]] = token[2:], None
        elif token == "--packages":
            key, out["packages"] = "packages", []
        elif key == "packages":
            out["packages"].append(token)
        elif key is not None:
            out[key], key = token, None
        else:
            raise SystemExit(f"unexpected argument: {token}")
    for needed in ("onex", "url", "model"):
        if not out.get(needed):
            raise SystemExit(f"--{needed} is required")
    out.setdefault("packages", [])
    return out


def _read_log(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def drive() -> int:
    args = read_arguments(sys.argv[1:])
    onex, url, model = args["onex"], args["url"], args["model"]
    record_stream, sys.stdout = sys.stdout, sys.stderr
    scratch = Path(tempfile.mkdtemp(prefix="c13-driver-"))
    recorder = scratch / "recorder"
    recorder.mkdir()
    (recorder / "sitecustomize.py").write_text(RECORDER_SOURCE, encoding="utf-8")
    work = scratch / "work"
    work.mkdir()
    bin_dir = str(Path(onex).parent)
    base_env = dict(os.environ)  # ONEX_FLAG_EXEMPT: the clean machine's own environment
    base_env["PATH"] = os.pathsep.join([bin_dir, base_env.get("PATH", "")])

    def recorded(log: Path) -> dict[str, str]:
        return {**base_env, "PYTHONPATH": str(recorder), EGRESS_LOG_ENV: str(log)}

    setup = run(
        [onex, "local", "init", "--json"], env=base_env, cwd=str(work), timeout=300
    )
    if setup["rc"] == 0:
        declare_model(url, model)
    entry = shutil.which("onex", path=base_env["PATH"])
    entry_is_install_own = bool(entry) and os.path.realpath(
        str(entry)
    ) == os.path.realpath(onex)
    before = generated_tokens(_get(url.rstrip("/") + "/metrics")[1])
    delegate_log = scratch / "delegate.log"
    delegation = run(
        [onex, "delegate", PROMPT, "--json"], env=recorded(delegate_log), cwd=str(work)
    )
    after = counter_after(url, before)
    control_log = scratch / "control.log"
    run([sys.executable, "-c", CONTROL_CODE], env=recorded(control_log), timeout=30)
    log = _read_log(delegate_log)
    external, model_connects = classify_connects(log, url)
    control_external, _ = classify_connects(_read_log(control_log), url)
    # With no --state-root the CLI keeps its runs under the home directory, which is the clean machine's own.
    runs = sorted(
        (
            d
            for d in (Path.home() / ".onex_state" / "runs").glob("*")
            if (d / "receipt.json").is_file()
        ),
        key=lambda d: d.stat().st_mtime,
    )
    observed = (
        read_run(runs[-1], [p.replace("-", "_") for p in args["packages"]])
        if runs
        else {}
    )
    judge = read_judge(url, model, _models_read(url))
    record: dict[str, Any] = {
        "rc": delegation["rc"] if delegation["rc"] is not None else -1,
        "no_checkout": False,
        "three_files_agree": False,
        "attempt_tiers": [],
        **observed,
        "egress_recorded": RECORDER_HEADER in [line.strip() for line in log],
        "egress_external": external,
        "model_connects": model_connects,
        "token_counter_moved": after > before,
        "control_external_classified": CONTROL_TARGET in control_external,
        "entry_path": str(entry) if entry else None,
        "entry_is_install_own": entry_is_install_own,
        "versions": installed_versions(args["packages"]),
        "routing": {**walk_routing(scratch=scratch), **judge},
    }
    json.dump(record, record_stream)
    record_stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(drive())
