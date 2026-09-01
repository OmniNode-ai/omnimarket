#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Backend secret-ref / credential-ref discipline gate (OMN-12971).

ENFORCEMENT RATCHET, not detection. Wired as a pre-commit hook and a CI gate.

Why this exists
---------------
The Vertex runtime wiring (OMN-12971) added a credential-backed backend
(``cloud-vertex-gemini``) whose ADC credential is a FILE resolved from the
secret store at the effect boundary. The whole point of secret-ref discipline
is that the *credential value never lands in committed config*. This gate makes
that invariant mechanically enforced so a future edit cannot regress it by
pasting a literal key / service-account JSON / bearer token into the routing
authority.

What it checks (over the committed routing-authority config files)
------------------------------------------------------------------
1. NO literal credential value appears anywhere in the scanned config:
   - PEM private-key blocks (``-----BEGIN ... PRIVATE KEY-----``)
   - service-account JSON markers (``"private_key"``, ``"client_email"``)
   - bearer/api-key-shaped literals (``Bearer <...>``, ``sk-...``, ``AIza...``,
     ``ya29.`` OAuth tokens, Google API keys)
2. Every CLOUD backend that requires authentication declares a LOGICAL
   reference (``secret_ref`` / ``api_key_ref`` / ``api_key_env`` for API-key
   backends, or ``credential_ref`` for ADC backends) — not an inline value.
3. ADC and API-key auth are mutually exclusive on a single backend
   (``credential_ref`` must not coexist with ``secret_ref`` / ``api_key_ref`` /
   ``api_key_env``).

The gate FAILS (exit 1) on any violation. There is no warn-only mode: a literal
credential in committed config is never acceptable.

Usage:
    python scripts/ci/check_backend_secret_discipline.py            # enforce
    python scripts/ci/check_backend_secret_discipline.py --json     # JSON report
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT_MARKER = ".git"

# Committed routing-authority config files that must never carry a credential
# value and must declare logical refs for authenticated cloud backends.
_SCANNED_CONFIGS: tuple[str, ...] = (
    "src/omnimarket/configs/bifrost_delegation.yaml",
    "src/omnimarket/configs/routing_tiers.yaml",
    "src/omnimarket/data/model_registry/model_registry_v1.yaml",
)

# OMN-17372: house inference-provider credential variables. Every spelling that
# has ever named one of OmniNode's OWN provider keys, including the retired
# OPEN_ROUTER_API_KEY form -- a name no host defines still reads as configured,
# which is how this class of defect returns.
_HOUSE_INFERENCE_ENV_VARS: frozenset[str] = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPEN_ROUTER_API_KEY",
        "LLM_OPENROUTER_API_KEY",
        "LLM_GLM_API_KEY",
        "ZHIPU_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "VERTEX_ACCESS_TOKEN",
    }
)

# Source tree scanned for in-code house-credential reads. The config gate alone
# cannot see these: the paths removed by OMN-17372 in
# ``inference/bridge_config_loader.py`` were a hardcoded ``env_var_fallback``
# and a bare ``os.environ.get`` in SOURCE, so a config-only rule would have
# reported PASS while the house key still resolved.
_SCANNED_SOURCE_ROOT = "src/omnimarket"

# Matched over the parsed AST, never the raw text: a module that DISCUSSES one
# of these variables in a docstring or comment (this repo's resolver documents
# the retired-alias history at length, and so does the file the removal landed
# in) is not reading it. Scanning source text would make prose a violation and
# push authors toward deleting the explanation of the rule in order to pass the
# rule. Only a real read of the process environment, or a real
# ``env_var_fallback=`` argument, counts.

# Local backends do not require cloud auth; identified by tier == "local" or a
# base_url_env pointing at a local inference endpoint.
_LOCAL_TIERS: frozenset[str] = frozenset({"local"})

# Tiers whose backends route to a provider that requires authentication.
_CLOUD_TIERS: frozenset[str] = frozenset(
    {"cheap_cloud", "cheap_frontier", "frontier_api"}
)

# Backend ids that are dispatched via subprocess (CLI agents) or use OAuth with
# no committed secret (Claude Code OAuth) — no secret/credential ref required.
_NO_SECRET_BACKEND_IDS: frozenset[str] = frozenset(
    {"cli-claude", "cli-opencode", "cloud-sonnet", "cloud-haiku"}
)

# Literal-credential signatures. A match means a real secret value leaked into
# committed config — always a hard failure.
_CREDENTIAL_LITERAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("service-account-private-key", re.compile(r'"private_key"\s*:')),
    ("service-account-client-email", re.compile(r'"client_email"\s*:')),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z._\-]{30,}")),
    ("gcp-oauth-token", re.compile(r"\bya29\.[0-9A-Za-z._\-]{20,}")),
)


def _find_repo_root(start: Path) -> Path:
    candidate = start
    while candidate != candidate.parent:
        if (candidate / _REPO_ROOT_MARKER).exists():
            return candidate
        candidate = candidate.parent
    return start


