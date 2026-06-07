# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""url-authority check + ratchet — every URL/endpoint resolves from a contract.

Part of OMN-12803 (PR-2, the enforcement gate). Detects URL-hardcoding in
executable source — public HTTPS endpoint literals and ``*_URL`` / ``*_ENDPOINT``
env reads — and applies a ratchet baseline: existing violations are grandfathered
by content fingerprint, but any NEW violation fails the gate locally and in CI.

Spec: ``docs/audits/2026-06-07-demo-path-routing-arch-audit.md`` PART 2 §C.

Scope boundaries (C3/C4):
- API-key / token / secret env reads stay LEGAL — the regex targets ``_URL`` /
  ``_ENDPOINT`` only, never ``_API_KEY`` / ``_TOKEN`` / ``_SECRET``.
- Config-PATH env reads (``*_PATH`` / ``*_CONTRACT_PATH`` / ``*_OVERLAY_PATH``)
  carry ``# contract-config-ok: config`` and are exempt.
- The authority files themselves (``catalog.yaml``, ``bifrost_delegation.yaml``)
  are path-allowlisted — literals there ARE the authority.
- ``# url-authority-ok: <reason>`` on the offending line suppresses one finding.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Detection patterns (C2)
# ---------------------------------------------------------------------------

# 1. Public HTTPS endpoint literal — a quoted https URL to a public host with a
#    dotted TLD. Excludes localhost/loopback (owned by the existing hardcoded-config
#    rule). Connection-target scope only: example/placeholder hosts, VCS permalink
#    URLs (github.com/<repo>/pull|blob|issues — display links, not API endpoints),
#    and $schema / raw-content refs are NOT connection targets (audit class 1K).
_PUBLIC_HTTPS_LITERAL = re.compile(
    r"""["']https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[/"':]|$)""",
    re.IGNORECASE,
)

# Hosts/paths that are NOT connection targets — excluded from the public-https rule
# to match the audit's executable-source scope (no example placeholders, no VCS
# permalinks, no JSON-schema refs).
_NON_ENDPOINT_MARKERS = (
    "example.com",
    "example.org",
    ".invalid",
    "://github.com/",  # display permalinks; api.github.com IS matched (bare host is not)
    "://gitlab.com/",
    "raw.githubusercontent.com",
    "$schema",
    "schemastore.org",
    "json-schema.org",
    "w3.org",
    "spdx.org",
)

# 2. ``*_URL`` / ``*_ENDPOINT`` env read — os.environ[...] subscript or
#    os.environ.get(...) call of a var whose NAME ends in _URL or _ENDPOINT.
#    API-key/token/secret names do not end in _URL/_ENDPOINT, so they are not matched.
_ENV_URL_READ = re.compile(
    r"""os\.environ(?:\.get\(\s*|\[\s*)["'][A-Z0-9_]*(?:_URL|_ENDPOINT)["']""",
)

# 3. ``*_URL`` module-constant assignment from os.environ or an https?:// literal.
_CONST_URL_FROM_ENV = re.compile(
    r"""^[A-Z0-9_]*(?:URL|ENDPOINT)[A-Z0-9_]*\s*=\s*os\.environ""",
)
_CONST_URL_FROM_LITERAL = re.compile(
    r"""^[A-Z0-9_]*(?:URL|ENDPOINT)[A-Z0-9_]*\s*=\s*["']https?://""",
)

# Suppression annotation (same mechanism as onex-allow-internal-ip).
_SUPPRESS_ANNOTATION = "# url-authority-ok:"
# Config-PATH env reads are exempt (already annotated in the codebase).
_CONFIG_PATH_ANNOTATION = "# contract-config-ok:"

# Path-allowlist: the authority files ARE the URL authority; literals are correct.
_AUTHORITY_PATH_SUFFIXES = (
    "configs/bifrost_delegation.yaml",
    "contracts/integrations/catalog.yaml",
)


@dataclass(frozen=True)
class UrlAuthorityViolation:
    """A single url-authority finding with a content fingerprint for the ratchet."""

    repo: str
    path: str
    line: int
    rule: str  # "public-https-literal" | "env-url-read" | "url-const-assignment"
    snippet: str
    fingerprint: str


def _normalize(snippet: str) -> str:
    """Normalize the offending substring for a stable, line-number-independent hash."""
    return re.sub(r"\s+", " ", snippet.strip())


def make_fingerprint(repo: str, path: str, snippet: str) -> str:
    """sha256 over {repo, path, normalized-snippet} — survives unrelated edits above."""
    payload = f"{repo}\0{path}\0{_normalize(snippet)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_authority_path(path: str) -> bool:
    """True when the file IS a URL authority (its literals are canonical)."""
    norm = path.replace("\\", "/")
    return any(norm.endswith(suffix) for suffix in _AUTHORITY_PATH_SUFFIXES)


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return "test" in lowered or "conftest" in lowered


def scan_source(repo: str, path: str, source: str) -> list[UrlAuthorityViolation]:
    """Scan one source file's text for url-authority violations.

    Test files and authority files are skipped. Lines carrying
    ``# url-authority-ok:`` (or, for env reads, ``# contract-config-ok:``) are
    suppressed. Returns at most one violation per line.
    """
    if _is_test_path(path) or is_authority_path(path):
        return []

    violations: list[UrlAuthorityViolation] = []
    for index, raw_line in enumerate(source.splitlines(), start=1):
        stripped = raw_line.strip()
        # Skip blank lines, comment-only lines, and docstring lines (a triple-quoted
        # block holding a URL is documentation, not a connection target — matches the
        # audit's cosmetic-exclusion class 1K).
        if (
            not stripped
            or stripped.startswith("#")
            or stripped.startswith('"""')
            or stripped.startswith("'''")
        ):
            continue
        if _SUPPRESS_ANNOTATION in raw_line:
            continue

        rule = _match_rule(raw_line, stripped)
        if rule is None:
            continue
        # Config-PATH env reads are exempt; they never match _URL/_ENDPOINT anyway,
        # but a contract-config-ok annotation also clears any borderline match.
        if rule == "env-url-read" and _CONFIG_PATH_ANNOTATION in raw_line:
            continue

        snippet = stripped[:200]
        violations.append(
            UrlAuthorityViolation(
                repo=repo,
                path=path,
                line=index,
                rule=rule,
                snippet=snippet,
                fingerprint=make_fingerprint(repo, path, snippet),
            )
        )
    return violations


