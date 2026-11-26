# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The half of the C13 and C14 collector that runs inside the clean tool environment (OMN-26046).

The collector streams this file on stdin to the INSTALLED tool environment's interpreter, after the guide's install line has
run, so it imports the standard library and whatever the install put on the interpreter, nothing from this repository. It
measures and never grades; the node grades the JSON document it prints.

What it does, in order, with every host, model and window handed in on the command line (they are the settings row's responder
chain and the model server's own reading, never a literal here):

* writes the customer's overlay in its default place, binding the two shipped local rungs to the responder;
* starts a recording forward proxy that sits IN the path of every process of the run (proxy settings are environment, which
  the product's child processes inherit), records each destination and refuses any non-private one;
* runs one real ``onex delegate`` through it and reads the run's ``receipt.json`` and its three files;
* sends one deliberate outbound request through the same proxy, so the parser's ability to see an external host is measured;
* walks the routing of every customer-visible task class through the installed reducer's own helpers and classifies each
  backend it could hand the class, with two negative controls through the same classifier.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import http.server
import importlib
import importlib.metadata as md
import ipaddress
import json
import re
import selectors
import shutil
import socket
import subprocess
import sys
import threading
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

_SAFE_TARGET = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*")
_TARGET_ALPHABET = {
    c: c
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~!$&'()*+,;=:@%/?-"
}

PRODUCT_DISTS = ("omnibase-core", "omnibase-infra", "omnimarket")
PRODUCT_MODULES = ("omnimarket", "omnibase_infra", "omnibase_core")
DELETED_BACKEND_ID = "c14-control-backend-declared-nowhere"
# This machine's own loopback address, asked of the resolver: no host literal sits in a handler of this node.
LOOPBACK = str(socket.getaddrinfo(None, 0, socket.AF_INET)[0][4][0])
# A globally routable address, built from its integer (TEST-NET ranges count as private to the standard library). The recorder
# classifies it external and refuses it without ever connecting.
EXTERNAL_CONTROL_URL = (
    f"http://{ipaddress.IPv4Address(0x08080808)}/c14-external-control"
)


def closed_port_endpoint() -> str:
    """An endpoint on a loopback port nothing listens on: the unreachable negative control."""
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        port = probe.getsockname()[1]
    return f"http://{LOOPBACK}:{port}/v1/chat/completions"


OUTPUT_CAP = 20000


def address_is_private(raw: str) -> bool:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def endpoint_is_private(endpoint_url: str) -> bool:
    host = urlsplit(endpoint_url).hostname or ""
    if not host:
        return False
    try:
        return address_is_private(str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    try:
        addresses = {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addresses) and all(address_is_private(a) for a in addresses)


def private_address(endpoint_url: str) -> str | None:
    if not endpoint_is_private(endpoint_url):
        return None
    host = urlsplit(endpoint_url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return None
    for info in infos:
        candidate = str(info[4][0])
        if address_is_private(candidate):
            return ipaddress.ip_address(candidate.split("%", 1)[0]).compressed
    return None


def _pipe(client: socket.socket, upstream: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while True:
            events = selector.select(timeout=600)
            if not events:
                return
            for key, _ in events:
                data = cast("socket.socket", key.fileobj).recv(65536)
                if not data:
                    return
                key.data.sendall(data)
    except OSError:
        return
    finally:
        selector.close()
        upstream.close()


class EgressRecorder:
    """A recording forward proxy in the path of the run. A non-private destination is recorded, then refused."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:
                return

            def _record(self, method: str, target: str, host_url: str) -> None:
                with recorder._lock:
                    recorder.entries.append(
                        {
                            "method": method,
                            "target": target,
                            "private": endpoint_is_private(host_url),
                        }
                    )

            def do_CONNECT(self) -> None:
                host, _, port = self.path.partition(":")
                target = f"https://{host}:{port or 443}"
                self._record("CONNECT", self.path, target)
                address = private_address(target)
                if address is None:
                    self.send_error(403, "c14 probe: non-private egress refused")
                    return
                try:
                    upstream = socket.create_connection(
                        (address, int(port or 443)), timeout=30
                    )
                except OSError:
                    self.send_error(502)
                    return
                self.send_response(200, "Connection Established")
                self.end_headers()
                _pipe(self.connection, upstream)

            def forward(self) -> None:
                self._record(self.command, self.path, self.path)
                parts = urlsplit(self.path)
                address = private_address(self.path)
                if address is None:
                    self.send_error(403, "c14 probe: non-private egress refused")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in {"proxy-connection", "connection", "keep-alive"}
                }
                path = parts.path + (f"?{parts.query}" if parts.query else "")
                path = path or "/"
                if _SAFE_TARGET.fullmatch(path) is None or path.startswith("//"):
                    self.send_error(400, "c14 probe: malformed request target")
                    return
                try:
                    conn = http.client.HTTPConnection(
                        address, parts.port or 80, timeout=900
                    )
                    headers["Host"] = parts.netloc
                    safe_path = "".join(_TARGET_ALPHABET[c] for c in path)
                    conn.request(self.command, safe_path, body=body, headers=headers)
                    response = conn.getresponse()
                    payload = response.read()
                except OSError:
                    self.send_error(502)
                    return
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in {
                        "transfer-encoding",
                        "connection",
                        "content-length",
                    }:
                        self.send_header(
                            key.replace("\r", "").replace("\n", ""),
                            value.replace("\r", "").replace("\n", ""),
                        )
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

        for verb in ("GET", "POST", "PUT", "DELETE", "HEAD"):
            setattr(Handler, f"do_{verb}", Handler.forward)
        self._server = http.server.ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> EgressRecorder:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def run(
    argv: list[str], env: Mapping[str, str], cwd: Path, timeout: float
) -> dict[str, Any]:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(env),
            cwd=str(cwd),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "argv": argv,
            "rc": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }
    return {
        "argv": argv,
        "rc": done.returncode,
        "stdout": done.stdout[-OUTPUT_CAP:],
        "stderr": done.stderr[-OUTPUT_CAP:],
    }


def _git_ancestor(path: Path, root: Path) -> str | None:
    """A ``.git`` at or above ``path``, looking no higher than the clean root the install lives under."""
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return str(parent)
        if parent == root:
            return None
    return None


def distribution_facts(root: Path) -> dict[str, Any]:
    """Where each OmniNode distribution came from: the package index leaves no direct_url.json, a checkout or path does."""
    out: dict[str, Any] = {}
    for name in PRODUCT_DISTS:
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            out[name] = {"installed": False}
            continue
        direct = dist.read_text("direct_url.json")
        out[name] = {
            "installed": True,
            "version": dist.version,
            "direct_url": bool(direct),
        }
    for module_name in PRODUCT_MODULES:
        module = importlib.import_module(module_name)
        path = Path(str(module.__file__)).resolve()
        out.setdefault(module_name, {}).update(
            {
                "path": str(path),
                "in_site_packages": any(
                    part in ("site-packages", "dist-packages") for part in path.parts
                ),
                "git_ancestor": _git_ancestor(path.parent, root),
            }
        )
    return out


def three_files_agree(run_doc: Mapping[str, Any]) -> bool:
    """result.txt, receipt.json and run.json exist, parse, and agree on the correlation and run ids."""
    run_dir = run_doc.get("run_dir")
    receipt = run_doc.get("receipt")
    skill = run_doc.get("skill_result") or {}
    if not run_dir or not isinstance(receipt, dict):
        return False
    try:
        run_json = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
        text = (Path(run_dir) / "result.txt").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    correlation = {
        receipt.get("correlation_id"),
        (receipt.get("receipt") or {}).get("correlation_id"),
        run_json.get("correlation_id"),
        skill.get("correlation_id"),
    }
    run_ids = {run_json.get("run_id"), skill.get("run_id")}
    return (
        bool(text.strip())
        and len(correlation) == 1
        and None not in correlation
        and len(run_ids) == 1
        and None not in run_ids
    )


def write_overlay(home: Path, *, endpoint: str, served: str, window: int) -> Path:
    """The customer's overlay in its default place: the two shipped local rungs bound to the responder."""
    import yaml

    from omnimarket.handlers.routing.routing_tiers_path import (
        ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
    )

    shipped = yaml.safe_load(
        ROUTING_TIERS_PACKAGED_DEFAULT_PATH.read_text(encoding="utf-8")
    )
    local = next(tier for tier in shipped["tiers"] if tier["name"] == "local")
    use_for: dict[str, set[str]] = {}
    for model in local["models"]:
        use_for.setdefault(model["backend_id"], set()).update(model["use_for"])
    backends = [
        {
            "backend_id": backend_id,
            "endpoint_url": f"{endpoint}/v1/chat/completions",
            "model_name": served,
            "tier": "local",
            "placement": {
                "tier": "local",
                "max_context_tokens": window,
                "use_for": sorted(use_for[backend_id]),
            },
        }
        for backend_id in ("local-coder", "local-heavy-reasoning")
    ]
    overlay = {
        "config_version": "2.1.0",
        "schema_version": "bifrost_delegation.v1",
        "backends": backends,
    }
    target = home / ".omninode" / "delegation" / "bifrost_overrides.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(overlay, handle, sort_keys=False)
    return target


def receipt_result_of(receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    result = (receipt.get("receipt") or {}).get("result")
    if not isinstance(result, dict):
        return None
    terminal = result.get("terminal_payload")
    return terminal if isinstance(terminal, dict) else result


def run_delegation(
    onex: str,
    env: Mapping[str, str],
    work_dir: Path,
    *,
    prompt: str,
    task_type: str,
    timeout: float,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    state_root = work_dir.resolve() / ".onex_state"
    with EgressRecorder() as recorder:
        run_env = dict(env)
        run_env["ONEX_STATE_DIR"] = str(state_root)
        for key in ("NO_PROXY", "no_proxy"):
            run_env.pop(key, None)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            run_env[key] = recorder.url
        proc = run(
            [onex, "delegate", prompt, "--task-type", task_type, "--json"],
            run_env,
            work_dir,
            timeout,
        )
        session_egress = list(recorder.entries)
        # The control goes through the same recorder and the same parser as the run itself.
        before = len(recorder.entries)
        opener_error = None
        try:
            proxy = urlsplit(recorder.url)
            conn = http.client.HTTPConnection(
                proxy.hostname or LOOPBACK, proxy.port, timeout=20
            )
            conn.request("GET", EXTERNAL_CONTROL_URL)
            conn.getresponse().read()
        except OSError as exc:
            opener_error = f"{type(exc).__name__}: {exc}"
        control_entries = list(recorder.entries[before:])
    run_doc: dict[str, Any] = {
        "exit_code": proc["rc"],
        "stderr_tail": (proc["stderr"] or "")[-2000:],
        "egress": session_egress,
        "control_egress": control_entries,
        "control_error": opener_error,
    }
    skill_result: Any = {}
    try:
        skill_result = json.loads((proc["stdout"] or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        run_doc["terminal_error"] = (
            "stdout carried no JSON: " + (proc["stdout"] or "")[-600:]
        )
    run_id = skill_result.get("run_id") if isinstance(skill_result, dict) else None
    run_dir = state_root / "runs" / str(run_id)
    receipt_path = run_dir / "receipt.json"
    if run_id and receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        run_doc["receipt_result"] = receipt_result_of(receipt)
        run_doc["receipt"] = receipt
        files: dict[str, Any] = {}
        for name in ("result.txt", "receipt.json", "run.json"):
            path = run_dir / name
            files[name] = path.is_file() and path.stat().st_size > 0
        run_doc["files"] = files
        run_doc["three_files_agree"] = False
        run_doc["run_dir"] = str(run_dir)
    run_doc["skill_result"] = skill_result if isinstance(skill_result, dict) else {}
    run_doc["three_files_agree"] = three_files_agree(run_doc)
    return run_doc


def _classify_private(
    facts: dict[str, Any],
    endpoint: str,
    model_name: str,
    served_cache: dict[str, Any],
    probe_served_models: Any,
) -> dict[str, Any]:
    if endpoint not in served_cache:
        served_cache[endpoint] = probe_served_models(endpoint, timeout_seconds=10.0)
    served = served_cache[endpoint]
    facts["served_models"] = sorted(served) if served else None
    if not served:
        facts["classification"] = "unreachable"
    elif model_name not in served and model_name.lower() not in {
        s.lower() for s in served
    }:
        facts["classification"] = "model_not_served"
    else:
        facts["classification"] = "bindable"
    return facts


def _classify_backend(
    backend_id: str,
    *,
    declarations: Mapping[str, Any],
    loaded: Mapping[str, Any],
    served_cache: dict[str, Any],
    probe_served_models: Any,
) -> dict[str, Any]:
    declaration = declarations.get(backend_id)
    if declaration is None:
        return {"classification": "deleted_backend"}
    ref = loaded.get(backend_id)
    if ref is None:
        return {"classification": "no_endpoint"}
    endpoint = str(ref.endpoint_url)
    facts: dict[str, Any] = {
        "endpoint": endpoint,
        "model_name": ref.model_name,
        "requires_credential": bool(ref.api_key_ref),
        "endpoint_private": endpoint_is_private(endpoint),
    }
    if not facts["endpoint_private"]:
        facts["classification"] = "bindable"
        return facts
    return _classify_private(
        facts, endpoint, ref.model_name, served_cache, probe_served_models
    )


def _private(owner: object, name: str) -> Any:
    """A name the reducer and judge keep to themselves, read from the owner's own namespace.

    The walk grades the helpers the product calls, so it calls those, not copies of them.
    """
    if isinstance(owner, types.ModuleType):
        return vars(owner)[name]
    return vars(type(owner))[name].__get__(owner)


def routing_walk() -> dict[str, Any]:
    import yaml

    from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
        load_bifrost_delegation_config,
    )
    from omnimarket.handlers.routing.routing_tiers_path import (
        ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
    )
    from omnimarket.inference.delegation_config_provenance import (
        resolve_bifrost_path_binding,
    )
    from omnimarket.inference.task_class_authority import load_task_class_authority
    from omnimarket.nodes.node_delegation_quality_gate_reducer.judge import (
        adapter_routing_resolved_judge as judge_module,
    )
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as reducer,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
        probe_served_models,
    )

    out: dict[str, Any] = {}
    shipped = ROUTING_TIERS_PACKAGED_DEFAULT_PATH.resolve()
    authority = load_task_class_authority()
    public = sorted(authority.public_task_classes)
    out["public_task_classes"] = public
    shipped_contract = yaml.safe_load(
        (shipped.parent / "task_class_contracts.v1.yaml").read_text(encoding="utf-8")
    )
    raw_classes = (shipped_contract or {}).get("task_classes") or {}
    out["public_task_classes_from_contract_yaml"] = sorted(
        name
        for name, entry in raw_classes.items()
        if isinstance(entry, dict) and entry.get("gateway_exposure") == "public"
    )
    binding = resolve_bifrost_path_binding()
    merged = load_bifrost_delegation_config(
        config_path=binding.contract_path, overlay_path=binding.overlay_path
    )
    declarations = {b.backend_id: b for b in merged.backends if b.backend_id}
    loaded = _private(reducer, "_load_bifrost_endpoints")()
    config = _private(reducer, "_get_config")()
    contract = _private(reducer, "_get_task_class_contract")()
    tier_by_name = {tier.name: tier for tier in config.tiers}
    served_cache: dict[str, Any] = {}
    walk: dict[str, list[dict[str, Any]]] = {}
    referenced: set[str] = set()
    for task_class in public:
        entry = _private(reducer, "_task_class_entry")(contract, task_class)
        policy = (entry or {}).get("escalation_policy")
        declared_order = policy.get("tier_order") if isinstance(policy, dict) else None
        names = (
            list(declared_order)
            if isinstance(declared_order, list) and declared_order
            else [
                t.name
                for t in _private(reducer, "_tier_order_from_contract")(config, entry)
            ]
        )
        override = _private(reducer, "_get_contract_model_ref")(
            task_class, contract=contract
        )
        explicit = _private(reducer, "_is_explicit_task_model_override")(
            task_class, contract
        )
        tiers_out: list[dict[str, Any]] = []
        for name in names:
            tier = tier_by_name.get(name)
            if tier is None:
                tiers_out.append({"tier": name, "declared": False, "candidates": []})
                continue
            candidates = [m.backend_ref for m in tier.models if task_class in m.use_for]
            if explicit and override is not None:
                candidates += [m.backend_ref for m in tier.models if m.id == override]
            unique = sorted(set(candidates))
            referenced.update(unique)
            tiers_out.append({"tier": name, "declared": True, "candidates": unique})
        walk[task_class] = tiers_out
    out["walk"] = walk
    out["backends"] = {
        backend_id: _classify_backend(
            backend_id,
            declarations=declarations,
            loaded=loaded,
            served_cache=served_cache,
            probe_served_models=probe_served_models,
        )
        for backend_id in sorted(referenced)
    }
    closed = closed_port_endpoint()
    out["controls"] = [
        {
            "name": "deleted_backend_control",
            **_classify_backend(
                DELETED_BACKEND_ID,
                declarations=declarations,
                loaded=loaded,
                served_cache=served_cache,
                probe_served_models=probe_served_models,
            ),
        },
        {
            "name": "unreachable_endpoint_control",
            **_classify_private(
                {"endpoint_private": endpoint_is_private(closed)},
                closed,
                "c14-control-model",
                served_cache,
                probe_served_models,
            ),
        },
    ]
    try:
        resolved = _private(
            judge_module.RoutingResolvedJudgeInferenceAdapter(), "_resolve_backend"
        )()
        endpoint = str(resolved.endpoint_ref)
        judge: dict[str, Any] = {
            "backend_id": str(getattr(resolved, "backend_id", "")),
            "endpoint": endpoint,
            "model_id": str(resolved.model_id),
            "requires_credential": bool(getattr(resolved, "secret_ref", None)),
            "endpoint_private": endpoint_is_private(endpoint),
        }
        if judge["endpoint_private"]:
            _classify_private(
                judge,
                endpoint,
                str(resolved.model_id),
                served_cache,
                probe_served_models,
            )
        out["judge"] = judge
    except Exception as exc:
        out["judge"] = {"error": f"{type(exc).__name__}: {exc}"[:600]}
    return out


def drive(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onex", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--served-model", required=True)
    parser.add_argument("--window", type=int, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--task-type", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--packages", nargs="*", default=[])
    args = parser.parse_args(argv)

    env: dict[str, str] = json.loads(Path(args.env_file).read_text(encoding="utf-8"))
    home = Path(env["HOME"])
    work_dir = Path(args.root) / "work"
    document: dict[str, Any] = {
        "python": sys.version.split()[0],
        "isolated_interpreter": bool(sys.flags.isolated),
    }
    versions: dict[str, str] = {}
    for name in args.packages:
        with contextlib.suppress(md.PackageNotFoundError):
            versions[name] = md.version(name)
    document["versions"] = versions
    document["distributions"] = distribution_facts(Path(args.root))
    document["entry"] = {
        "resolved": shutil.which("onex", path=env.get("PATH")),
        "install_own": args.onex,
    }
    overlay = write_overlay(
        home, endpoint=args.base_url, served=args.served_model, window=args.window
    )
    document["overlay"] = str(overlay)
    document["local_init"] = run(
        [args.onex, "local", "init", "--json"], env, Path(args.root), 180
    )
    document["run"] = run_delegation(
        args.onex,
        env,
        work_dir,
        prompt=args.prompt,
        task_type=args.task_type,
        timeout=args.timeout,
    )
    try:
        document["routing"] = routing_walk()
    except Exception as exc:
        document["routing"] = {"collect_error": f"{type(exc).__name__}: {exc}"[:1500]}
    sys.stdout.write(json.dumps(document))
    return 0
