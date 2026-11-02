# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the dev-merge tag each first-party consumer pins for omnimarket (OMN-21901).

The Wire Compatibility Gate (``scripts/ci/check_wire_compatibility.py``) grades
a wire change against the baseline this handler returns. Operator ruling
2026-11-01T17:05:10Z: releases are optional and internal consumers run from
dev-merge tags (``dev-<UTC YYYYMMDDHHMMSS>-<sha12>``, cut on every push to dev),
so the baseline is the tag a consumer pins in its own ``pyproject.toml`` and
``uv.lock``, never a release and never a sha parsed out of a lock.

A pin that is not a dev-merge tag is REFUSED, not translated: a commit sha, a
branch, a release tag, a registry source, a lock that disagrees with its
pyproject, a tag that does not resolve, or a tag that resolves to a commit
other than the sha12 its name carries. Every refusal raises :class:`GateError`
with its own outcome token, so a pin that cannot be read never reads as a pass.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import parse_qs

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_wire_consumer_pin import (
    ModelConsumerPinFiles,
    ModelPinBaseline,
    ModelWireConsumerPinRequest,
    ModelWireConsumerPinResult,
)

OUTCOME_PIN_REFUSED = "WIRE_COMPAT_PIN_REFUSED"
OUTCOME_PIN_UNRESOLVABLE = "WIRE_COMPAT_PIN_UNRESOLVABLE"

#: ``dev-<UTC YYYYMMDDHHMMSS>-<sha12>``, the tag dev-merge-tag.yml cuts on every
#: push to dev. The sha12 lets the reader prove a tag still names its own commit.
DEV_MERGE_TAG_RE = re.compile(r"^dev-(?P<stamp>\d{14})-(?P<sha>[0-9a-f]{12})$")


class GateError(Exception):
    """A condition the gate cannot grade, carrying its own outcome token."""

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


def _refuse(detail: str) -> GateError:
    return GateError(OUTCOME_PIN_REFUSED, detail)


def _not_a_dev_tag(value: str) -> str:
    """Name what a refused pin value actually is, for the refusal message."""
    if re.fullmatch(r"[0-9a-f]{40}", value):
        kind = "a commit sha"
    elif re.fullmatch(r"v?\d+(\.\d+)*([.-]?(a|b|rc|dev|post)\d*)?", value):
        kind = "a release tag"
    else:
        kind = "not a dev-merge tag"
    return f"{value!r} is {kind}; a pin must be dev-<UTC YYYYMMDDHHMMSS>-<sha12>"


def _toml(label: str, kind: str, path: str) -> dict[str, object]:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _refuse(
            f"{label}: the {kind} at {path!r} is not readable ({exc}); the "
            "consumer pin is unknown, so wire compatibility is UNPROVEN"
        ) from exc


def _table(data: object, *keys: str) -> dict[str, object]:
    for key in keys:
        data = data.get(key, {}) if isinstance(data, dict) else {}
    return data if isinstance(data, dict) else {}


def pyproject_pin(label: str, path: str) -> str:
    """Return the dev-merge tag ``[tool.uv.sources].omnimarket`` pins."""
    source = _table(_toml(label, "pyproject.toml", path), "tool", "uv", "sources").get(
        "omnimarket"
    )
    if not isinstance(source, dict) or "omnimarket" not in str(source.get("git", "")):
        raise _refuse(
            f"{label}: [tool.uv.sources].omnimarket is not a git source, so the "
            "consumer does not pin a dev-merge tag"
        )
    extra = sorted(key for key in ("rev", "branch") if key in source)
    if extra:
        raise _refuse(
            f"{label}: [tool.uv.sources].omnimarket also sets {extra}; a pin is "
            "exactly one dev-merge tag"
        )
    tag = source.get("tag")
    if not isinstance(tag, str):
        raise _refuse(f"{label}: [tool.uv.sources].omnimarket names no tag")
    if not DEV_MERGE_TAG_RE.fullmatch(tag):
        raise _refuse(f"{label}: {_not_a_dev_tag(tag)}")
    return tag


def lock_pin(label: str, path: str) -> tuple[str, str | None]:
    """Return ``(tag, locked commit or None)`` from the consumer's ``uv.lock``.

    The lock is what actually runs, so it is cross-checked against the
    pyproject. Its commit is never a baseline; it only has to match the tag.
    """
    data = _toml(label, "uv.lock", path)
    entries = data.get("package")
    packages = [
        pkg
        for pkg in (entries if isinstance(entries, list) else [])
        if isinstance(pkg, dict) and pkg.get("name") == "omnimarket"
    ]
    if len(packages) != 1:
        raise _refuse(f"{label}: uv.lock holds {len(packages)} omnimarket packages")
    git_url = _table(packages[0], "source").get("git")
    if not isinstance(git_url, str):
        raise _refuse(
            f"{label}: uv.lock locks omnimarket from a registry or path, not a "
            "dev-merge tag"
        )
    location, _, commit = git_url.partition("#")
    params = parse_qs(location.partition("?")[2])
    if set(params) != {"tag"} or len(params["tag"]) != 1:
        raise _refuse(
            f"{label}: uv.lock locks omnimarket by {sorted(params) or 'no ref'}, "
            "not by one tag"
        )
    tag = params["tag"][0]
    if not DEV_MERGE_TAG_RE.fullmatch(tag):
        raise _refuse(f"{label}: uv.lock: {_not_a_dev_tag(tag)}")
    return tag, (commit or None)


