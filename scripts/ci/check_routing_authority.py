#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Routing-authority verification gate (OMN-12821, OMN-12877, OMN-12883).

Extended in OMN-12877 (M7.2) to add a RESIDUE AUDIT over four confirmed env-var
residue files, and in OMN-12883 (M7.2) to add a PROVIDER-CLASS ENDPOINT SHAPE
audit over the bifrost delegation config.

This gate proves three things about the routing authority and emits a structured
evidence packet:

POSITIVE route-source proof
    For each demo-path entry, ``provider``, ``model`` (served_model_id),
    ``endpoint_ref``, the resolved ``endpoint`` URL, and ``route_source`` all
    come from the contract / overlay / routing authority — recorded with the
    source file:line that proves each came from authority.

NEGATIVE audit
    Static AST scan over the exact demo-path source files proves NO demo-path
    code reads env vars for endpoint/provider/model, no hardcoded provider
    literals, and no fallback endpoint strings after route resolution.

RESIDUE audit (OMN-12877)
    Scope-extended ratchet over confirmed residue files that carry known
    env-authority debt. For each file, the gate enforces that the violation
    count never INCREASES beyond the baselined value (the existing debt is
    itemised with ticket refs but not retroactively fixed in this PR). Any NEW
    env-authority violation in these files — i.e. a violation count above the
    baseline — fails the gate.

    Confirmed residue files and their violation baselines:
      src/omnimarket/inference/bridge_config_loader.py
          Baseline: 2 env reads (os.environ.get(url_env) + os.environ.get(model_env)
          inside the _MODEL_KEY_REGISTRY loop — bootstrap config loader).
          Debt ticket: OMN-12877 (migrate to contract routing authority)
      src/omnimarket/cli/cli_ab_compare_suite.py
          Baseline: 2 env reads (LLM_GLM_URL:344 + LLM_GLM_MODEL_NAME:346).
          Debt ticket: OMN-12877 (migrate to contract routing authority)
      src/omnimarket/model_policy.yaml
          Baseline: 6 env_var declarations (coder, coder_fast, judge, delegation,
          delegation_review, embedding policies).
          Debt ticket: OMN-12877 (supersede with bifrost routing authority)
      omnibase_infra:service_llm_endpoint_health.py:237-238
          CROSS-REPO (omnibase_infra, not scanned here). os.getenv calls in
          the docstring Usage:: example.
          Debt ticket: OMN-12877 (omnibase_infra remediation, separate PR)

PROVIDER-CLASS ENDPOINT SHAPE audit (OMN-12883)
    Static scan over the committed bifrost_delegation.yaml proves every backend
    respects the provider-class endpoint URL shape contract:
      * If a backend declares ``endpoint_url_env``, ``endpoint_url`` MUST be null.
        The overlay is responsible for supplying the complete URL at runtime.
      * If a backend does NOT declare ``endpoint_url_env`` (i.e. the URL is static),
        ``endpoint_url`` MUST be a non-null non-empty complete URL (full chat
        path verbatim — no in-code append). An empty string is a
        misconfiguration that fails closed.
    Special backends:
      * ``local``: always overlay-supplied (endpoint_url_env required,
        endpoint_url must be null).
      * CLI backends: CLI-invoked agents, not HTTP backends; endpoint_url may
        be a ``cli://`` URI or empty for legacy excluded agent tiers.
    The rule is: overlay-supplied (endpoint_url_env set) → endpoint_url null;
    static-URL (no endpoint_url_env) + non-CLI backend → endpoint_url complete.

The gate FAILS (exit 1) when:
    * a demo-path routing field cannot be resolved from authority, OR
    * the negative audit finds an env read / provider literal / fallback URL on
      a demo-path source file, OR
    * a residue-file violation count exceeds its baselined value (new violation
      introduced), OR
    * a bifrost backend violates the provider-class endpoint URL shape contract.

