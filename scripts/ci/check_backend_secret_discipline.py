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

# Local backends do not require cloud auth; identified by tier == "local" or an
# endpoint_url_env pointing at a local inference endpoint.
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
    for key in ("secret_ref", "api_key_ref", "api_key_env", "credential_ref"):
        value = backend.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _backend_auth_is_exclusive(backend: dict[str, Any]) -> bool:
    credential = backend.get("credential_ref")
    has_credential = isinstance(credential, str) and credential.strip()
    has_api_key = any(
        isinstance(backend.get(key), str) and backend.get(key, "").strip()
        for key in ("secret_ref", "api_key_ref", "api_key_env")
    )
    return not (has_credential and has_api_key)


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
            else:
                errors.append(f"{rel}: did not parse to a mapping")

    all_violations = literal_violations + backend_violations
    return {
        "ticket": "OMN-12971",
        "gate": "backend-secret-discipline",
        "scanned_configs": list(_SCANNED_CONFIGS),
        "literal_credential_violations": literal_violations,
        "backend_ref_violations": backend_violations,
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
    print(
        "\nFix: keep credential VALUES in the secret store; declare only logical "
        "refs (secret_ref / credential_ref) in routing-authority config."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