def resolve_pin_commit(root: Path, tag: str) -> str:
    """Return the commit *tag* names, proving it still names the one it was cut for."""
    done = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
        cwd=str(root),
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        check=False,
    )
    commit = os.fsdecode(done.stdout).strip()
    if done.returncode or not commit:
        raise GateError(
            OUTCOME_PIN_UNRESOLVABLE,
            f"git could not resolve pinned tag {tag!r} "
            f"({os.fsdecode(done.stderr).strip()}); the pinned consumer cannot "
            "be resolved, so wire compatibility is UNPROVEN rather than proven",
        )
    match = DEV_MERGE_TAG_RE.fullmatch(tag)
    if match is None or not commit.startswith(match.group("sha")):
        raise GateError(
            OUTCOME_PIN_UNRESOLVABLE,
            f"pinned tag {tag!r} resolves to {commit[:12]}, not the sha its name "
            "carries; a moved tag proves nothing",
        )
    return commit


def consumer_files(
    pyprojects: list[str], locks: list[str]
) -> tuple[ModelConsumerPinFiles, ...]:
    """Pair each ``LABEL=PATH`` pyproject with the lock of the same label."""
    paths: dict[str, dict[str, str]] = {}
    for flag, key, values in (
        ("--consumer-pyproject", "pyproject_path", pyprojects),
        ("--consumer-lock", "lock_path", locks),
    ):
        for value in values:
            label, _, path = value.partition("=")
            if not label or not path or key in paths.setdefault(label, {}):
                raise _refuse(
                    f"{flag} expects one LABEL=PATH per consumer, got {value!r}"
                )
            paths[label][key] = path
    return tuple(
        ModelConsumerPinFiles(label=label, **found) for label, found in paths.items()
    )


class HandlerWireConsumerPin:
    """Resolve every declared consumer's pin into one baseline per distinct tag."""

    def handle(
        self, request: ModelWireConsumerPinRequest
    ) -> ModelWireConsumerPinResult:
        """Return the distinct pinned tags oldest first, or raise :class:`GateError`.

        One bad pin refuses the whole read; the others are never graded in its
        place. A required consumer that was not supplied is itself a refusal.
        """
        supplied = {files.label: files for files in request.consumers}
        pins: dict[str, list[str]] = {}
        commits: dict[str, str] = {}
        for label in sorted(set(supplied) | set(request.required)):
            files = supplied.get(label)
            if files is None or not files.pyproject_path or not files.lock_path:
                raise _refuse(
                    f"{label}: both --consumer-pyproject and --consumer-lock are "
                    "required for every consumer; a missing pin is unproven, "
                    "never compatible"
                )
            tag = pyproject_pin(label, files.pyproject_path)
            locked_tag, locked_commit = lock_pin(label, files.lock_path)
            if locked_tag != tag:
                raise _refuse(
                    f"{label}: pyproject pins {tag!r} but uv.lock locks "
                    f"{locked_tag!r}; what runs is the lock, and the two disagree"
                )
            commits[tag] = resolve_pin_commit(Path(request.repo_root), tag)
            if locked_commit is not None and locked_commit != commits[tag]:
                raise _refuse(
                    f"{label}: uv.lock locks {locked_commit[:12]} for {tag!r}, "
                    f"which the tag resolves to {commits[tag][:12]}"
                )
            pins.setdefault(tag, []).append(label)
        return ModelWireConsumerPinResult(
            baselines=tuple(
                ModelPinBaseline(
                    tag=tag, commit=commits[tag], consumers=tuple(pins[tag])
                )
                for tag in sorted(pins, key=lambda t: t[4:])
            )
        )


def report_pin_verdicts(
    verdicts: list[tuple[ModelPinBaseline, list[str], list[str]]], graded: str
) -> tuple[int, list[tuple[bool, str]]]:
    """Render one verdict per pin, oldest first; return ``(exit code, lines)``.

    Each line is ``(is_error, text)``. Any refusing pin refuses the gate, so
    the oldest pin stays the strict baseline: the newest cannot mask it.
    """
    lines: list[tuple[bool, str]] = []
    refused = 0
    for baseline, findings, notes in verdicts:
        lines += [(False, f"note: {note}") for note in notes]
        who = f"{baseline.tag} (pinned by {', '.join(baseline.consumers)})"
        if findings:
            refused += 1
            lines.append(
                (
                    True,
                    f"pin-verdict REFUSED: {who} cannot decode this tree's wire payloads.",
                )
            )
            lines += [(True, finding) for finding in findings]
        else:
            lines.append(
                (
                    False,
                    f"pin-verdict PASS: {who} decodes every wire payload emitted by {graded}.",
                )
            )
    oldest = verdicts[0][0].tag
    if refused:
        lines.append(
            (
                True,
                f"WIRE_COMPAT_REFUSED: {refused} of {len(verdicts)} pinned consumer "
                f"tag(s) cannot decode this tree's wire payloads (oldest pin {oldest}).\n"
                "\nConsumer-first is the remedy, not a waiver: land a consumer that "
                "accepts the new shape on dev first, then the consumer's pin "
                "advances to its dev-merge tag (no release is needed).",
            )
        )
        return 1, lines
    lines.append(
        (
            False,
            f"WIRE_COMPAT_PASS: all {len(verdicts)} pinned consumer tag(s) decode "
            f"every wire payload emitted by {graded} (oldest pin {oldest}).",
        )
    )
    return 0, lines
