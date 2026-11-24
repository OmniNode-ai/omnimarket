# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C13 collector: one real customer-local delegation on a clean tool environment, traced for egress (OMN-26043).

``LocalDelegationCustomerMachine`` is a customer machine whose ``run_delegation`` is real for C13 and C14 and whose routing walk
is real for C14 (the walk runs under the installed tool's own interpreter, after the delegation, on the same machine). Its
surface collector is still absent (C17 keeps reading PRECONDITION-ABSENT until its collector lands). The machine is a
temporary directory: its own home, uv tool directory and tool bin directory, a PATH holding only ``uv`` and the system
directories, and none of the registry-root variables, so the install lands where a customer's would and nothing the host
carries stands in for it. The customer configures the one documented way, a machine-local overlay file under their home
written after a negative control that runs with no file at all. Every step runs under ``strace -f -e trace=connect``; the
model server's own token counter and a deliberate outbound connect are the two positive controls that make a zero proven.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.observe_delegation_run import (
    KIND_EXTERNAL,
    KIND_MODEL,
    KIND_NAME_LOOKUP,
    ONEX_DISTRIBUTIONS,
    RUN_FILES,
    Connect,
    load_json,
    no_checkout_findings,
    of_kind,
    parse_connects,
    receipt_facts,
    three_files_findings,
    tokens_from_metrics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    ModelDelegateObservation,
    ModelRoutingCollection,
    ModelSurfaceObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

TOOL_PACKAGES: Final = ("omnibase-core", "omnibase-infra", "omnimarket")
SYSTEM_PATH: Final = ("/usr/bin", "/bin")
#: The only host variables a clean environment keeps: how to reach the package index through a proxy or a private CA.
PASSTHROUGH_ENV: Final = (
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
#: The one documented customer configuration file, relative to the customer's home.
OVERLAY_RELATIVE_PATH: Final = ".omninode/delegation/bifrost_overrides.yaml"
#: The shipped local backends the customer overlay points at their own model.
LOCAL_BACKEND_IDS: Final = ("local-coder", "local-heavy-reasoning")
PROMPT: Final = "explain what a calendar app needs"
INSTALL_TIMEOUT_S: Final = 900
STEP_TIMEOUT_S: Final = 600
SHORT_TIMEOUT_S: Final = 120
METRICS_TIMEOUT_S: Final = 10
MAX_TOKENS: Final = 8192
SURFACE_ABSENT: Final = "the clean-machine run of C17 is not wired to this node yet; the C13 delegation and the C14 routing walk are"
UNSUPPORTED_CHECK: Final = "this machine runs the delegation of C13 and C14 only"
#: The classes the routing walk driver reports for its two negative controls, and for an endpoint that serves its model.
CLASS_DELETED_BACKEND: Final = "deleted_backend"
CLASS_UNREACHABLE: Final = "unreachable"
CLASS_BINDABLE: Final = "bindable"

#: Runs inside the installed tool's own interpreter: the overlay places each shipped local backend at the customer's model, with
#: the classes the shipped routing_tiers.yaml names for it (an install no overlay root names a code budget on is not offered the
#: packaged local rungs, so a bare ``tier: local`` binds nothing).
OVERLAY_DRIVER: Final = """\
import sys

import yaml
from omnimarket.handlers.routing.routing_tiers_path import ROUTING_TIERS_PACKAGED_DEFAULT_PATH

target, endpoint, served, window, max_tokens, backend_ids = sys.argv[1:7]
shipped = yaml.safe_load(ROUTING_TIERS_PACKAGED_DEFAULT_PATH.read_text(encoding="utf-8"))
local = next(tier for tier in shipped["tiers"] if tier["name"] == "local")
use_for = {}
for model in local["models"]:
    use_for.setdefault(model["backend_id"], set()).update(model["use_for"])
backends = [
    {
        "backend_id": backend_id,
        "endpoint_url": endpoint,
        "model_name": served,
        "tier": "local",
        "timeout_ms": 240000,
        "max_tokens": int(max_tokens),
        "placement": {
            "tier": "local",
            "max_context_tokens": int(window),
            "use_for": sorted(use_for[backend_id]),
        },
    }
    for backend_id in backend_ids.split(",")
]
overlay = {"config_version": "2.1.0", "schema_version": "bifrost_delegation.v1", "backends": backends}
with open(target, "w", encoding="utf-8") as handle:
    yaml.safe_dump(overlay, handle, sort_keys=False)
"""

#: Runs inside the installed tool's own interpreter: the install-origin record of each distribution (None when it came from an index).
DIRECT_URL_DRIVER: Final = (
    "import importlib.metadata as m, json\n"
    "out = {}\n"
    "for d in %r:\n"
    "    try:\n"
    "        out[d] = m.distribution(d).read_text('direct_url.json')\n"
    "    except m.PackageNotFoundError:\n"
    "        out[d] = 'NOT INSTALLED'\n"
    "print(json.dumps(out))\n"
)
OUTBOUND_DRIVER: Final = "import socket, sys; s = socket.socket(); s.settimeout(5); s.connect_ex((sys.argv[1], int(sys.argv[2])))"

#: Runs inside the installed tool's own interpreter: the first endpoint the installed routing contract declares that is not on a
#: private network, resolved to an address. The deliberate outbound connect of the positive control goes there, so no address
#: lives in this node: it is the one the product itself would call for a cloud rung.
TARGET_DRIVER: Final = """\
import ipaddress
import json
import socket
from urllib.parse import urlsplit

from omnimarket.nodes.node_delegation_routing_reducer.handlers import handler_delegation_routing as reducer


def is_private(raw):
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_private or address.is_loopback


out = {"target": None, "error": None}
try:
    for backend_id, ref in sorted(reducer._load_bifrost_endpoints().items()):
        parts = urlsplit(str(ref.endpoint_url))
        if not parts.hostname:
            continue
        try:
            port = parts.port or socket.getservbyname(parts.scheme)
            infos = socket.getaddrinfo(parts.hostname, port, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            continue
        public = [info[4][0] for info in infos if not is_private(info[4][0])]
        if public:
            out["target"] = {"host": public[0], "port": port, "backend_id": backend_id}
            break
except Exception as exc:
    out["error"] = type(exc).__name__ + ": " + str(exc)[:400]
print(json.dumps(out))
"""

#: Runs inside the installed tool's own interpreter (``-I``): the routing walk of C14. Every customer-visible task class is
#: walked tier by tier through the installed reducer's own helpers, every backend a tier could hand the class is classified on
#: this machine (declared, complete endpoint after the machine's overlay, and for a private endpoint actually serving its
#: model), and the reviewer leg is resolved through the judge adapter's own resolver. Two negative controls go through the same
#: classifier: a backend declared nowhere, and a loopback endpoint on a port that was just closed. It prints one JSON document.
ROUTING_DRIVER: Final = """\
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import load_bifrost_delegation_config
from omnimarket.handlers.routing.routing_tiers_path import ROUTING_TIERS_PACKAGED_DEFAULT_PATH, ROUTING_TIERS_PATH_ENV_KEY
from omnimarket.inference.delegation_config_provenance import resolve_bifrost_path_binding
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge import adapter_routing_resolved_judge as judge_module
from omnimarket.nodes.node_delegation_routing_reducer.handlers import handler_delegation_routing as reducer
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import probe_served_models

DELETED = "deleted_backend"
UNREACHABLE = "unreachable"
BINDABLE = "bindable"


def is_private(url):
    host = urlsplit(url).hostname or ""
    if not host:
        return False
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        try:
            addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
        except OSError:
            return False
    if not addresses:
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        if not (address.is_private or address.is_loopback):
            return False
    return True


served_cache = {}


def classify_private(endpoint, model_name):
    if endpoint not in served_cache:
        served_cache[endpoint] = probe_served_models(endpoint, timeout_seconds=10.0)
    served = served_cache[endpoint]
    if not served:
        return UNREACHABLE
    if model_name not in served and model_name.lower() not in {name.lower() for name in served}:
        return "model_not_served"
    return BINDABLE


def classify_backend(backend_id, declarations, loaded):
    if backend_id not in declarations:
        return DELETED
    ref = loaded.get(backend_id)
    if ref is None:
        return "no_endpoint_on_this_machine"
    endpoint = str(ref.endpoint_url)
    if not is_private(endpoint):
        return BINDABLE
    return classify_private(endpoint, ref.model_name)


out = {"unbindable": [], "deleted_control": None, "unreachable_control": None, "judge": None, "collect_error": None}
try:
    shipped = ROUTING_TIERS_PACKAGED_DEFAULT_PATH.resolve()
    if not os.environ.get(ROUTING_TIERS_PATH_ENV_KEY, "").strip():
        os.environ[ROUTING_TIERS_PATH_ENV_KEY] = str(shipped)
    authority = load_task_class_authority()
    public = sorted(authority.public_task_classes)
    contract_yaml = yaml.safe_load(
        (ROUTING_TIERS_PACKAGED_DEFAULT_PATH.parent / "task_class_contracts.v1.yaml").read_text(encoding="utf-8")
    )
    raw_classes = (contract_yaml or {}).get("task_classes") or {}
    contract_public = sorted(
        name for name, entry in raw_classes.items() if isinstance(entry, dict) and entry.get("gateway_exposure") == "public"
    )
    if not public:
        out["unbindable"].append("the task-class authority yielded no customer-visible class")
    if contract_public != public:
        out["unbindable"].append("the authority's public classes " + str(public) + " differ from the shipped contract's " + str(contract_public))
    binding = resolve_bifrost_path_binding()
    merged = load_bifrost_delegation_config(config_path=binding.contract_path, overlay_path=binding.overlay_path)
    declarations = {b.backend_id: b for b in merged.backends if b.backend_id}
    loaded = reducer._load_bifrost_endpoints()
    config = reducer._get_config()
    contract = reducer._get_task_class_contract()
    tier_by_name = {tier.name: tier for tier in config.tiers}
    classified = {}
    for task_class in public:
        entry = reducer._task_class_entry(contract, task_class)
        policy = (entry or {}).get("escalation_policy")
        declared_order = policy.get("tier_order") if isinstance(policy, dict) else None
        names = list(declared_order) if isinstance(declared_order, list) and declared_order else [t.name for t in reducer._tier_order_from_contract(config, entry)]
        override = reducer._get_contract_model_ref(task_class, contract=contract)
        explicit = reducer._is_explicit_task_model_override(task_class, contract)
        if not names:
            out["unbindable"].append(task_class + ": an empty tier order")
            continue
        if names[0] != "local":
            out["unbindable"].append(task_class + ": no_local_first_rung, the tier order starts at " + str(names[0]))
        for name in names:
            tier = tier_by_name.get(name)
            if tier is None:
                out["unbindable"].append(task_class + "/" + str(name) + ": tier_undeclared")
                continue
            candidates = [m.backend_ref for m in tier.models if task_class in m.use_for]
            if explicit and override is not None:
                candidates += [m.backend_ref for m in tier.models if m.id == override]
            if not candidates:
                out["unbindable"].append(task_class + "/" + str(name) + ": tier_serves_class_with_no_backend")
                continue
            for backend_id in sorted(set(candidates)):
                if backend_id not in classified:
                    classified[backend_id] = classify_backend(backend_id, declarations, loaded)
                if classified[backend_id] != BINDABLE:
                    out["unbindable"].append(task_class + "/" + str(name) + ": backend " + backend_id + " is " + classified[backend_id])
    out["deleted_control"] = classify_backend("a-backend-declared-nowhere", declarations, loaded)
    held = socket.socket()
    held.bind((str(ipaddress.IPv4Address(socket.INADDR_LOOPBACK)), 0))
    closed_endpoint = "http://" + held.getsockname()[0] + ":" + str(held.getsockname()[1]) + "/v1/chat/completions"
    held.close()
    out["unreachable_control"] = classify_private(closed_endpoint, "a-model-served-nowhere")
    try:
        resolved = judge_module.RoutingResolvedJudgeInferenceAdapter()._resolve_backend()
        endpoint = str(resolved.endpoint_ref)
        private = is_private(endpoint)
        out["judge"] = {
            "endpoint_private": private,
            "requires_credential": bool(getattr(resolved, "secret_ref", None)),
            "serves_model": private and classify_private(endpoint, str(resolved.model_id)) == BINDABLE,
        }
    except Exception as exc:
        out["judge"] = {"error": type(exc).__name__ + ": " + str(exc)[:400]}
except Exception as exc:
    out["collect_error"] = type(exc).__name__ + ": " + str(exc)[:600]
print(json.dumps(out))
"""

_SKIP_DIRS: Final = frozenset(
    {
        "site-packages",
        "node_modules",
        ".cache",
        "hostedtoolcache",
        ".git",
        "proc",
        "sys",
    }
)
_NAME_RE: Final = re.compile(r'^\s*name\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_SOURCE_NAMES: Final = frozenset(
    name for dist in ONEX_DISTRIBUTIONS for name in (dist, dist.replace("_", "-"))
)


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


class HandlerSubprocessCommand:
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


def read_metrics(base_url: str) -> str:
    """The model server's Prometheus exposition."""
    with urllib.request.urlopen(
        f"{base_url}/metrics", timeout=METRICS_TIMEOUT_S
    ) as response:
        return str(response.read().decode("utf-8", errors="replace"))


def _responder_address(base_url: str | None) -> tuple[str, int]:
    """The host and port of a local responder: an IP literal on loopback or a private address, never a public one."""
    parts = urlsplit(base_url or "")
    try:
        address = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        address = None
    if address is None or parts.port is None:
        raise CollectorAbsentError(
            "responder_address",
            f"{base_url!r} is not http://<ip literal>:<port>; a customer-local model is declared by address so the run needs no name lookup",
        )
    if not (address.is_loopback or (address.is_private and not address.is_global)):
        raise CollectorAbsentError(
            "responder_address",
            f"{base_url!r} is neither loopback nor a private address; a model reached over the public internet is a provider",
        )
    return str(address), parts.port


def find_source_trees(roots: Sequence[Path], max_depth: int = 5) -> tuple[str, ...]:
    """Directories under ``roots`` holding a pyproject.toml that declares an OmniNode distribution."""
    found: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for current, dirs, files in os.walk(root, onerror=lambda _e: None):
            depth = len(Path(current).parts) - base_depth
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and depth < max_depth]
            if "pyproject.toml" not in files:
                continue
            try:
                text = (Path(current) / "pyproject.toml").read_text(errors="replace")
            except OSError:
                continue
            match = _NAME_RE.search(text)
            if match and match.group(1) in _SOURCE_NAMES:
                found.add(current)
    return tuple(sorted(found))


def git_ancestor(path: Path) -> str | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


class LocalDelegationCustomerMachine:
    def __init__(
        self,
        *,
        runner: ProtocolCommandRunner | None = None,
        metrics_reader: Callable[[str], str] = read_metrics,
        uv: str | None = None,
        strace: str | None = None,
        host_path: str | None = None,
        host_env: Mapping[str, str] | None = None,
        prompt: str = PROMPT,
        base_dirs: Sequence[Path] | None = None,
    ) -> None:
        self._runner = runner or HandlerSubprocessCommand()
        self._metrics = metrics_reader
        self._uv = uv
        self._strace = strace
        self._host_path = host_path
        self._host_env: Mapping[str, str] = {} if host_env is None else host_env
        self._prompt = prompt
        self._base_dirs = tuple(
            base_dirs
            if base_dirs is not None
            else (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"))
        )
        self._versions: dict[str, str] = {}
        self._routing: ModelRoutingCollection | None = None

    def versions(self) -> dict[str, str]:
        return dict(self._versions)

    def collect_routing(
        self, responder: ModelChainResolution
    ) -> ModelRoutingCollection:
        """The routing walk the delegation run of this same tick took on the machine, before it was removed."""
        if self._routing is None:
            raise CollectorAbsentError(
                "routing_walk", "no routing walk was taken on the machine"
            )
        return self._routing

    def collect_surface(self) -> ModelSurfaceObservation:
        raise CollectorAbsentError("clean_machine", SURFACE_ABSENT)

    def _tool(self, given: str | None, name: str) -> str:
        found = given or shutil.which(name, path=self._host_path)
        if not found:
            raise CollectorAbsentError(
                name,
                f"{name} is not on this host, so the delegation cannot be run or its egress traced",
            )
        return found

    def _clean_base(self) -> Path:
        """The first directory the machine can live in without a git work tree above it."""
        for base in self._base_dirs:
            if base.is_dir() and git_ancestor(base) is None:
                return base
        raise CollectorAbsentError(
            "clean_machine",
            f"every candidate directory {[str(b) for b in self._base_dirs]} is missing or sits inside a git work tree",
        )

    def _clean_env(self, root: Path, uv: str) -> dict[str, str]:
        uv_dir = root / "uv-bin"
        uv_dir.mkdir()
        (uv_dir / "uv").symlink_to(uv)
        home = root / "home"
        home.mkdir()
        env = {k: self._host_env[k] for k in PASSTHROUGH_ENV if k in self._host_env}
        env.update(
            {
                "HOME": str(home),
                "UV_TOOL_DIR": str(root / "tool"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
                "PATH": os.pathsep.join([str(uv_dir), *SYSTEM_PATH]),
                "LANG": "C.UTF-8",
            }
        )
        return env

    def _traced(
        self,
        strace: str,
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        trace_dir: Path,
        timeout_s: float,
    ) -> tuple[CommandResult, str]:
        trace_file = trace_dir / f"{name}.strace"
        result = self._runner.run(
            [
                strace,
                "-f",
                "-qq",
                "-e",
                "trace=connect",
                "-o",
                str(trace_file),
                "--",
                *argv,
            ],
            env=env,
            cwd=cwd,
            timeout_s=timeout_s,
        )
        trace = trace_file.read_text(errors="replace") if trace_file.exists() else ""
        return result, trace

    def _tokens(self, base_url: str) -> int:
        try:
            tokens = tokens_from_metrics(self._metrics(base_url))
        except (OSError, ValueError) as exc:
            raise CollectorAbsentError(
                "model_metrics",
                f"{base_url}/metrics did not answer: {type(exc).__name__}: {exc}",
            ) from exc
        if tokens is None:
            raise CollectorAbsentError(
                "model_metrics",
                f"{base_url} exposes no token counter, so a delegation's zero provider calls cannot be tied to the process that answered",
            )
        return tokens

    def _install_failed(self, rc: int | None) -> ModelDelegateObservation:
        # The product's own install did not complete: every clause that needs it reads FAIL, never an absent instrument.
        return ModelDelegateObservation(rc=-1 if rc is None else rc)

    def run_delegation(
        self, check_id: CheckId, responder: ModelChainResolution
    ) -> ModelDelegateObservation:
        if check_id not in ("C13", "C14"):
            raise CollectorAbsentError("delegation", UNSUPPORTED_CHECK)
        self._routing = None
        model_host, model_port = _responder_address(responder.base_url)
        base_url = str(responder.base_url)
        if not responder.served_model or not responder.window:
            raise CollectorAbsentError(
                "responder_window",
                "the chain chose a responder with no served model or window",
            )
        uv = self._tool(self._uv, "uv")
        strace = self._tool(self._strace, "strace")
        with tempfile.TemporaryDirectory(
            prefix=f"{check_id.lower()}-customer-", dir=self._clean_base()
        ) as tmp:
            return self._observe(
                check_id,
                Path(tmp),
                uv=uv,
                strace=strace,
                base_url=base_url,
                model_host=model_host,
                model_port=model_port,
                served_model=responder.served_model,
                window=responder.window,
            )

    def _driver(
        self,
        python: Path,
        source: str,
        *,
        env: Mapping[str, str],
        cwd: Path,
        name: str,
    ) -> dict[str, Any]:
        """Run a driver under the installed tool's isolated interpreter and read the one JSON document it prints."""
        done = self._runner.run(
            [str(python), "-I", "-c", source],
            env=env,
            cwd=cwd,
            timeout_s=STEP_TIMEOUT_S,
        )
        doc = (
            load_json(done.stdout.strip().splitlines()[-1])
            if done.stdout.strip()
            else None
        )
        if not isinstance(doc, dict):
            raise CollectorAbsentError(
                name,
                f"the driver printed no document (rc {done.rc}): {done.stderr.strip()[-300:]}",
            )
        return doc

    def _routing_walk(
        self, python: Path, *, env: Mapping[str, str], cwd: Path
    ) -> ModelRoutingCollection:
        doc = self._driver(
            python, ROUTING_DRIVER, env=env, cwd=cwd, name="routing_walk"
        )
        if doc.get("collect_error"):
            raise CollectorAbsentError(
                "routing_walk", f"the walk could not run: {doc['collect_error']}"
            )
        judge = doc.get("judge")
        judge = judge if isinstance(judge, dict) and not judge.get("error") else {}
        return ModelRoutingCollection(
            unbindable=tuple(str(f) for f in doc.get("unbindable") or ()),
            undeclared_control_classified_deleted=doc.get("deleted_control")
            == CLASS_DELETED_BACKEND,
            closed_port_control_classified_unreachable=doc.get("unreachable_control")
            == CLASS_UNREACHABLE,
            judge_endpoint_private=bool(judge.get("endpoint_private")),
            judge_needs_credential=bool(judge.get("requires_credential", True)),
            judge_serves_model=bool(judge.get("serves_model")),
        )

    def _observe(
        self,
        check_id: CheckId,
        root: Path,
        *,
        uv: str,
        strace: str,
        base_url: str,
        model_host: str,
        model_port: int,
        served_model: str,
        window: int,
    ) -> ModelDelegateObservation:
        env = self._clean_env(root, uv)
        if shutil.which("onex", path=env["PATH"]):
            raise CollectorAbsentError(
                "clean_machine", "an onex already resolves on the clean PATH"
            )
        install = self._runner.run(
            [
                uv,
                "tool",
                "install",
                TOOL_PACKAGES[0],
                *[a for p in TOOL_PACKAGES[1:] for a in ("--with", p)],
            ],
            env=env,
            cwd=root,
            timeout_s=INSTALL_TIMEOUT_S,
        )
        bin_dir = root / "bin"
        onex = bin_dir / "onex"
        if install.rc != 0 or not onex.exists():
            return self._install_failed(install.rc if install.rc != 0 else 3)
        # The customer's environment holds only the install's bin directory first on PATH and the system directories.
        customer_env = {
            **{k: v for k, v in env.items() if k in PASSTHROUGH_ENV},
            "HOME": env["HOME"],
            "PATH": os.pathsep.join([str(bin_dir), *SYSTEM_PATH]),
            "LANG": "C.UTF-8",
            "TERM": "dumb",
        }
        workdir = root / "work"
        trace_dir = root / "traces"
        workdir.mkdir()
        trace_dir.mkdir()
        python = root / "tool" / TOOL_PACKAGES[0] / "bin" / "python"
        home = Path(env["HOME"])
        steps: dict[str, tuple[CommandResult, str]] = {}

        def step(name: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
            steps[name] = self._traced(
                strace,
                name,
                argv,
                env=customer_env,
                cwd=workdir,
                trace_dir=trace_dir,
                timeout_s=timeout_s,
            )
            return steps[name][0]

        step("init", [str(onex), "local", "init", "--json"], SHORT_TIMEOUT_S)
        step(
            "unconfigured",
            [str(onex), "delegate", self._prompt, "--json"],
            STEP_TIMEOUT_S,
        )
        overlay_path = home / OVERLAY_RELATIVE_PATH
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        wrote = self._runner.run(
            [
                str(python),
                "-c",
                OVERLAY_DRIVER,
                str(overlay_path),
                f"{base_url}/v1/chat/completions",
                served_model,
                str(window),
                str(MAX_TOKENS),
                ",".join(LOCAL_BACKEND_IDS),
            ],
            env=customer_env,
            cwd=workdir,
            timeout_s=SHORT_TIMEOUT_S,
        )
        if wrote.rc != 0 or not overlay_path.exists():
            raise CollectorAbsentError(
                "overlay_driver",
                f"the customer overlay was not written (rc {wrote.rc}): {wrote.stderr.strip()[-300:]}",
            )
        runs_root = home / ".onex_state" / "runs"
        if runs_root.exists():
            runs_root.rename(runs_root.with_name("runs.unconfigured"))
        tokens_before = self._tokens(base_url)
        configured = step(
            "configured",
            [str(onex), "delegate", self._prompt, "--json"],
            STEP_TIMEOUT_S,
        )
        tokens_after = self._tokens(base_url)
        target = self._driver(
            python,
            TARGET_DRIVER,
            env=customer_env,
            cwd=workdir,
            name="outbound_control",
        )
        control = target.get("target")
        if not isinstance(control, dict):
            raise CollectorAbsentError(
                "outbound_control",
                f"the installed routing contract names no endpoint off the private network to aim the positive control at: {target.get('error')}",
            )
        step(
            "outbound_control",
            [
                "/usr/bin/python3",
                "-c",
                OUTBOUND_DRIVER,
                str(control["host"]),
                str(control["port"]),
            ],
            SHORT_TIMEOUT_S,
        )

        stdout_doc = load_json(configured.stdout) or {}
        run_id = stdout_doc.get("run_id") if isinstance(stdout_doc, dict) else None
        run_dir = runs_root / str(run_id) if run_id else None
        files: dict[str, str | None] = dict.fromkeys(RUN_FILES)
        if run_dir is not None and run_dir.is_dir():
            for name in RUN_FILES:
                path = run_dir / name
                files[name] = (
                    path.read_text(errors="replace") if path.exists() else None
                )
        receipt = load_json(files.get("receipt.json"))
        facts = receipt_facts(receipt)
        self._versions = dict(facts.versions)
        readback = self._runner.run(
            [str(python), "-c", DIRECT_URL_DRIVER % (ONEX_DISTRIBUTIONS,)],
            env=customer_env,
            cwd=workdir,
            timeout_s=SHORT_TIMEOUT_S,
        )
        direct_urls = load_json(readback.stdout)
        trees = find_source_trees([root])
        no_checkout = not no_checkout_findings(
            receipt,
            direct_urls if isinstance(direct_urls, dict) else {},
            trees,
            git_ancestor(workdir),
        )

        def connects(name: str) -> tuple[Connect, ...]:
            return parse_connects(
                steps[name][1], model_host=model_host, model_port=model_port
            )

        session = [
            c for name in ("init", "unconfigured", "configured") for c in connects(name)
        ]
        configured_connects = connects("configured")
        unconfigured = steps["unconfigured"][0]
        if check_id == "C14":
            self._routing = self._routing_walk(python, env=customer_env, cwd=workdir)
        entry = shutil.which("onex", path=customer_env["PATH"])
        runner_entry = shutil.which(
            "onex", path=self._host_path or self._host_env.get("PATH")
        )
        return ModelDelegateObservation(
            rc=-1 if configured.rc is None else configured.rc,
            no_checkout=no_checkout,
            three_files_agree=not three_files_findings(
                files, stdout_doc, receipt, configured.rc
            ),
            receipt_tier=facts.tier,
            receipt_model=facts.model,
            receipt_endpoint=facts.endpoint,
            attempt_tiers=facts.attempt_tiers,
            accepted_last=facts.accepted_last,
            escalated=facts.escalated,
            cost_usd=facts.cost_usd,
            egress_recorded=bool(configured_connects),
            egress_external=tuple(
                c.label() for c in of_kind(session, KIND_EXTERNAL, KIND_NAME_LOOKUP)
            ),
            model_connects=len(of_kind(configured_connects, KIND_MODEL)),
            token_counter_moved=tokens_after - tokens_before >= 1,
            control_external_classified=bool(
                of_kind(connects("outbound_control"), KIND_EXTERNAL)
            ),
            negative_control_refused=unconfigured.rc != 0
            and not of_kind(connects("unconfigured"), KIND_MODEL),
            entry_path=entry,
            entry_is_install_own=entry is not None and Path(entry) == onex,
            runner_entry_path=runner_entry,
        )


__all__ = [
    "CommandResult",
    "HandlerSubprocessCommand",
    "LocalDelegationCustomerMachine",
    "ProtocolCommandRunner",
    "find_source_trees",
    "git_ancestor",
    "read_metrics",
]
