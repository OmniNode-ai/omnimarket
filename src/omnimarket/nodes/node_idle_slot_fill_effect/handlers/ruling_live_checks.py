# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read declared ruling invariants afresh each throughput tick, including read failures."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import find_node_overlay

Finding = tuple[str, str, str, str]


def packaged_checks() -> list[dict[str, Any]]:
    """The two standing invariants; no cached reading or cached configuration."""
    data = yaml.safe_load(
        (Path(__file__).parents[1] / "ruling_checks.yaml").read_text(encoding="utf-8")
    )
    return list(data["checks"])


def evaluate_check(check: Mapping[str, Any], reading: Any) -> Finding | None:
    """A mismatch prints MISSING with the observed value, never a guessed OK."""
    kind = check.get("kind")
    matches = False
    if isinstance(reading, Exception):
        live = f"unread: {type(reading).__name__}: {reading}"
    elif kind == "review-voters":
        matches = isinstance(reading, list) and Counter(reading) == Counter(
            check["model_ids"]
        )
        live = json.dumps(reading, sort_keys=True)
    elif kind == "compose-runtime":
        projects: dict[str, list[str]] = defaultdict(list)
        for row in reading:
            projects[row["project"]].append(row["service"])
        runtime = set(check["runtime_services"])
        allowed = runtime | set(check["backing_services"])
        matches = bool(projects) and all(
            sum(s in runtime for s in services) <= check.get("max_runtime", 1)
            and set(services) <= allowed
            for services in projects.values()
        )
        live = json.dumps(dict(projects), sort_keys=True)
    else:
        live = f"unsupported check kind {kind!r}"
    if matches:
        return None
    return (
        "missing",
        f"ruling-drift:{check['ruling']}",
        f"live={live}; expected={json.dumps(dict(check), sort_keys=True)}",
        "restore the ruled state through its sanctioned owner; never weaken a gate",
    )


def _compose_reading() -> list[dict[str, str]]:
    done = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project",
            "--format",
            "{{json .}}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    rows = []
    for line in done.stdout.splitlines():
        container = json.loads(line)
        labels = dict(
            cell.split("=", 1) for cell in container["Labels"].split(",") if "=" in cell
        )
        project, service = (
            labels.get("com.docker.compose.project"),
            labels.get("com.docker.compose.service"),
        )
        if not project or not service:
            raise ValueError("compose container has no project or service label")
        rows.append({"project": project, "service": service})
    return rows


def _served_model(
    voter: Mapping[str, Any],
    data: Mapping[str, Any],
    path: Path,
    env: Mapping[str, str],
) -> str:
    endpoint = voter.get("endpoint_url")
    if not endpoint and voter.get("backend_id"):
        name = str(data.get("backend_overlay") or "")
        if not name or Path(name).name != name:
            raise ValueError("backend_overlay must name one adjacent file")
        backends = yaml.safe_load((path.parent / name).read_text(encoding="utf-8"))
        endpoint = next(
            (
                b.get("endpoint_url")
                for b in backends["backends"]
                if b.get("backend_id") == voter["backend_id"]
            ),
            None,
        )
    if not endpoint:
        raise ValueError("served voter endpoint is unresolved")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
    ):
        raise ValueError("served voter endpoint is invalid")
    base = parsed.path.removesuffix("/chat/completions").rstrip("/")
    models_path = base + "/models" if base.endswith("/v1") else base + "/v1/models"
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, models_path, "", ""))
    headers = {"Accept": "application/json"}
    if (key_name := voter.get("api_key_env")) and (key := env.get(key_name)):
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        document = json.loads(response.read(1_048_577))
    models = [m["id"] for m in document["data"]]
    if len(models) != 1 or not isinstance(models[0], str) or not models[0]:
        raise ValueError("served voter must report exactly one model id")
    return str(models[0])


def _review_reading(env: Mapping[str, str]) -> list[str]:
    """Read the same overlay as the review workflow; never report voter role names as models.

    Served voters are queried for their current model id, just as the review
    gate does. Declared Claude API voters name their model in the live overlay.
    """
    explicit = env.get("REVIEW_VOTERS_OVERLAY")
    path = (
        Path(explicit)
        if explicit
        else find_node_overlay("node_review_voters_overlay_compute", env)
    )
    if path is None:
        raise ValueError(
            "REVIEW_VOTERS_OVERLAY or node_review_voters_overlay_compute overlay is required"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    voters = data["voters"]
    if not isinstance(voters, list) or not voters:
        raise ValueError("review overlay has no voters")
    result = []
    for voter in voters:
        if not isinstance(voter, dict):
            raise ValueError("each review voter must be a mapping")
        if not voter.get("required", False):
            continue
        # Read the declared identity from the overlay's schema fields. The
        # observation does not choose a provider or an inference route.
        model = voter.get("model")
        if not model and voter.get("model_id_source") == "declared":
            model = voter.get("api_model_id")
        if not model:
            try:
                model = _served_model(voter, data, path, env)
            except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
                model = None
        result.append(
            str(model or f"unresolved-served-model:{voter.get('voter_id', 'unknown')}")
        )
    return result


def read_check(check: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Any:
    """The bounded live read. Returned errors are data and are printed every tick."""
    try:
        if check["kind"] == "compose-runtime":
            return _compose_reading()
        if check["kind"] == "review-voters":
            return _review_reading(os.environ if env is None else env)
        raise ValueError(f"unsupported check {check['kind']}")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        yaml.YAMLError,
    ) as exc:
        # Do not echo subprocess stderr or arbitrary YAML values, which may hold secrets.
        if isinstance(exc, subprocess.SubprocessError):
            return RuntimeError(f"live reader {type(exc).__name__}")
        return RuntimeError(
            f"live reader {type(exc).__name__}: {exc}"
            if isinstance(exc, ValueError) and not isinstance(exc, yaml.YAMLError)
            else f"live reader {type(exc).__name__}"
        )
