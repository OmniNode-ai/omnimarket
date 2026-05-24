#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FORBIDDEN_KEYS = {
    "api_base",
    "default_model",
    "model_hf_id",
    "model_id",
    "served_model_id",
}

MODEL_LITERAL_RE = re.compile(
    r"(?i)(?:[\w.-]+/)?(?:qwen|deepseek|llama|mistral|mixtral|codellama)[\w./:+-]*"
    r"|claude-(?:sonnet|opus|haiku|3|4)[\w.-]*"
    r"|gpt-[\w.-]+"
    r"|gemini-[\w.-]+"
    r"|text-embedding-[\w.-]+"
)
LLM_ENDPOINT_RE = re.compile(r"(?i)\bhttps?://[^\s\"']+")
MODEL_ENV_DEFAULT_RE = re.compile(
    r"\b[A-Z0-9_]*MODEL[A-Z0-9_]*\b\s*[:=]\s*['\"]?[^'\"\s]+"
)

IGNORED_KEY_PATH_PARTS = {
    "author",
    "created",
    "description",
    "note",
    "related_tickets",
    "tags",
}
SCHEMA_KEY_PATH_PARTS = {
    "inputs",
    "outputs",
}

APPROVED_CONTRACT_NAMES = {
    "llm_endpoints.yaml",
    "model_registry.yaml",
    "model_registry_v1.yaml",
}


@dataclass(frozen=True)
class BoundaryFinding:
    path: Path
    yaml_path: str
    reason: str
    value: str

    def format(self, root: Path) -> str:
        rel = self.path.relative_to(root).as_posix()
        return f"{rel}::{self.yaml_path}: {self.reason}: {self.value}"


def _is_approved_authority_file(path: Path) -> bool:
    return path.name in APPROVED_CONTRACT_NAMES or path.as_posix().endswith(
        "contracts/llm_endpoints.yaml"
    )


def _iter_contracts(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("contract.yaml")):
        parts = set(path.parts)
        if parts & {".git", ".venv", "node_modules", "__pycache__"}:
            continue
        if "/.claude/worktrees/" in path.as_posix():
            continue
        yield path


def _path_text(parts: tuple[str, ...]) -> str:
    return ".".join(parts) if parts else "<root>"


def _ignored_context(parts: tuple[str, ...]) -> bool:
    return any(part.lower() in IGNORED_KEY_PATH_PARTS for part in parts)


def _schema_context(parts: tuple[str, ...]) -> bool:
    return any(part.lower() in SCHEMA_KEY_PATH_PARTS for part in parts)


def _walk_yaml(
    value: Any, parts: tuple[str, ...] = ()
) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield parts, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_yaml(child, (*parts, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_yaml(child, (*parts, str(index)))


def _find_in_contract(path: Path) -> list[BoundaryFinding]:
    if _is_approved_authority_file(path):
        return []

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - surfaced by caller as finding
        return [
            BoundaryFinding(
                path=path,
                yaml_path="<parse>",
                reason="YAML_PARSE_ERROR",
                value=str(exc),
            )
        ]

    findings: list[BoundaryFinding] = []
    for parts, value in _walk_yaml(raw):
        if not parts:
            continue
        key = parts[-1].lower()
        yaml_path = _path_text(parts)

        if key in FORBIDDEN_KEYS and not _schema_context(parts):
            findings.append(
                BoundaryFinding(
                    path=path,
                    yaml_path=yaml_path,
                    reason="FORBIDDEN_MODEL_OR_ENDPOINT_KEY",
                    value=str(value),
                )
            )
            continue

        if not isinstance(value, str) or _ignored_context(parts):
            continue

        if LLM_ENDPOINT_RE.search(value):
            findings.append(
                BoundaryFinding(
                    path=path,
                    yaml_path=yaml_path,
                    reason="FORBIDDEN_ENDPOINT_LITERAL",
                    value=value,
                )
            )
        elif MODEL_LITERAL_RE.search(value):
            findings.append(
                BoundaryFinding(
                    path=path,
                    yaml_path=yaml_path,
                    reason="FORBIDDEN_MODEL_LITERAL",
                    value=value,
                )
            )
        elif MODEL_ENV_DEFAULT_RE.search(value):
            findings.append(
                BoundaryFinding(
                    path=path,
                    yaml_path=yaml_path,
                    reason="FORBIDDEN_MODEL_ENV_DEFAULT",
                    value=value,
                )
            )

    return findings


def validate_contract_overlay_boundary(root: Path) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    for contract in _iter_contracts(root):
        findings.extend(_find_in_contract(contract))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate node contract.yaml files do not own LLM model or endpoint defaults."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to scan. Defaults to current working directory.",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    findings = validate_contract_overlay_boundary(root)
    if findings:
        print(
            f"contract-overlay-boundary: blocking - {len(findings)} finding(s)",
            file=sys.stderr,
        )
        for finding in findings:
            print(finding.format(root), file=sys.stderr)
        return 1

    print("contract-overlay-boundary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