Acceptance (plan §A1.5): "evidence packet records {provider, model,
endpoint_ref, endpoint, route_source} for the demo path, with the source
file/line proving each came from authority, PLUS the negative-audit result. Any
hardcoded/env path on the demo path fails this gate."

Usage:
    uv run python scripts/ci/check_routing_authority.py            # enforce (exit 1 on fail)
    uv run python scripts/ci/check_routing_authority.py --json     # print evidence packet JSON
    uv run python scripts/ci/check_routing_authority.py --emit <path>  # write packet to <path>
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
# "contract-config-ok" is honoured for bootstrap config loaders that read
# from env vars by design (e.g. OPENROUTER_BASE_URL in bridge_config_loader.py
# where the overlay-vs-env distinction is explicit in the surrounding comment).
_SKIP_TOKENS: tuple[str, ...] = (
    "ONEX_FLAG_EXEMPT",
    "ONEX_EXCLUDE",
    "contract-config-ok",
)

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
    "endpoint_url_env",
)

# ---------------------------------------------------------------------------
# RESIDUE audit constants (OMN-12877)
#
# Each entry: (file_rel, baseline_count, debt_ticket, description)
#   file_rel        — path relative to repo root
#   baseline_count  — maximum allowed violation count (pre-existing debt)
#   debt_ticket     — Linear ticket owning the remediation
#   description     — human-readable description of the debt
#
# The gate FAILS when actual_count > baseline_count (new violation added).
# The gate PASSES when actual_count <= baseline_count (no regression).
# ---------------------------------------------------------------------------

_RESIDUE_SOURCES: tuple[tuple[str, int, str, str], ...] = (
    (
        "src/omnimarket/inference/bridge_config_loader.py",
        2,  # os.environ.get(url_env) + os.environ.get(model_env) in loop body
        "OMN-12877",
        "bootstrap config loader reads LLM_*_URL and LLM_*_MODEL_NAME from env vars",
    ),
    (
        "src/omnimarket/cli/cli_ab_compare_suite.py",
        2,  # LLM_GLM_URL:344 + LLM_GLM_MODEL_NAME:346
        "OMN-12877",
        "CLI A/B compare suite reads LLM_GLM_URL and LLM_GLM_MODEL_NAME directly",
    ),
)

# YAML residue file: model_policy.yaml declares env_var fields that reference
# LLM_*_URL endpoint env vars. These are superseded by the bifrost routing
# authority but remain as a legacy config layer.
_RESIDUE_YAML_POLICIES: tuple[tuple[str, int, str, str], ...] = (
    (
        "src/omnimarket/model_policy.yaml",
        6,  # coder, coder_fast, judge, delegation, delegation_review, embedding
        "OMN-12877",
        "model_policy.yaml carries 6 env_var declarations superseded by bifrost authority",
    ),
)

# Cross-repo debt note (omnibase_infra — not scanned here).
# Documented for audit completeness; remediation tracked in OMN-12877.
_CROSS_REPO_DEBT: tuple[tuple[str, str, str, str], ...] = (
    (
        "omnibase_infra",
        "src/omnibase_infra/services/service_llm_endpoint_health.py:237-238",
        "OMN-12877",
        "docstring Usage:: example uses os.getenv('LLM_CODER_URL'); "
        "remediation is a separate omnibase_infra PR",
    ),
)

# ---------------------------------------------------------------------------
# PROVIDER-CLASS ENDPOINT SHAPE constants (OMN-12883)
# ---------------------------------------------------------------------------

_BIFROST_CONFIG_REL = "src/omnimarket/configs/bifrost_delegation.yaml"

