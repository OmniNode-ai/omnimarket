#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Routing-authority verification gate for the demo path (OMN-12821, plan A1.5).

This is a DEMO GATE, not a build. It proves two things about the exact demo
path (node generation + delegation inference) and emits a structured evidence
packet:

POSITIVE route-source proof
    For each demo-path entry, ``provider``, ``model`` (served_model_id),
    ``endpoint_ref``, the resolved ``endpoint`` URL, and ``route_source`` all
    come from the contract / overlay / routing authority — recorded with the
    source file:line that proves each came from authority.

NEGATIVE audit
    Static AST scan over the exact demo-path source files proves NO demo-path
    code reads env vars for endpoint/provider/model, no hardcoded provider
    literals, and no fallback endpoint strings after route resolution.

The gate FAILS (exit 1) when:
    * a demo-path routing field cannot be resolved from authority, OR
    * the negative audit finds an env read / provider literal / fallback URL on
      a demo-path source file.

Acceptance (plan §A1.5): "evidence packet records {provider, model,
endpoint_ref, endpoint, route_source} for the demo path, with the source
file/line proving each came from authority, PLUS the negative-audit result. Any
hardcoded/env path on the demo path fails this gate."

Usage:
    python scripts/ci/check_routing_authority.py            # enforce (exit 1 on fail)
    python scripts/ci/check_routing_authority.py --json     # print evidence packet JSON
    python scripts/ci/check_routing_authority.py --emit <path>  # write packet to <path>
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Demo-path definition (authority-of-record for what "the demo path" means).
#
# Each entry names a node whose model_routing is resolved through the routing
# authority for the close-the-loop demo, and the handler source file(s) that
# perform the resolution + call. The negative audit scans exactly these files.
# ---------------------------------------------------------------------------

_REPO_ROOT_MARKER = ".git"

# Demo-path node contracts whose model_routing must resolve from authority.
_DEMO_PATH_CONTRACTS: tuple[str, ...] = (
    "src/omnimarket/nodes/node_generation_consumer/contract.yaml",
)

# Exact demo-path source files subjected to the negative audit. These are the
# files that resolve routing + perform the LLM call on the accepted demo path.
_DEMO_PATH_SOURCES: tuple[str, ...] = (
    "src/omnimarket/nodes/node_generation_consumer/handlers/handler_generation_consumer.py",
    "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers/handler_inference_intent.py",
    "src/omnimarket/adapters/llm/bifrost/config_loader_bifrost_delegation.py",
)

# Required model_routing keys that MUST be present in a demo-path contract and
# MUST resolve from the contract (not a code default / env var).
_REQUIRED_ROUTING_KEYS: tuple[str, ...] = (
    "provider",
    "served_model_id",
    "endpoint_ref",
    "routing_source",
)

# Inline skip tokens honored by the negative audit (same vocabulary as the
# delegation env scanner — a deliberate, reviewed exemption, not a silent one).
_SKIP_TOKENS: tuple[str, ...] = ("ONEX_FLAG_EXEMPT", "ONEX_EXCLUDE")

# Provider literals that must never be hardcoded on a demo path AFTER routing.
# Resolution must come from the contract-declared ``provider``, never a literal.
_PROVIDER_LITERAL_TOKENS: tuple[str, ...] = (
    "generativelanguage.googleapis.com",
    "openrouter.ai",
    "api.openai.com",
    "api.anthropic.com",
)

# Fallback-endpoint env-var names: a demo path must not read an endpoint URL
# from a shared env var. These are the historically-abused shared bases.
_FALLBACK_ENDPOINT_ENV_TOKENS: tuple[str, ...] = (
    "LLM_CODER_URL",
    "LLM_REASONER_URL",
    "LLM_BASE_URL",
    "OPENROUTER_BASE_URL",
    "base_url_env",
)


def _find_repo_root(start: Path) -> Path:
    candidate = start
    while candidate != candidate.parent:
        if (candidate / _REPO_ROOT_MARKER).exists():
            return candidate
        candidate = candidate.parent
    return start


