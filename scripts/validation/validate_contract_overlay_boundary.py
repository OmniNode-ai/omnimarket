#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
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

# A git hook exports these, and they override ``git -C`` and ``cwd``.
_GIT_LOCATION_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}

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


def _is_scanned_contract(path: Path) -> bool:
    if path.name != "contract.yaml":
        return False
    if set(path.parts) & {".git", ".venv", "node_modules", "__pycache__"}:
        return False
    return "/.claude/worktrees/" not in path.as_posix()


def _iter_contracts(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("contract.yaml")):
        if _is_scanned_contract(path):
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


def validate_contract_overlay_boundary(
    root: Path, paths: Iterable[Path] | None = None
) -> list[BoundaryFinding]:
    """Scan every node contract under ``root``, or only the contracts in ``paths``.

    A path that is not an existing ``contract.yaml`` is skipped, so a caller can hand
    over a whole changed-file list.
    """
    if paths is None:
        contracts: Iterable[Path] = _iter_contracts(root)
    else:
        contracts = sorted(
            {p.resolve() for p in paths if p.is_file() and _is_scanned_contract(p)}
        )
    findings: list[BoundaryFinding] = []
    for contract in contracts:
        findings.extend(_find_in_contract(contract))
    return findings


def changed_contracts(root: Path, ref: str) -> list[Path]:
    """The contracts added, copied, modified or renamed between ``ref`` and HEAD.

    Raises ``RuntimeError`` when git cannot resolve ``ref``.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"{ref}...HEAD",
            "--",
            "*contract.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV},
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff against {ref!r} failed: {result.stderr.strip()}")
    return [root / line for line in result.stdout.splitlines() if line]


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
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        help="Scan only contracts changed between REF and HEAD (git merge-base diff).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Scan only these contract.yaml files (pre-commit passes the staged ones).",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    scoped: list[Path] | None = None
    if args.changed_since:
        try:
            scoped = changed_contracts(root, args.changed_since)
        except RuntimeError as exc:
            print(f"contract-overlay-boundary: {exc}", file=sys.stderr)
            return 2
    if args.paths:
        scoped = [*(scoped or []), *args.paths]

    findings = validate_contract_overlay_boundary(root, scoped)
    if findings:
        print(
            f"contract-overlay-boundary: blocking - {len(findings)} finding(s)",
            file=sys.stderr,
        )
        for finding in findings:
            print(finding.format(root), file=sys.stderr)
        return 1

    print(
        "contract-overlay-boundary: PASS"
        if scoped is None
        else f"contract-overlay-boundary: PASS ({len(scoped)} contract path(s) scoped)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
