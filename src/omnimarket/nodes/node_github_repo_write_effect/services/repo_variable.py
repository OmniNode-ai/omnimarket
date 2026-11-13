# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The repository-variable requests of node_github_repo_write_effect.

The GET that reads one variable, the POST that creates it and the PATCH that
updates it, against the /actions/variables endpoints only: repository
variables, never secrets. The value is not built here; repo_variable_set reads
it from the source the overlay declares (``read_variable_source_value``: a
plain text file, or one selected key of a YAML document rendered as compact
JSON), and no path or message carries one. The handler sends the requests;
this module only builds them.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

from omnimarket.github_landing.github_landing_requests import split_repository
from omnimarket.github_landing.model_github_http_exchange import ModelGithubHttpRequest


def _repo_path(repository: str) -> str:
    owner, name = split_repository(repository)
    return f"/repos/{owner}/{name}"


def _variable_path(repository: str, name: str) -> str:
    quoted = urllib.parse.quote(name, safe="")
    return f"{_repo_path(repository)}/actions/variables/{quoted}"


def get_variable_request(repository: str, name: str) -> ModelGithubHttpRequest:
    """GET one repository variable by name."""
    return ModelGithubHttpRequest(method="GET", path=_variable_path(repository, name))


def create_variable_request(
    repository: str, name: str, value: str
) -> ModelGithubHttpRequest:
    """POST a new repository variable."""
    return ModelGithubHttpRequest(
        method="POST",
        path=f"{_repo_path(repository)}/actions/variables",
        body={"name": name, "value": value},
    )


def update_variable_request(
    repository: str, name: str, value: str
) -> ModelGithubHttpRequest:
    """PATCH an existing repository variable."""
    return ModelGithubHttpRequest(
        method="PATCH",
        path=_variable_path(repository, name),
        body={"name": name, "value": value},
    )


def _read_source_text(root: Path, source: str, *, max_chars: int) -> str:
    """Resolve the declared source under ``root`` and return its decoded text.

    The one place the source file is bounded, for both formats: it refuses
    (raises ``ValueError``, with a message that never contains the file
    content) a path that escapes ``root`` after ``resolve()`` — including a
    symlink whose target resolves outside it — a missing or unreadable file,
    non-UTF-8 content, empty content after stripping one trailing newline, or
    content longer than ``max_chars``. Returns the text with one trailing
    newline stripped.
    """
    if source.startswith("/") or ".." in source.split("/"):
        raise ValueError(
            "source path must be a relative POSIX path under the overlay root"
        )
    target = (root / source).resolve()
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise ValueError("source path escapes the overlay root")
    if not target.is_file():
        raise ValueError("source file is missing or unreadable")
    try:
        raw = target.read_bytes()
    except OSError:
        raise ValueError("source file is missing or unreadable") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("source file is not valid UTF-8") from None
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise ValueError("source file is empty")
    if len(text) > max_chars:
        raise ValueError("source file exceeds the maximum variable length")
    return text


def read_variable_source(root: Path, source: str, *, max_chars: int) -> str:
    """Read the declared source file under ``root`` and return its text."""
    return _read_source_text(root, source, max_chars=max_chars)


def _without_nones(value: Any) -> Any:
    """Drop every None value from ``value``'s mappings, recursively."""
    if isinstance(value, dict):
        return {
            key: _without_nones(item) for key, item in value.items() if item is not None
        }
    if isinstance(value, list):
        return [_without_nones(item) for item in value]
    return value


def read_variable_source_value(
    root: Path,
    source: str,
    *,
    fmt: str,
    select: str | None,
    name: str,
    repository: str,
    max_chars: int,
) -> str:
    """Read the declared source under ``root`` and return the variable's value.

    For ``text`` the source file holds the value itself and this is exactly
    ``read_variable_source``. For ``json_compact`` the source is a YAML
    document in the overlays repository that may declare ``variable`` and
    ``repository``; the variable's value is the ``select`` key's value with
    every None dropped from its mappings, rendered as compact JSON.

    Refuses (raises ``ValueError``, with a message that never contains the
    document's content) a source ``_read_source_text`` refuses, a document
    that is not a mapping, one declaring another ``variable`` or
    ``repository`` (case-insensitively), a ``select`` key the document does
    not carry or one holding None or an empty value, or a rendered value
    longer than ``max_chars``.
    """
    text = _read_source_text(root, source, max_chars=max_chars)
    if fmt == "text":
        return text
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("source document is not a mapping") from None
    if not isinstance(document, dict):
        raise ValueError("source document is not a mapping")
    declared_variable = document.get("variable")
    if declared_variable is not None and declared_variable != name:
        raise ValueError("source document declares another variable")
    declared_repository = document.get("repository")
    if (
        declared_repository is not None
        and str(declared_repository).lower() != repository.lower()
    ):
        raise ValueError("source document declares another repository")
    if select is None or select not in document:
        raise ValueError("the select key is not in the source document")
    selected = document[select]
    if selected is None or (isinstance(selected, str | list | dict) and not selected):
        raise ValueError("the select key holds no value in the source document")
    rendered = json.dumps(
        _without_nones(selected), separators=(",", ":"), ensure_ascii=True
    )
    if len(rendered) > max_chars:
        raise ValueError("the rendered value exceeds the maximum variable length")
    return rendered


def variable_digest(value: str) -> str:
    """The sha256 hex digest of ``value``."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
