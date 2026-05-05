"""Audit raw env-var usage across omnimarket production source.

OMN-10547. Walks `omnimarket/src/omnimarket/**/*.py`, ast-parses for
`os.environ.get(...)`, `os.environ[...]`, `os.getenv(...)` and emits CSV at
`omnimarket/docs/audits/2026-05-05-raw-env-usage.csv`.

Run from repo root:
    python scripts/audit/raw_env_usage_audit.py

Exit 0 always; CSV is the deliverable.
"""

from __future__ import annotations

import argparse
import ast
import csv
import sys
from pathlib import Path


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


def _context(source_lines: list[str], lineno: int) -> str:
    if 1 <= lineno <= len(source_lines):
        return source_lines[lineno - 1].strip()
    return ""


def _scan_file(path: Path, repo_root: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    lines = text.splitlines()
    rel = path.relative_to(repo_root).as_posix()
    rows: list[dict[str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_os_environ_get(node):
                key_node = node.args[0] if node.args else None
                default_node = node.args[1] if len(node.args) > 1 else None
                # Also accept `default=...` kw
                if default_node is None:
                    for kw in node.keywords:
                        if kw.arg == "default":
                            default_node = kw.value
                            break
                key = _literal_str(key_node) if key_node else None
                if key is not None:
                    key_field = key
                elif key_node is not None:
                    key_field = _literal_repr(key_node)
                else:
                    key_field = ""
                rows.append(
                    {
                        "file": rel,
                        "line": str(node.lineno),
                        "kind": "os.environ.get",
                        "key": key_field,
                        "has_default": "true" if default_node is not None else "false",
                        "default_value": (
                            _literal_repr(default_node)
                            if default_node is not None
                            else ""
                        ),
                        "context": _context(lines, node.lineno),
                    }
                )
            elif _is_os_getenv(node):
                key_node = node.args[0] if node.args else None
                default_node = node.args[1] if len(node.args) > 1 else None
                key = _literal_str(key_node) if key_node else None
                if key is not None:
                    key_field = key
                elif key_node is not None:
                    key_field = _literal_repr(key_node)
                else:
                    key_field = ""
                rows.append(
                    {
                        "file": rel,
                        "line": str(node.lineno),
                        "kind": "os.getenv",
                        "key": key_field,
                        "has_default": "true" if default_node is not None else "false",
                        "default_value": (
                            _literal_repr(default_node)
                            if default_node is not None
                            else ""
                        ),
                        "context": _context(lines, node.lineno),
                    }
                )
        elif isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "environ"
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
            ):
                slice_node = node.slice
                key = _literal_str(slice_node)
                rows.append(
                    {
                        "file": rel,
                        "line": str(node.lineno),
                        "kind": "os.environ[]",
                        "key": key or _literal_repr(slice_node),
                        "has_default": "false",
                        "default_value": "",
                        "context": _context(lines, node.lineno),
                    }
                )
    return rows


def _walk(src_root: Path) -> list[Path]:
    return sorted(src_root.rglob("*.py"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: cwd).",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Source root to scan (default: <repo-root>/src/omnimarket).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output CSV path (default: <repo-root>/docs/audits/2026-05-05-raw-env-usage.csv).",
    )
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()
    src_root: Path = (args.src or (repo_root / "src" / "omnimarket")).resolve()
    out_path: Path = (
        args.out or (repo_root / "docs" / "audits" / "2026-05-05-raw-env-usage.csv")
    ).resolve()

    if not src_root.is_dir():
        print(f"src root not found: {src_root}", file=sys.stderr)
        return 2

    rows: list[dict[str, str]] = []
    for path in _walk(src_root):
        rows.extend(_scan_file(path, repo_root))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "file",
        "line",
        "kind",
        "key",
        "has_default",
        "default_value",
        "context",
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