def _match_rule(raw_line: str, stripped: str) -> str | None:
    """Return the first matching rule id for a line, or None."""
    if _ENV_URL_READ.search(raw_line):
        return "env-url-read"
    if _CONST_URL_FROM_ENV.match(stripped) or _CONST_URL_FROM_LITERAL.match(stripped):
        return "url-const-assignment"
    if _PUBLIC_HTTPS_LITERAL.search(raw_line) and _is_connection_target(raw_line):
        return "public-https-literal"
    return None


def _is_connection_target(raw_line: str) -> bool:
    """True when an https literal is a real connection endpoint, not a placeholder,
    a VCS permalink, a JSON-schema ref, or a value inside a JSON payload literal."""
    lowered = raw_line.lower()
    if any(marker in lowered for marker in _NON_ENDPOINT_MARKERS):
        return False
    # A JSON payload literal line (e.g. a GraphQL response shape) is not a target.
    if stripped_is_json_object(raw_line):
        return False
    return True


def stripped_is_json_object(raw_line: str) -> bool:
    """Heuristic: the line is (part of) a JSON object literal, not an assignment."""
    s = raw_line.strip()
    return s.startswith("{") or s.startswith('{"') or '":{"' in s


# ---------------------------------------------------------------------------
# Ratchet
# ---------------------------------------------------------------------------


def partition_against_baseline(
    violations: list[UrlAuthorityViolation],
    baseline_fingerprints: set[str],
) -> tuple[list[UrlAuthorityViolation], list[UrlAuthorityViolation]]:
    """Split violations into (new, grandfathered) by baseline membership.

    NEW violations (fingerprint absent from the baseline) fail the gate;
    grandfathered ones (present) pass. This is the ratchet's core.
    """
    new: list[UrlAuthorityViolation] = []
    grandfathered: list[UrlAuthorityViolation] = []
    for v in violations:
        if v.fingerprint in baseline_fingerprints:
            grandfathered.append(v)
        else:
            new.append(v)
    return new, grandfathered


def assert_baseline_shrinks_only(before: set[str], after: set[str]) -> None:
    """Anti-gaming: the baseline may shrink (burn-down) but never grow.

    Raises ValueError if ``after`` introduces a fingerprint not in ``before`` —
    you cannot whitelist fresh debt by appending to the baseline.
    """
    added = after - before
    if added:
        raise ValueError(
            "url-authority baseline grew: "
            f"{len(added)} new fingerprint(s) added. The baseline is burn-down only "
            "— fix the violation or annotate it with # url-authority-ok:, never add "
            "it to the baseline. Offending fingerprints: "
            f"{sorted(added)[:5]}"
        )


# ---------------------------------------------------------------------------
# Directory scan + baseline file I/O
# ---------------------------------------------------------------------------

_PY_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "docs",
        "examples",
        "fixtures",
        "migrations",
        "vendored",
        "tests",
    }
)


def scan_tree(repo: str, repo_root: Path) -> list[UrlAuthorityViolation]:
    """Scan all ``*.py`` under ``repo_root`` for url-authority violations.

    Paths in the returned violations are repo-relative so fingerprints are
    machine-independent. Excludes vendored/build/test directories and test files.
    """
    violations: list[UrlAuthorityViolation] = []
    for py in sorted(repo_root.rglob("*.py")):
        if set(py.parts) & _PY_EXCLUDED_PARTS:
            continue
        if _is_test_path(py.name):
            continue
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(py.relative_to(repo_root))
        violations.extend(scan_source(repo, rel, source))
    return violations


def load_baseline(baseline_path: Path) -> set[str]:
    """Load the frozen fingerprint set from the baseline JSON. Missing file = empty."""
    if not baseline_path.exists():
        return set()
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    entries = data.get("violations", []) if isinstance(data, dict) else []
    return {
        str(e["fingerprint"])
        for e in entries
        if isinstance(e, dict) and "fingerprint" in e
    }


def serialize_baseline(violations: list[UrlAuthorityViolation]) -> dict[str, object]:
    """Build the on-disk baseline document — sorted, deterministic, fingerprint-keyed."""
    entries = sorted(
        (
            {
                "repo": v.repo,
                "path": v.path,
                "rule": v.rule,
                "fingerprint": v.fingerprint,
            }
            for v in violations
        ),
        key=lambda e: (e["repo"], e["path"], e["fingerprint"]),
    )
    # Dedup by fingerprint (identical offending content collapses to one entry).
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for e in entries:
        if e["fingerprint"] in seen:
            continue
        seen.add(e["fingerprint"])
        unique.append(e)
    return {"schema_version": "1.0.0", "count": len(unique), "violations": unique}


__all__ = [
    "UrlAuthorityViolation",
    "assert_baseline_shrinks_only",
    "is_authority_path",
    "load_baseline",
    "make_fingerprint",
    "partition_against_baseline",
    "scan_source",
    "scan_tree",
    "serialize_baseline",
]
