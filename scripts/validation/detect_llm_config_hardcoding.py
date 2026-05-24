#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Detect hardcoded LLM routing/configuration surfaces.

Phase 1 is report-only by default. Blocking mode is available for changed-file
rollout once the inventory has enough owner decisions.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class DetectorCategory(StrEnum):
    BLOCKING_RUNTIME_VIOLATION = "blocking_runtime_violation"
    ALLOWED_PROVIDER_CATALOG = "allowed_provider_catalog"
    ALLOWED_MIGRATION_FIXTURE = "allowed_migration_fixture"
    ALLOWED_HISTORICAL_EVIDENCE = "allowed_historical_evidence"
    ALLOWED_COMPATIBILITY_ALIAS_TEST = "allowed_compatibility_alias_test"
    GENERATED_RUNTIME_ARTIFACT = "generated_runtime_artifact"


class DetectorSeverity(StrEnum):
    BLOCKING_RUNTIME = "BLOCKING_RUNTIME"
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    HISTORICAL_ONLY = "HISTORICAL_ONLY"
    GENERATED_ARTIFACT = "GENERATED_ARTIFACT"


class DetectorConfidence(StrEnum):
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    MEDIUM_CONFIDENCE = "MEDIUM_CONFIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


@dataclass(frozen=True)
class EscapeHatch:
    reason: str
    ticket: str
    owner: str
    category: str | None = None
    expires: str | None = None
    review_by: str | None = None


@dataclass(frozen=True)
class DetectorFinding:
    path: str
    line: int
    rule_id: str
    category: str
    severity: str
    confidence: str
    message: str
    text: str
    escaped: bool = False
    escape_hatch: dict[str, str | None] | None = None


EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}