# Legacy excluded agent tier whose endpoints are CLI-invoked agents; empty
# endpoint_url is still allowed for these until every backend declares cli://.
_CLI_AGENT_TIERS: frozenset[str] = frozenset({"cli_agents"})
_CLI_URL_PREFIX = "cli://"


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
    config_path = repo_root / _BIFROST_CONFIG_REL
    if not config_path.exists():
        return None, None, f"{_BIFROST_CONFIG_REL}: NOT FOUND"

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
            f"{_BIFROST_CONFIG_REL}: backend_id={endpoint_ref!r} (overlay-merged at runtime)",
        )
    return (
        None,
        None,
        f"{_BIFROST_CONFIG_REL}: backend_id={endpoint_ref!r} NOT DECLARED",
    )


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
            # Fail closed: endpoint_ref MUST map to a declared bifrost backend in
            # the routing authority. A missing mapping ("NOT FOUND"/"NOT DECLARED")
            # is a broken demo path, not a PASS — otherwise positive_ok could be
            # true with no authority backing the endpoint (false PASS evidence).
            if "NOT FOUND" in endpoint_source or "NOT DECLARED" in endpoint_source:
                errors.append(
                    f"{contract_rel}: endpoint_ref={endpoint_ref!r} does not map to a "
                    f"declared bifrost backend in the routing authority "
                    f"({endpoint_source}) — the demo path is not authority-backed"
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
# RESIDUE audit (OMN-12877)
# ---------------------------------------------------------------------------


def _count_yaml_env_var_fields(data: dict[str, Any]) -> int:
    """Count ``env_var:`` declarations in the top-level ``policies`` mapping."""
    policies = data.get("policies", {})
    if not isinstance(policies, dict):
        return 0
    count = 0
    for _name, policy in policies.items():
        if isinstance(policy, dict) and policy.get("env_var"):
            count += 1
    return count


def build_residue_audit(repo_root: Path) -> dict[str, Any]:
    """Scope-extended ratchet over confirmed env-authority residue files (OMN-12877).

    For each residue file, counts current violations and compares against the
    baselined count. The gate FAILS if actual_count > baseline_count (new
    violation introduced since the baseline was set). The gate PASSES if
    actual_count <= baseline_count (no regression).

    Cross-repo debt (omnibase_infra) is documented but not scanned.
    """
    file_results: list[dict[str, Any]] = []
    errors: list[str] = []
    new_violations: list[str] = []

    # Python source residue files
    for src_rel, baseline, debt_ticket, debt_desc in _RESIDUE_SOURCES:
        src_path = repo_root / src_rel
        if not src_path.exists():
            errors.append(f"residue source missing: {src_rel}")
            continue

        source = src_path.read_text(encoding="utf-8")
        lines = source.splitlines()
        env_reads = _scan_env_reads(source, lines)
        actual_count = len(env_reads)

        regression = actual_count > baseline
        if regression:
            delta = actual_count - baseline
            new_violations.append(
                f"{src_rel}: {delta} new env-authority violation(s) "
                f"(actual={actual_count}, baseline={baseline}, "
                f"debt-ticket={debt_ticket})"
            )

        file_results.append(
            {
                "source": src_rel,
                "debt_ticket": debt_ticket,
                "debt_description": debt_desc,
                "baseline_count": baseline,
                "actual_count": actual_count,
                "regression": regression,
                "violations": [f"{src_rel}:{ln}: env-read: {d}" for ln, d in env_reads],
            }
        )

    # YAML policy residue file
    for yaml_rel, baseline, debt_ticket, debt_desc in _RESIDUE_YAML_POLICIES:
        yaml_path = repo_root / yaml_rel
        if not yaml_path.exists():
            errors.append(f"residue YAML missing: {yaml_rel}")
            continue

        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            errors.append(f"{yaml_rel}: did not parse to a mapping")
            continue

        actual_count = _count_yaml_env_var_fields(data)
        regression = actual_count > baseline
        if regression:
            delta = actual_count - baseline
            new_violations.append(
                f"{yaml_rel}: {delta} new env_var policy declaration(s) "
                f"(actual={actual_count}, baseline={baseline}, "
                f"debt-ticket={debt_ticket})"
            )

        file_results.append(
            {
                "source": yaml_rel,
                "debt_ticket": debt_ticket,
                "debt_description": debt_desc,
                "baseline_count": baseline,
                "actual_count": actual_count,
                "regression": regression,
                "violations": [],
            }
        )

    # Cross-repo debt documentation (informational only — not a gate failure)
    cross_repo = [
        {
            "repo": repo,
            "location": location,
            "debt_ticket": debt_ticket,
            "description": desc,
        }
        for repo, location, debt_ticket, desc in _CROSS_REPO_DEBT
    ]

    clean = len(new_violations) == 0 and len(errors) == 0
    return {
        "files": file_results,
        "cross_repo_debt": cross_repo,
        "errors": errors,
        "new_violations": new_violations,
        "clean": clean,
    }


# ---------------------------------------------------------------------------
# PROVIDER-CLASS ENDPOINT SHAPE audit (OMN-12883)
# ---------------------------------------------------------------------------


def build_provider_endpoint_shape_audit(repo_root: Path) -> dict[str, Any]:
    """Validate provider-class endpoint URL shape for every bifrost backend.

    Rules (from memory reference_bifrost_bare_base_vs_complete_url):
      * If a backend declares ``endpoint_url_env`` (overlay-supplied endpoint),
        ``endpoint_url`` MUST be null. The overlay supplies the complete URL
        at runtime. A non-null endpoint_url alongside endpoint_url_env means two
        conflicting URL sources are declared — this is a misconfiguration.
      * If a backend does NOT declare ``endpoint_url_env`` (static endpoint):
        - For ``cli_agents`` tier: endpoint_url may be absent/empty (no HTTP
          call is made; the CLI agent is invoked directly).
        - For all other tiers: ``endpoint_url`` MUST be a non-null, non-empty
          complete URL (full chat path verbatim, e.g.
          ``.../v1/chat/completions``). A bare base (no chat path) is a
          misconfiguration that fails closed at call time.
    """
    config_path = repo_root / _BIFROST_CONFIG_REL
    if not config_path.exists():
        return {
            "config": _BIFROST_CONFIG_REL,
            "backends": [],
            "violations": [f"{_BIFROST_CONFIG_REL}: NOT FOUND"],
            "clean": False,
        }

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {
            "config": _BIFROST_CONFIG_REL,
            "backends": [],
            "violations": [f"{_BIFROST_CONFIG_REL}: did not parse to a mapping"],
            "clean": False,
        }

    backends = data.get("backends", [])
    if not isinstance(backends, list):
        return {
            "config": _BIFROST_CONFIG_REL,
            "backends": [],
            "violations": [f"{_BIFROST_CONFIG_REL}: 'backends' is not a list"],
            "clean": False,
        }

    violations: list[str] = []
    backend_results: list[dict[str, Any]] = []

    for backend in backends:
        if not isinstance(backend, dict):
            violations.append(f"{_BIFROST_CONFIG_REL}: backend entry is not a mapping")
            continue

        backend_id = backend.get("backend_id", "<unnamed>")
        tier = backend.get("tier", "")
        endpoint_url = backend.get("endpoint_url")  # may be None or str
        endpoint_url_env = backend.get("endpoint_url_env")  # may be None or str

        has_endpoint_url_env = bool(endpoint_url_env)
        endpoint_url_text = (
            str(endpoint_url).strip() if endpoint_url is not None else ""
        )
        has_endpoint_url = bool(endpoint_url_text)
        is_cli_backend = (
            str(backend_id).startswith("cli-")
            or endpoint_url_text.startswith(_CLI_URL_PREFIX)
            or tier in _CLI_AGENT_TIERS
        )

        backend_violations: list[str] = []

        if has_endpoint_url_env:
            # Overlay-supplied backend: endpoint_url MUST be null.
            if endpoint_url is not None:
                backend_violations.append(
                    f"backend_id={backend_id!r}: endpoint_url_env={endpoint_url_env!r} is set "
                    f"(overlay-supplied) but endpoint_url={endpoint_url!r} is non-null — "
                    "only one URL source is allowed; set endpoint_url to null"
                )
        else:
            # Static-URL backend.
            if is_cli_backend:
                # CLI agents: no outbound HTTP call; endpoint_url may be
                # empty for legacy cli_agents or a cli:// provider URI for
                # routable OAuth-backed terminal delegation.
                pass
            else:
                # All other tiers: endpoint_url MUST be a non-null, non-empty
                # complete URL (full chat path verbatim).
                if not has_endpoint_url:
                    backend_violations.append(
                        f"backend_id={backend_id!r} tier={tier!r}: "
                        f"endpoint_url is absent/empty but endpoint_url_env is not set — "
                        "a non-local backend with no endpoint_url_env must carry a complete "
                        "static endpoint_url (full chat path verbatim, e.g. "
                        ".../v1/chat/completions)"
                    )

        violations.extend(backend_violations)
        backend_results.append(
            {
                "backend_id": backend_id,
                "tier": tier,
                "endpoint_url": endpoint_url,
                "endpoint_url_env": endpoint_url_env,
                "url_source": "overlay" if has_endpoint_url_env else "static",
                "violations": backend_violations,
                "compliant": len(backend_violations) == 0,
            }
        )

    return {
        "config": _BIFROST_CONFIG_REL,
        "backends": backend_results,
        "violations": violations,
        "clean": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# Evidence packet + gate
# ---------------------------------------------------------------------------


def build_evidence_packet(repo_root: Path) -> dict[str, Any]:
    positive = build_positive_proof(repo_root)
    negative = build_negative_audit(repo_root)
    residue = build_residue_audit(repo_root)
    shape = build_provider_endpoint_shape_audit(repo_root)

    positive_ok = len(positive["errors"]) == 0 and len(positive["entries"]) > 0
    negative_ok = negative["clean"]
    residue_ok = residue["clean"]
    shape_ok = shape["clean"]
    passed = positive_ok and negative_ok and residue_ok and shape_ok

    return {
        "ticket": "OMN-12821",
        "extension_tickets": ["OMN-12877", "OMN-12883"],
        "gate": "routing-authority-demo-gate",
        "demo_path_contracts": list(_DEMO_PATH_CONTRACTS),
        "demo_path_sources": list(_DEMO_PATH_SOURCES),
        "positive_proof": positive,
        "negative_audit": negative,
        "residue_audit": residue,
        "provider_endpoint_shape_audit": shape,
        "positive_ok": positive_ok,
        "negative_ok": negative_ok,
        "residue_ok": residue_ok,
        "shape_ok": shape_ok,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Routing-authority verification gate "
            "(OMN-12821, extended OMN-12877, OMN-12883)"
        )
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
        residue_files = len(packet["residue_audit"]["files"])
        shape_backends = len(packet["provider_endpoint_shape_audit"]["backends"])
        print(
            "[routing-authority-gate] PASS — "
            f"{len(entries)} demo-path entry(ies) resolve from authority; "
            f"negative audit clean; "
            f"{residue_files} residue file(s) within baseline; "
            f"{shape_backends} backend(s) shape-compliant."
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
    for err in packet["residue_audit"]["errors"]:
        print(f"  residue-audit error: {err}")
    for v in packet["residue_audit"]["new_violations"]:
        print(f"  residue-audit new violation: {v}")
    for v in packet["provider_endpoint_shape_audit"]["violations"]:
        print(f"  shape-audit violation: {v}")
    print(
        "\nFix: every demo-path routing field must resolve from contract / "
        "overlay / routing authority; no env reads / provider literals / "
        "fallback endpoint strings on the demo path. Residue files must not "
        "exceed their baselined violation counts. Bifrost backends must "
        "respect provider-class endpoint URL shape rules."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
