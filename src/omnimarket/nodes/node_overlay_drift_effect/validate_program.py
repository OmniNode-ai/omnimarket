# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate overlay documents through the models the running interpreter has installed.

This file is self-contained on purpose: the drift node runs it as a script in the runtime's own
interpreter (``python -I validate_program.py``), which may hold an older omnimarket that does not
carry the drift node. It needs only the standard library, PyYAML and pydantic, and it never
imports the drift node.

Input on stdin, one JSON object: ``{"entries": [{"overlay_id", "model", "text"}]}``, where
``model`` is ``<module>:<class>`` and ``text`` the overlay file's YAML. Optionally ``"src"`` (a
directory holding an omnimarket source tree, put first on the import path, so the models judged
are that tree's) and ``"revision"`` (the commit that tree is, reported as the runtime's revision;
a tree exported from git has no git files to read it from). When ``src`` is given and omnimarket
does not import from it, the program stops with exit 3 rather than judge with other models. Output on stdout, one JSON
object: ``{"runtime": {"version", "revision"}, "results": [{"overlay_id", "verdict", "fields",
"error_class"}]}``. A verdict is ``accepted``, ``rejected`` (the model refused the document;
``fields`` names the refused fields), ``model_missing`` (the module or class is not installed
here) or ``unreadable`` (the text is not YAML). Fields and exception class names are reported,
never a document value, which may be a private deployment fact.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

PACKAGE = "omnimarket"


def _git_dir(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        marker = folder / ".git"
        if marker.is_dir():
            return marker
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                return target if target.is_absolute() else (folder / target).resolve()
    return None


def git_revision(start: Path) -> str:
    """The commit a checkout at or above ``start`` is at, read from git's files; ``unknown`` if none."""
    git_dir = _git_dir(start)
    if git_dir is None:
        return "unknown"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(":", 1)[1].strip()
        common = git_dir
        commondir = git_dir / "commondir"
        if commondir.is_file():
            common = (git_dir / commondir.read_text(encoding="utf-8").strip()).resolve()
        for base in (git_dir, common):
            loose = base / ref
            if loose.is_file():
                return loose.read_text(encoding="utf-8").strip()
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except (OSError, UnicodeError):
        return "unknown"
    return "unknown"


def runtime_identity(
    src: str | None = None, revision: str | None = None
) -> dict[str, str]:
    """The installed omnimarket's version and, for a checkout install, its commit.

    With ``src`` (a tree that was put first on the import path) the version is ``source-tree`` and
    the revision is the one given, since a tree has no git files to read it from.
    """
    if src is not None:
        return {"version": "source-tree", "revision": revision or "unknown"}
    try:
        version = importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        version = "not-installed"
    try:
        module = importlib.import_module(PACKAGE)
        location = Path(module.__file__ or ".").resolve().parent
        revision = git_revision(location)
    except ImportError:
        revision = "unknown"
    return {"version": version, "revision": revision}


def _model(spec: str) -> type[BaseModel] | None:
    module_name, _, class_name = spec.partition(":")
    try:
        found = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError, ValueError):
        return None
    if isinstance(found, type) and issubclass(found, BaseModel):
        return found
    return None


def validate_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One overlay's verdict through its model, the way the node loads it: YAML, then the model."""
    overlay_id = str(entry["overlay_id"])
    model = _model(str(entry["model"]))
    if model is None:
        return {
            "overlay_id": overlay_id,
            "verdict": "model_missing",
            "fields": [],
            "error_class": "ImportError",
        }
    try:
        document = yaml.safe_load(str(entry["text"]))
    except yaml.YAMLError as exc:
        return {
            "overlay_id": overlay_id,
            "verdict": "unreadable",
            "fields": [],
            "error_class": type(exc).__name__,
        }
    try:
        model.model_validate(document)
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in err["loc"]) or "<document>"
                for err in exc.errors(include_input=False, include_context=False)
            }
        )
        return {
            "overlay_id": overlay_id,
            "verdict": "rejected",
            "fields": fields,
            "error_class": "ValidationError",
        }
    return {
        "overlay_id": overlay_id,
        "verdict": "accepted",
        "fields": [],
        "error_class": "",
    }


class TreeNotFirstError(RuntimeError):
    """``src`` was given but omnimarket does not import from it."""


def _use_tree(src: str) -> None:
    sys.path.insert(0, src)
    module = importlib.import_module(PACKAGE)
    location = Path(module.__file__ or ".").resolve()
    if Path(src).resolve() not in location.parents:
        raise TreeNotFirstError(src)


def run(request: dict[str, Any]) -> dict[str, Any]:
    src = request.get("src")
    if src is not None:
        _use_tree(str(src))
    return {
        "runtime": runtime_identity(
            None if src is None else str(src), request.get("revision")
        ),
        "results": [validate_entry(entry) for entry in request["entries"]],
    }


def main() -> int:
    request = json.loads(sys.stdin.read())
    try:
        answer = run(request)
    except TreeNotFirstError:
        return 3
    sys.stdout.write(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