# ---------------------------------------------------------------------------
# POSITIVE route-source proof
# ---------------------------------------------------------------------------


def _find_routing_key_line(contract_text: str, key: str) -> int | None:
    """Return the 1-based line where ``<key>:`` is declared, or None."""
    for idx, line in enumerate(contract_text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            return idx
    return None


def _resolve_endpoint_for_ref(
    repo_root: Path, endpoint_ref: str
) -> tuple[str | None, str | None, str]:
    """Resolve (endpoint_url, api_key_ref, source) for ``endpoint_ref``.

    Reads the canonical bifrost delegation contract (committed) and reports the
    backend's endpoint_url. The source string records where the value came from
    so the proof is auditable. A ``None`` endpoint_url means the committed
    contract leaves it to the overlay (fail-closed at runtime by the resolver).
    """
    config_rel = "src/omnimarket/configs/bifrost_delegation.yaml"
    config_path = repo_root / config_rel
    if not config_path.exists():
        return None, None, f"{config_rel}: NOT FOUND"

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    backends = data.get("backends", []) if isinstance(data, dict) else []
    for backend in backends:
        if not isinstance(backend, dict):
            continue
        if backend.get("backend_id") != endpoint_ref:
            continue
        endpoint_url = backend.get("endpoint_url")
        api_key_ref = backend.get("api_key_env")
        return (
            endpoint_url,
            api_key_ref,
            f"{config_rel}: backend_id={endpoint_ref!r} (overlay-merged at runtime)",
        )
    return None, None, f"{config_rel}: backend_id={endpoint_ref!r} NOT DECLARED"


def build_positive_proof(repo_root: Path) -> dict[str, Any]:
    """Build the positive route-source proof for every demo-path contract."""
    entries: list[dict[str, Any]] = []
    errors: list[str] = []

    for contract_rel in _DEMO_PATH_CONTRACTS:
        contract_path = repo_root / contract_rel
        if not contract_path.exists():
            errors.append(f"demo-path contract missing: {contract_rel}")
            continue

        contract_text = contract_path.read_text(encoding="utf-8")
        data = yaml.safe_load(contract_text)
        if not isinstance(data, dict):
            errors.append(f"{contract_rel}: contract did not parse to a mapping")
            continue

        model_routing = data.get("model_routing")
        if not isinstance(model_routing, dict):
            errors.append(f"{contract_rel}: missing model_routing block")
            continue

        field_sources: dict[str, str] = {}
        resolved_fields: dict[str, Any] = {}
        for key in _REQUIRED_ROUTING_KEYS:
            value = model_routing.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(
                    f"{contract_rel}: model_routing.{key} is absent/blank — "
                    "it must be declared in the contract, not defaulted in code"
                )
                continue
            line = _find_routing_key_line(contract_text, key)
            field_sources[key] = (
                f"{contract_rel}:{line}" if line is not None else contract_rel
            )
            resolved_fields[key] = value

        endpoint_ref = resolved_fields.get("endpoint_ref")
        endpoint_url: str | None = None
        endpoint_source = ""
        if isinstance(endpoint_ref, str) and endpoint_ref:
            endpoint_url, _api_key_ref, endpoint_source = _resolve_endpoint_for_ref(
                repo_root, endpoint_ref
            )

        entries.append(
            {
                "contract": contract_rel,
                "provider": resolved_fields.get("provider"),
                "model": resolved_fields.get("served_model_id"),
                "endpoint_ref": resolved_fields.get("endpoint_ref"),
                "endpoint": endpoint_url,
                "route_source": resolved_fields.get("routing_source"),
                "field_sources": field_sources,
                "endpoint_source": endpoint_source,
            }
        )

    return {"entries": entries, "errors": errors}


# ---------------------------------------------------------------------------
# NEGATIVE audit
# ---------------------------------------------------------------------------


def _has_skip_token(line: str) -> bool:
    return any(token in line for token in _SKIP_TOKENS)


# Env-read argument names that resolve a SECRET VALUE at the effect boundary.
# These are the SANCTIONED contract-native pattern (api_key_ref declared in the
# contract, value resolved from the store at the call boundary —
# feedback_secrets_contract_ref_value_in_store). They are NOT endpoint/provider/
# model reads, so the demo-gate negative audit must NOT flag them.
_API_KEY_REF_HINTS: tuple[str, ...] = ("api_key", "api-key", "_KEY", "_TOKEN")


def _env_arg_repr(node: ast.AST) -> str:
    """Best-effort textual repr of an env-read key/argument for classification."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_api_key_resolution(arg_repr: str) -> bool:
    lowered = arg_repr.lower()
    return any(hint.lower() in lowered for hint in _API_KEY_REF_HINTS)


def _scan_env_reads(source: str, lines: list[str]) -> list[tuple[int, str]]:
    """Return (lineno, detail) for endpoint/provider/model env reads.

    SCOPE (plan §A1.5): the demo-gate negative audit flags os.environ/os.getenv
    reads that feed endpoint / provider / model on the demo path. It does NOT
    flag secret-value resolution (``os.environ[api_key_ref]``) at the effect
    boundary — that IS the sanctioned contract-native pattern (the contract
    declares ``api_key_ref``; the value is resolved from the store at the call
    boundary). Skip-token annotated reads are also exempt.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # lineno -> (detail, arg_repr, end_lineno)
    found: dict[int, tuple[str, str, int]] = {}
    for node in ast.walk(tree):
        lineno: int | None = None
        detail: str | None = None
        arg_repr: str = ""
        end_lineno: int = 0

        # os.environ[X] subscript
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
        ):
            lineno = node.lineno
            detail = "os.environ[...]"
            arg_repr = _env_arg_repr(node.slice)
            end_lineno = getattr(node, "end_lineno", node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            is_environ_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            )
            if is_getenv or is_environ_get:
                lineno = node.lineno
                detail = "os.getenv" if is_getenv else "os.environ.get"
                if node.args:
                    arg_repr = _env_arg_repr(node.args[0])
                end_lineno = getattr(node, "end_lineno", node.lineno)

        if lineno is not None and detail is not None and lineno not in found:
            found[lineno] = (detail, arg_repr, end_lineno)

    violations: list[tuple[int, str]] = []
    for lineno in sorted(found):
        detail, arg_repr, end_lineno = found[lineno]
        span = (
            lines[lineno - 1 : end_lineno]
            if end_lineno > lineno
            else [lines[lineno - 1] if lineno <= len(lines) else ""]
        )
        if any(_has_skip_token(ln) for ln in span):
            continue
        # Sanctioned secret-value resolution at the effect boundary is not a
        # demo-path endpoint/provider/model violation.
        if _is_api_key_resolution(arg_repr):
            continue
        text = (lines[lineno - 1] if lineno <= len(lines) else "").strip()
        violations.append((lineno, f"{detail}({arg_repr!r}) — {text}"))
    return violations