def _scan_literal_credentials(rel: str, text: str) -> list[str]:
    violations: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in _CREDENTIAL_LITERAL_PATTERNS:
            if pattern.search(line):
                violations.append(
                    f"{rel}:{lineno}: literal credential ({label}) in committed "
                    f"config — credentials must live in the secret store, never "
                    f"in routing-authority config"
                )
    return violations


def _backend_has_logical_ref(backend: dict[str, Any]) -> bool:
    # OMN-17372: ``api_key_env`` is no longer accepted as a logical reference.
    # It never was one -- it named a HOUSE environment variable, which is the
    # opposite of the store indirection ``secret_ref`` provides. A backend
    # carrying only ``api_key_env`` is now unreferenced, not "referenced by an
    # env var", and this gate says so.
    for key in ("secret_ref", "api_key_ref", "credential_ref"):
        value = backend.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _backend_auth_is_exclusive(backend: dict[str, Any]) -> bool:
    credential = backend.get("credential_ref")
    has_credential = isinstance(credential, str) and credential.strip()
    has_api_key = any(
        isinstance(backend.get(key), str) and backend.get(key, "").strip()
        for key in ("secret_ref", "api_key_ref")
    )
    return not (has_credential and has_api_key)


def _scan_api_key_env(rel: str, data: dict[str, Any]) -> list[str]:
    """Reject any ``api_key_env`` key in the delegation backend config.

    OMN-17372. ``api_key_env`` named a house environment variable, so a backend
    carrying one could authenticate on OmniNode's own provider account the
    moment that variable held a value. That is how a keyless customer's
    delegation -- routed to the platform-default ladder by the fail-open
    tenant-overlay miss -- executed on our credential instead of receiving an
    honest refusal.

    OmniNode does not offer inference and there are no keyless customers on the
    cloud: every customer brings their own provider key, resolved per-tenant
    from the managed store, and a customer with none gets a typed refusal. So
    the field is DELETED rather than deprecated -- no renamed variable, no
    dual-read fallback, and deliberately no suppression token. A backend that
    cannot authenticate from ``secret_ref`` alone must fail, not reach for an
    env var.

    Scanned on the RAW mapping so a re-added key is caught even when no code
    reads it any more: config that merely looks configured is exactly how this
    comes back.
    """
    violations: list[str] = []
    backends = data.get("backends", [])
    if not isinstance(backends, list):
        return violations
    for backend in backends:
        if not isinstance(backend, dict):
            continue
        if "api_key_env" not in backend:
            continue
        backend_id = backend.get("backend_id", "<unknown>")
        violations.append(
            f"{rel}: backend {backend_id!r} declares api_key_env. "
            f"The house env-var fallback was DELETED (OMN-17372): OmniNode does "
            f"not offer inference and there are no keyless customers on the "
            f"cloud, so a backend authenticates from its managed-store "
            f"secret_ref or not at all. Do not re-add this field, rename it, or "
            f"reintroduce it as a fallback -- a delegation that lacks a key must "
            f"refuse, not borrow the house account."
        )
    return violations