SCANNED_SUFFIXES = {
    ".cfg",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

PROVIDER_CONSTRUCTOR_NAMES = {
    "Anthropic",
    "AsyncAnthropic",
    "AsyncOpenAI",
    "AzureOpenAI",
    "OpenAI",
}

MODEL_LITERAL_RE = re.compile(
    r"\b("
    r"claude|codex|deepseek|gemini|glm-[0-9]|gpt-[0-9]|haiku|llama|"
    r"mistral|mixtral|opus|qwen|sonnet"
    r")",
    re.IGNORECASE,
)
ENDPOINT_LITERAL_RE = re.compile(r"(?P<url>(?:https?|cli)://[^\"'\s)>,]+)")
ENDPOINT_KW_RE = re.compile(
    r"\b(api_base|base_url|endpoint|endpoint_url|url)\b.*[\"'](?:https?|cli)://",
    re.IGNORECASE,
)
MODEL_ENV_FALLBACK_RE = re.compile(
    r"os\.(?:environ\.)?get(?:env)?\([^)]*(?:MODEL|LLM|PROVIDER|ENDPOINT)[^)]*,"
    r"\s*[\"'][^\"']+",
    re.IGNORECASE,
)
OR_MODEL_FALLBACK_RE = re.compile(
    r"\bor\s*[\"'][^\"']*(?:claude|codex|deepseek|gemini|glm-|gpt-|llama|qwen)",
    re.IGNORECASE,
)
HIDDEN_RETRY_RE = re.compile(
    r"(?:max_retries|retries|retry_strategy).*(?:os\.(?:environ\.)?get(?:env)?"
    r"\([^)]*,\s*[\"'][0-9]+|=\s*[1-9][0-9]?\b)",
    re.IGNORECASE,
)
POLICY_BYPASS_RE = re.compile(
    r"\bif\b.*\b(provider|model_id|served_model_id)\b.*(?:==|in)\s*"
    r"[\"'](?:anthropic|claude|deepseek|gemini|glm|gpt|openai|qwen)",
    re.IGNORECASE,
)
ESCAPE_MARKER_RE = re.compile(r"#\s*model-routing-ok(?P<body>.*)$")
ESCAPE_KV_RE = re.compile(
    r"(?P<key>[a-zA-Z_]+)=(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>\S+))"
)
TICKET_RE = re.compile(r"^OMN-[0-9]+$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _rule_severity(category: DetectorCategory) -> DetectorSeverity:
    if category == DetectorCategory.BLOCKING_RUNTIME_VIOLATION:
        return DetectorSeverity.BLOCKING_RUNTIME
    if category == DetectorCategory.ALLOWED_HISTORICAL_EVIDENCE:
        return DetectorSeverity.HISTORICAL_ONLY
    if category == DetectorCategory.GENERATED_RUNTIME_ARTIFACT:
        return DetectorSeverity.GENERATED_ARTIFACT
    if category == DetectorCategory.ALLOWED_PROVIDER_CATALOG:
        return DetectorSeverity.HIGH_CONFIDENCE
    return DetectorSeverity.REVIEW_REQUIRED


def classify_path(path: Path) -> DetectorCategory:
    parts = tuple(path.parts)
    path_text = path.as_posix().lower()
    name = path.name.lower()

    if (
        "artifact_manifest" in name
        or "generated" in path_text
        or path.suffix == ".jsonl"
    ):
        return DetectorCategory.GENERATED_RUNTIME_ARTIFACT
    if path_text.startswith(("docs/audits/", "docs/evidence/", "docs/plans/")):
        return DetectorCategory.ALLOWED_HISTORICAL_EVIDENCE
    if "catalog" in path_text or "provider_catalog" in path_text:
        return DetectorCategory.ALLOWED_PROVIDER_CATALOG
    if (
        path_text.startswith("tests/")
        or "/tests/" in path_text
        or "fixtures" in parts
        or "fixture" in path_text
    ):
        if "alias" in path_text or "compat" in path_text:
            return DetectorCategory.ALLOWED_COMPATIBILITY_ALIAS_TEST
        return DetectorCategory.ALLOWED_MIGRATION_FIXTURE
    if path_text.startswith("scripts/"):
        return DetectorCategory.ALLOWED_MIGRATION_FIXTURE
    if path_text.startswith("src/"):
        return DetectorCategory.BLOCKING_RUNTIME_VIOLATION
    return DetectorCategory.ALLOWED_MIGRATION_FIXTURE


def _parse_escape_hatch(line: str) -> tuple[EscapeHatch | None, str | None]:
    marker = ESCAPE_MARKER_RE.search(line)
    if marker is None:
        return None, None

    values: dict[str, str] = {}
    for match in ESCAPE_KV_RE.finditer(marker.group("body")):
        values[match.group("key")] = match.group("quoted") or match.group("bare") or ""

    missing = [key for key in ("reason", "ticket", "owner") if not values.get(key)]
    if missing:
        return None, f"missing required escape hatch field(s): {', '.join(missing)}"
    if not TICKET_RE.match(values["ticket"]):
        return None, "escape hatch ticket must look like OMN-12345"
    category = values.get("category")
    if category is not None and category not in set(DetectorCategory):
        return None, f"escape hatch category is not recognized: {category}"
    for date_key in ("expires", "review_by"):
        if values.get(date_key) and not DATE_RE.match(values[date_key]):
            return None, f"escape hatch {date_key} must use YYYY-MM-DD"

    return EscapeHatch(
        reason=values["reason"],
        ticket=values["ticket"],
        owner=values["owner"],
        category=category,
        expires=values.get("expires"),
        review_by=values.get("review_by"),
    ), None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
        return node.attr
    return None


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ast_findings_for_python(
    path: Path, rel: Path, text: str
) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_name = _call_name(node.func) or ""
        bare_call_name = call_name.rsplit(".", maxsplit=1)[-1]
        if bare_call_name in PROVIDER_CONSTRUCTOR_NAMES:
            findings.append(
                (
                    getattr(node, "lineno", 1),
                    "direct_provider_construction",
                    f"Direct provider client construction ({call_name}) must route through node_model_router.",
                )
            )
        if call_name.endswith("GenerativeModel"):
            findings.append(
                (
                    getattr(node, "lineno", 1),
                    "direct_provider_construction",
                    "Direct GenerativeModel construction must route through node_model_router.",
                )
            )

        for keyword in node.keywords:
            value = _literal_string(keyword.value)
            if (
                keyword.arg
                in {
                    "api_base",
                    "base_url",
                    "endpoint",
                    "endpoint_url",
                    "url",
                }
                and value
                and ENDPOINT_LITERAL_RE.search(value)
            ):
                findings.append(
                    (
                        getattr(keyword.value, "lineno", getattr(node, "lineno", 1)),
                        "endpoint_invention",
                        f"Runtime/business logic may not invent {keyword.arg} transport URLs.",
                    )
                )
            if (
                keyword.arg in {"model", "model_id", "served_model_id"}
                and value
                and MODEL_LITERAL_RE.search(value)
            ):
                findings.append(
                    (
                        getattr(keyword.value, "lineno", getattr(node, "lineno", 1)),
                        "hardcoded_model_identity",
                        "Served model IDs must come from resolved routing policy/topology.",
                    )
                )

    return findings


def _line_rule_matches(line: str) -> list[tuple[str, str, DetectorConfidence]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return []

    matches: list[tuple[str, str, DetectorConfidence]] = []
    if ENDPOINT_KW_RE.search(line):
        matches.append(
            (
                "endpoint_invention",
                "Runtime/business logic may not invent endpoint URLs.",
                DetectorConfidence.HIGH_CONFIDENCE,
            )
        )
    if MODEL_ENV_FALLBACK_RE.search(line) or OR_MODEL_FALLBACK_RE.search(line):
        matches.append(
            (
                "undeclared_routing_fallback",
                "Silent model/provider fallback must be contract-declared and observable.",
                DetectorConfidence.HIGH_CONFIDENCE,
            )
        )
    if HIDDEN_RETRY_RE.search(line):
        matches.append(
            (
                "hidden_retry_logic",
                "Retry behavior must be declared by routing policy, not hidden defaults.",
                DetectorConfidence.MEDIUM_CONFIDENCE,
            )
        )
    if POLICY_BYPASS_RE.search(line):
        matches.append(
            (
                "policy_bypass",
                "Routing must branch on logical roles/policy, not provider or served model names.",
                DetectorConfidence.HIGH_CONFIDENCE,
            )
        )
    if MODEL_LITERAL_RE.search(line):
        matches.append(
            (
                "hardcoded_model_identity",
                "Model identity literals require catalog/evidence classification or migration.",
                DetectorConfidence.MEDIUM_CONFIDENCE,
            )
        )
    return matches


def _finding(
    *,
    rel: Path,
    line_no: int,
    rule_id: str,
    category: DetectorCategory,
    message: str,
    text: str,
    confidence: DetectorConfidence,
    escape_hatch: EscapeHatch | None = None,
) -> DetectorFinding:
    effective_category = category
    if escape_hatch and escape_hatch.category:
        effective_category = DetectorCategory(escape_hatch.category)
    severity = _rule_severity(effective_category)
    return DetectorFinding(
        path=rel.as_posix(),
        line=line_no,
        rule_id=rule_id,
        category=effective_category.value,
        severity=severity.value,
        confidence=confidence.value,
        message=message,
        text=text.strip(),
        escaped=escape_hatch is not None,
        escape_hatch=asdict(escape_hatch) if escape_hatch else None,
    )


def scan_file(path: Path, root: Path) -> list[DetectorFinding]:
    rel = path.relative_to(root)
    category = classify_path(rel)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    findings: list[DetectorFinding] = []
    line_cache = text.splitlines()
    escape_by_line: dict[int, EscapeHatch] = {}
    invalid_escape_lines: dict[int, str] = {}

    for line_no, line in enumerate(line_cache, start=1):
        escape_hatch, error = _parse_escape_hatch(line)
        if escape_hatch is not None:
            escape_by_line[line_no] = escape_hatch
        elif error is not None:
            invalid_escape_lines[line_no] = error

    for line_no, error in invalid_escape_lines.items():
        findings.append(
            DetectorFinding(
                path=rel.as_posix(),
                line=line_no,
                rule_id="invalid_escape_hatch",
                category=DetectorCategory.BLOCKING_RUNTIME_VIOLATION.value,
                severity=DetectorSeverity.BLOCKING_RUNTIME.value,
                confidence=DetectorConfidence.HIGH_CONFIDENCE.value,
                message=error,
                text=line_cache[line_no - 1].strip(),
            )
        )

    ast_hits: dict[tuple[int, str], str] = {}
    if path.suffix == ".py":
        for line_no, rule_id, message in _ast_findings_for_python(path, rel, text):
            ast_hits[(line_no, rule_id)] = message

    for (line_no, rule_id), message in ast_hits.items():
        line_text = line_cache[line_no - 1] if 0 < line_no <= len(line_cache) else ""
        findings.append(
            _finding(
                rel=rel,
                line_no=line_no,
                rule_id=rule_id,
                category=category,
                message=message,
                text=line_text,
                confidence=DetectorConfidence.HIGH_CONFIDENCE,
                escape_hatch=escape_by_line.get(line_no),
            )
        )

    for line_no, line in enumerate(line_cache, start=1):
        seen_rules = {
            rule_id
            for (hit_line, rule_id), _ in ast_hits.items()
            if hit_line == line_no
        }
        for rule_id, message, confidence in _line_rule_matches(line):
            if rule_id in seen_rules:
                continue
            findings.append(
                _finding(
                    rel=rel,
                    line_no=line_no,
                    rule_id=rule_id,
                    category=category,
                    message=message,
                    text=line,
                    confidence=confidence,
                    escape_hatch=escape_by_line.get(line_no),
                )
            )

    return findings


def _is_scannable(path: Path) -> bool:
    if path.suffix not in SCANNED_SUFFIXES:
        return False
    return not any(part in EXCLUDED_DIRS for part in path.parts)


def collect_files(
    root: Path, scope: str, base_ref: str, paths: list[str]
) -> list[Path]:
    if paths:
        candidates = [(root / item).resolve() for item in paths]
        return [path for path in candidates if path.is_file() and _is_scannable(path)]

    if scope == "diff":
        proc = subprocess.run(
            ["git", "diff", "--name-only", "-z", f"{base_ref}...HEAD"],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
        )
        if proc.returncode == 0:
            files: list[Path] = []
            for raw in proc.stdout.split(b"\0"):
                if not raw:
                    continue
                path = root / raw.decode("utf-8")
                if path.is_file() and _is_scannable(path):
                    files.append(path)
            return files

    files = []
    for path in root.rglob("*"):
        if path.is_file() and _is_scannable(path.relative_to(root)):
            files.append(path)
    return sorted(files)


def scan_paths(root: Path, files: list[Path]) -> list[DetectorFinding]:
    findings: list[DetectorFinding] = []
    for path in files:
        findings.extend(scan_file(path, root))
    return findings


def _is_blocking(finding: DetectorFinding) -> bool:
    if finding.escaped:
        return False
    return finding.category == DetectorCategory.BLOCKING_RUNTIME_VIOLATION.value


def _render_text(findings: list[DetectorFinding], mode: str, scope: str) -> str:
    blocking_count = sum(1 for finding in findings if _is_blocking(finding))
    lines = [
        "llm-config-detector: "
        f"mode={mode} scope={scope} findings={len(findings)} blocking={blocking_count}"
    ]
    for finding in findings:
        marker = "escaped" if finding.escaped else "active"
        lines.append(
            f"{finding.path}:{finding.line}: {finding.rule_id} "
            f"[{finding.category} {finding.severity} {finding.confidence} {marker}] "
            f"{finding.message}"
        )
    if mode == "report":
        lines.append("llm-config-detector: report mode - exit 0 regardless of findings")
    return "\n".join(lines)


def _render_json(findings: list[DetectorFinding], mode: str, scope: str) -> str:
    payload: dict[str, Any] = {
        "mode": mode,
        "scope": scope,
        "finding_count": len(findings),
        "blocking_count": sum(1 for finding in findings if _is_blocking(finding)),
        "findings": [asdict(finding) for finding in findings],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan.")
    parser.add_argument(
        "--mode",
        choices=("report", "blocking"),
        default="report",
        help="Report-only exits 0; blocking exits 1 on active runtime violations.",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "diff"),
        default="all",
        help="Scan the full tree or changed files versus --base-ref.",
    )
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Relative file path to scan. May be passed more than once.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).resolve()
    files = collect_files(root, args.scope, args.base_ref, args.path)
    findings = scan_paths(root, files)
    if args.format == "json":
        print(_render_json(findings, args.mode, args.scope))
    else:
        print(_render_text(findings, args.mode, args.scope))

    if args.mode == "blocking" and any(_is_blocking(finding) for finding in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