def _iter_string_literals(source: str) -> list[tuple[int, str]]:
    """Return (lineno, value) for every string literal in the AST.

    Docstrings and comments are documentation, not demo-path runtime behavior:
    a docstring that says "NOT from LLM_CODER_URL" must not trip the audit.
    Module/class/function docstrings are the first ``Expr(Constant[str])`` in a
    body; we exclude those, and only inspect non-docstring string literals
    (which carry actual runtime behavior — e.g. a hardcoded provider URL).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_ids.add(id(first.value))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            out.append((node.lineno, node.value))
    return out


def _scan_literal_tokens(
    source: str, lines: list[str], tokens: tuple[str, ...], label: str
) -> list[tuple[int, str]]:
    """Flag tokens that appear inside non-docstring STRING LITERALS only.

    A token mentioned in prose/comments (documenting what is NOT done) is not a
    behavior violation; a token embedded in a live string literal is.
    """
    violations: list[tuple[int, str]] = []
    for lineno, literal in _iter_string_literals(source):
        line_text = lines[lineno - 1] if lineno <= len(lines) else ""
        if _has_skip_token(line_text):
            continue
        for token in tokens:
            if token in literal:
                violations.append((lineno, f"{label}: {token!r} — {line_text.strip()}"))
                break
    return violations


def build_negative_audit(repo_root: Path) -> dict[str, Any]:
    """Static audit over the exact demo-path source files."""
    file_results: list[dict[str, Any]] = []
    errors: list[str] = []

    for src_rel in _DEMO_PATH_SOURCES:
        src_path = repo_root / src_rel
        if not src_path.exists():
            errors.append(f"demo-path source missing: {src_rel}")
            continue

        source = src_path.read_text(encoding="utf-8")
        lines = source.splitlines()

        env_reads = _scan_env_reads(source, lines)
        provider_literals = _scan_literal_tokens(
            source, lines, _PROVIDER_LITERAL_TOKENS, "provider-literal"
        )
        fallback_endpoints = _scan_literal_tokens(
            source, lines, _FALLBACK_ENDPOINT_ENV_TOKENS, "fallback-endpoint-env"
        )

        violations: list[str] = []
        violations.extend(f"{src_rel}:{ln}: env-read: {d}" for ln, d in env_reads)
        violations.extend(f"{src_rel}:{ln}: {d}" for ln, d in provider_literals)
        violations.extend(f"{src_rel}:{ln}: {d}" for ln, d in fallback_endpoints)

        file_results.append(
            {
                "source": src_rel,
                "clean": len(violations) == 0,
                "violations": violations,
            }
        )

    all_violations = [v for fr in file_results for v in fr["violations"]]
    return {
        "files": file_results,
        "errors": errors,
        "clean": len(all_violations) == 0 and len(errors) == 0,
        "violation_count": len(all_violations),
    }


# ---------------------------------------------------------------------------
# Evidence packet + gate
# ---------------------------------------------------------------------------


def build_evidence_packet(repo_root: Path) -> dict[str, Any]:
    positive = build_positive_proof(repo_root)
    negative = build_negative_audit(repo_root)

    positive_ok = len(positive["errors"]) == 0 and len(positive["entries"]) > 0
    negative_ok = negative["clean"]
    passed = positive_ok and negative_ok

    return {
        "ticket": "OMN-12821",
        "gate": "routing-authority-demo-gate",
        "demo_path_contracts": list(_DEMO_PATH_CONTRACTS),
        "demo_path_sources": list(_DEMO_PATH_SOURCES),
        "positive_proof": positive,
        "negative_audit": negative,
        "positive_ok": positive_ok,
        "negative_ok": negative_ok,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Routing-authority verification gate for the demo path (OMN-12821)"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the evidence packet as JSON"
    )
    parser.add_argument(
        "--emit", type=str, default="", help="write the evidence packet JSON to PATH"
    )
    args = parser.parse_args()

    repo_root = _find_repo_root(Path.cwd())
    packet = build_evidence_packet(repo_root)

    if args.emit:
        out = Path(args.emit)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")

    if args.json:
        print(json.dumps(packet, indent=2, sort_keys=True))
        return 0 if packet["passed"] else 1

    if packet["passed"]:
        entries = packet["positive_proof"]["entries"]
        print(
            "[routing-authority-gate] PASS — "
            f"{len(entries)} demo-path entry(ies) resolve from authority; "
            "negative audit clean."
        )
        for entry in entries:
            print(
                f"  {entry['contract']}: provider={entry['provider']!r} "
                f"model={entry['model']!r} endpoint_ref={entry['endpoint_ref']!r} "
                f"route_source={entry['route_source']!r}"
            )
        return 0

    print("[routing-authority-gate] FAIL")
    for err in packet["positive_proof"]["errors"]:
        print(f"  positive-proof error: {err}")
    for err in packet["negative_audit"]["errors"]:
        print(f"  negative-audit error: {err}")
    for fr in packet["negative_audit"]["files"]:
        for v in fr["violations"]:
            print(f"  negative-audit violation: {v}")
    print(
        "\nFix: every demo-path routing field must resolve from contract / "
        "overlay / routing authority; no env reads / provider literals / "
        "fallback endpoint strings on the demo path."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