def _scan_source_for_house_credentials(repo_root: Path) -> list[str]:
    """Reject in-code reads of a house inference-provider credential.

    OMN-17372, the source-tree half of the same rule ``_scan_api_key_env``
    enforces over config. Deleting ``api_key_env`` from
    ``bifrost_delegation.yaml`` closes the DECLARED house fallback; it does
    nothing about an equivalent read written directly in Python, and this repo
    had two of those on the same boundary:

      * ``resolve_api_key(..., env_var_fallback="OPEN_ROUTER_API_KEY")`` --
        ``env_var_fallback`` is serviced by a direct ``os.environ.get`` inside
        the resolver, AFTER the store lookup, so it bypasses the lane secret
        mapping entirely and kept resolving on a deployed lane where that
        mapping is the only sanctioned path;
      * ``os.environ.get("LLM_GLM_API_KEY")`` -- no secret store in the path at
        all, honouring neither the lane mapping nor per-tenant scoping.

    Both would have survived a config-only gate reporting PASS. A credential
    reaches an inference call through its secret ref and the store, or it does
    not reach it.
    """
    violations: list[str] = []
    source_root = repo_root / _SCANNED_SOURCE_ROOT
    if not source_root.is_dir():
        return violations

    for path in sorted(source_root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (OSError, SyntaxError) as exc:
            violations.append(f"{rel}: could not be parsed ({type(exc).__name__})")
            continue

        for node, _name, kind in _house_credential_reads(tree):
            violations.append(
                f"{rel}:{node.lineno}: {kind} names the house inference "
                f"credential. OMN-17372: OmniNode does not offer inference "
                f"and there are no keyless customers on the cloud, so an "
                f"inference credential resolves from its secret_ref through "
                f"the store (per-tenant scoped) or not at all. Do not reach "
                f"for the process environment here -- a delegation with no "
                f"key must refuse, not borrow the house account."
            )
    return violations


def _is_os_environ(node: ast.expr) -> bool:
    """Whether ``node`` is the ``os.environ`` attribute chain."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _string_constant(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _house_credential_reads(tree: ast.AST) -> list[tuple[ast.AST, str, str]]:
    """Yield (node, variable, kind) for every real house-credential read.

    Three shapes, all resolved on the AST so comments and docstrings are
    invisible to the rule:

      * ``os.environ["NAME"]`` -- subscript read;
      * ``os.environ.get("NAME", ...)`` -- call read;
      * ``f(..., env_var_fallback="NAME")`` -- the resolver's env escape hatch,
        which is serviced by a direct ``os.environ.get`` after the store lookup
        and therefore bypasses the lane secret mapping.
    """
    found: list[tuple[ast.AST, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            name = _string_constant(node.slice)
            if name in _HOUSE_INFERENCE_ENV_VARS:
                found.append((node, str(name), "os.environ read"))
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_environ(func.value)
                and node.args
            ):
                name = _string_constant(node.args[0])
                if name in _HOUSE_INFERENCE_ENV_VARS:
                    found.append((node, str(name), "os.environ read"))
            for kw in node.keywords:
                if kw.arg != "env_var_fallback":
                    continue
                name = _string_constant(kw.value)
                if name in _HOUSE_INFERENCE_ENV_VARS:
                    found.append((node, str(name), "env_var_fallback"))
    return found


def _scan_bifrost_backends(rel: str, data: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    backends = data.get("backends", [])
    if not isinstance(backends, list):
        return [f"{rel}: backends must be a list"]
    for backend in backends:
        if not isinstance(backend, dict):
            continue
        backend_id = backend.get("backend_id", "<unknown>")
        tier = backend.get("tier", "")
        if backend_id in _NO_SECRET_BACKEND_IDS:
            continue
        if tier in _LOCAL_TIERS:
            continue
        if tier in _CLOUD_TIERS and not _backend_has_logical_ref(backend):
            violations.append(
                f"{rel}: cloud backend {backend_id!r} (tier={tier!r}) requires a "
                f"logical secret reference (secret_ref/api_key_ref/api_key_env for "
                f"API-key auth, or credential_ref for ADC) — none declared"
            )
        if not _backend_auth_is_exclusive(backend):
            violations.append(
                f"{rel}: backend {backend_id!r} declares both credential_ref (ADC) "
                f"and api-key auth — they are mutually exclusive"
            )
    return violations


def build_report(repo_root: Path) -> dict[str, Any]:
    literal_violations: list[str] = []
    backend_violations: list[str] = []
    api_key_env_violations: list[str] = []
    errors: list[str] = []

    for rel in _SCANNED_CONFIGS:
        path = repo_root / rel
        if not path.exists():
            errors.append(f"scanned config missing: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        literal_violations.extend(_scan_literal_credentials(rel, text))
        if rel.endswith("bifrost_delegation.yaml"):
            parsed = yaml.safe_load(text)
            if isinstance(parsed, dict):
                backend_violations.extend(_scan_bifrost_backends(rel, parsed))
                api_key_env_violations.extend(_scan_api_key_env(rel, parsed))
            else:
                errors.append(f"{rel}: did not parse to a mapping")

    house_source_violations = _scan_source_for_house_credentials(repo_root)

    all_violations = (
        literal_violations
        + backend_violations
        + api_key_env_violations
        + house_source_violations
    )
    return {
        "ticket": "OMN-12971",
        "gate": "backend-secret-discipline",
        "scanned_configs": list(_SCANNED_CONFIGS),
        "literal_credential_violations": literal_violations,
        "backend_ref_violations": backend_violations,
        # OMN-17372: the house env-var fallback ban, reported as its own bucket
        # so a failure names the actual rule rather than reading as generic
        # "missing ref" drift.
        "api_key_env_violations": api_key_env_violations,
        # OMN-17372: the source-tree half of the same rule — an in-code read of
        # a house provider credential, which the config buckets cannot see.
        "house_credential_source_violations": house_source_violations,
        "errors": errors,
        "passed": len(all_violations) == 0 and len(errors) == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backend secret-ref / credential-ref discipline gate (OMN-12971)"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    repo_root = _find_repo_root(Path.cwd())
    report = build_report(repo_root)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["passed"] else 1

    if report["passed"]:
        print(
            "[backend-secret-discipline] PASS — no literal credentials; every "
            "authenticated cloud backend declares a logical secret/credential ref."
        )
        return 0

    print("[backend-secret-discipline] FAIL")
    for err in report["errors"]:
        print(f"  error: {err}")
    for v in report["literal_credential_violations"]:
        print(f"  literal-credential: {v}")
    for v in report["backend_ref_violations"]:
        print(f"  backend-ref: {v}")
    for v in report["api_key_env_violations"]:
        print(f"  api-key-env: {v}")
    for v in report["house_credential_source_violations"]:
        print(f"  house-credential-source: {v}")
    print(
        "\nFix: keep credential VALUES in the secret store; declare only logical "
        "refs (secret_ref / credential_ref) in routing-authority config."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
