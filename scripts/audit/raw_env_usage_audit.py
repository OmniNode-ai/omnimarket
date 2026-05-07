"""Audit raw env-var and hardcoded-value usage across omnimarket production source.

OMN-10564. Walks `src/omnimarket/**/*.py` (excluding __pycache__, .venv), finds:
  - os.environ.get(, os.environ[, os.getenv( calls
  - Hardcoded /Users/ or /Volumes/ path strings
  - Hardcoded 192.168. LAN IP strings
  - Hardcoded model ID strings (common lab model patterns)

Emits CSV to docs/audits/2026-05-07-raw-env-usage.csv with columns:
  file, line, pattern_type, key_or_value, default_value, severity

Severity:
  S1 = no fallback (env_var with no default, or raw hardcoded value)
  S2 = has env fallback but default contains a lab-specific value

Run from repo root:
    uv run python scripts/audit/raw_env_usage_audit.py

Exit 0 always; CSV is the deliverable.
"""

from __future__ import annotations

import ast
import csv
import re
import sys
from pathlib import Path

_HARDCODED_PATH_PAT = re.compile(r'["\'](?P<val>/(?:Users|Volumes)/[^"\']{3,})["\']')
_LAN_IP_PAT = re.compile(r'["\'](?P<val>192\.168\.\d+\.\d+(?::\d+)?)["\']')
_MODEL_ID_PAT = re.compile(
    r'["\'](?P<val>(?:cyankiwi|Corianas|Alibaba-NLP|mlx-community|'
    r'Qwen[^"\']{2,}|DeepSeek[^"\']{2,}|gte-Qwen[^"\']{2,})[^"\']*)["\']'
)

_SKIP_DIRS = {"__pycache__", ".venv", ".git"}

_OUTPUT_DATE = "2026-05-07"


def _is_os_environ_get(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return False
    value = func.value
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "environ"
        and isinstance(value.value, ast.Name)
        and value.value.id == "os"
    )


def _is_os_getenv(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _is_os_environ_subscript(node: ast.Subscript) -> bool:
    value = node.value
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "environ"
        and isinstance(value.value, ast.Name)
        and value.value.id == "os"
    )


def _literal_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_repr(node: ast.expr) -> str:
    if isinstance(node, ast.Constant):
        return repr(node.value)
    try:
        return ast.unparse(node)
    except Exception:
        return "<expr>"


def _default_is_lab_specific(default_repr: str) -> bool:
    return bool(
        re.search(r"192\.168\.", default_repr)
        or re.search(r"/Users/|/Volumes/", default_repr)
        or re.search(r"jonah@", default_repr)
        or re.search(r"omni_home", default_repr)
        or re.search(r"/Volumes/PRO-G40", default_repr)
    )


def _scan_ast(path: Path, repo_root: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    rel = path.relative_to(repo_root).as_posix()
    rows: list[dict[str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_os_environ_get(node) or _is_os_getenv(node):
                key_node = node.args[0] if node.args else None
                default_node = node.args[1] if len(node.args) > 1 else None
                if default_node is None:
                    for kw in node.keywords:
                        if kw.arg == "default":
                            default_node = kw.value
                            break
                key = _literal_str(key_node) if key_node else None
                key_field = (
                    key
                    if key is not None
                    else (_literal_repr(key_node) if key_node else "")
                )
                default_repr = (
                    _literal_repr(default_node) if default_node is not None else ""
                )
                has_default = default_node is not None
                severity = (
                    "S2"
                    if (has_default and _default_is_lab_specific(default_repr))
                    else ("S1" if not has_default else "")
                )
                rows.append(
                    {
                        "file": rel,
                        "line": str(node.lineno),
                        "pattern_type": "env_var",
                        "key_or_value": key_field,
                        "default_value": default_repr,
                        "severity": severity,
                    }
                )
        elif isinstance(node, ast.Subscript) and _is_os_environ_subscript(node):
            slice_node = node.slice
            key = _literal_str(slice_node)
            rows.append(
                {
                    "file": rel,
                    "line": str(node.lineno),
                    "pattern_type": "env_var",
                    "key_or_value": key or _literal_repr(slice_node),
                    "default_value": "",
                    "severity": "S1",
                }
            )
    return rows


def _scan_regex(path: Path, repo_root: Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    rel = path.relative_to(repo_root).as_posix()
    rows: list[dict[str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _HARDCODED_PATH_PAT.finditer(line):
            rows.append(
                {
                    "file": rel,
                    "line": str(lineno),
                    "pattern_type": "hardcoded_path",
                    "key_or_value": m.group("val"),
                    "default_value": "",
                    "severity": "S1",
                }
            )
        for m in _LAN_IP_PAT.finditer(line):
            rows.append(
                {
                    "file": rel,
                    "line": str(lineno),
                    "pattern_type": "lan_ip",
                    "key_or_value": m.group("val"),
                    "default_value": "",
                    "severity": "S1",
                }
            )
        for m in _MODEL_ID_PAT.finditer(line):
            rows.append(
                {
                    "file": rel,
                    "line": str(lineno),
                    "pattern_type": "model_id",
                    "key_or_value": m.group("val"),
                    "default_value": "",
                    "severity": "S1",
                }
            )
    return rows


def _walk(src_root: Path) -> list[Path]:
    result: list[Path] = []
    for p in src_root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        result.append(p)
    return sorted(result)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--src", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()
    src_root: Path = (args.src or (repo_root / "src" / "omnimarket")).resolve()
    out_path: Path = (
        args.out
        or (repo_root / "docs" / "audits" / f"{_OUTPUT_DATE}-raw-env-usage.csv")
    ).resolve()

    if not src_root.is_dir():
        print(f"src root not found: {src_root}", file=sys.stderr)
        return 2

    rows: list[dict[str, str]] = []
    for path in _walk(src_root):
        rows.extend(_scan_ast(path, repo_root))
        rows.extend(_scan_regex(path, repo_root))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "file",
        "line",
        "pattern_type",
        "key_or_value",
        "default_value",
        "severity",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"wrote {len(rows)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
