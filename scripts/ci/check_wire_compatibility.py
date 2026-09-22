# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a wire change the LAST RELEASED consumer cannot decode (OMN-18868).

THE DEFECT THIS EXISTS TO CLOSE

``omnimarket#2692`` added ``published_at`` to ``ModelDelegateSkillRequest``.
Every test in this repository passed, and they could not have done otherwise:
**in a test the producer and the consumer are the same commit.** The DEPLOYED
consumer was an older released wheel whose model is ``extra="forbid"``, so it
refused the payload at the decode boundary and dead-lettered every delegate
(OMN-18852). At 2026-09-20T01:43:38Z a delegate through the sanctioned wrapper
terminalised on the failure topic with ``ModelBoundaryFailureTerminal`` and the
reason ``published_at: Extra inputs are not permitted``. OMN-18633 is the same
class on a different field name, which is the argument for a gate rather than
a third one-off fix.

771 golden-chain files did not catch it. Version skew IS the defect, and a
single-commit test cannot express a version skew.

Worse, the version numbers were no help either: the OMN-18852 postmortem
records producer and consumer BOTH reporting ``0.4.134``, because the consumer
image had been baked fifteen minutes earlier from a different tree. So a
version comparison -- which this repository already has, in
``scripts/check_release_identity.py`` -- structurally could not have caught it.
Only decoding the new shape with the old model can.

WHAT THIS GATE DOES

For every wire model module a pull request changes, it

1. resolves the LAST RELEASED version from the release tags this tree descends
   from, never from the working tree;
2. materialises that release's ``src/`` out of git into a scratch directory;
3. asks the WORKING TREE, in its own subprocess, which keys each wire model can
   emit;
4. pushes those keys through the RELEASED model's own ``model_validate``, in a
   second subprocess whose ``sys.path`` is rooted in the released tree; and
5. fails the pull request on the released validator's own refusal, naming the
   field, the model and the released version that refused it.

This is the mechanical form of the consumer-first rule -- a consumer able to
accept the new shape must be RELEASED before the producer that emits it -- which
until now existed only as intent.

WHAT IT DELIBERATELY DOES NOT DO

**It does not grade values, only keys.** Every key in the replay payload carries
one placeholder string and only ``extra_forbidden`` and ``missing`` errors are
findings; see ``_wire_compat_probe.PROBE_PLACEHOLDER`` for the full argument.
Synthesising a type-correct value per annotation would mean shipping a factory
library, and every gap in it would redden a pull request that broke nothing --
which is how a gate gets switched off.

**It grades the MAXIMAL shape the producer can emit**, so a new optional field
is a finding even when the producer excludes it while unset. That is not
over-strictness, it is the rule: ``published_at`` carries
``exclude_if=lambda value: value is None`` TODAY and is still refused by a
pre-``published_at`` consumer the moment anything stamps it -- and stamping it
is the entire reason the field exists. The remedy the gate is steering toward
is the correct one: release the consumer first.

**It grades top-level keys only.** A nested model's own shape change is graded
when that nested model's module is itself part of the changed wire set, not
transitively through its parent. A change to a wire model's nested type that
lives OUTSIDE the wire package is out of scope and is named here rather than
left for a reader to discover.

**There is no waiver list.** One was considered and rejected: a waiver is the
hole the gate exists to close, and the sanctioned escape is the release order
the gate is enforcing.

FAIL-CLOSED, EVERYWHERE

An unresolvable release, a release whose module will not import, or a probe
that resolved the wrong copy of a module each exit non-zero with their own
outcome token. A gate that cannot run must never read as a gate that passed:
that shape -- the quiet green -- is the failure class this whole epic is about.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

#: Directories, relative to the repository root, whose ``model_*.py`` modules
#: are wire payload contracts.
#:
#: This repository has no marker base class and no wire registry -- the only
#: structural signal is a pydantic model under a directory literally named
#: ``wire``. Rather than infer the set at run time (where a new wire directory
#: would silently enrol, or silently not), it is declared here and
#: ``tests/ci/test_check_wire_compatibility_omn18868.py`` asserts the
#: declaration still covers every such directory in the tree. A new wire
#: package therefore turns a test RED until someone decides, in writing,
#: whether it is graded.
WIRE_MODEL_ROOTS: tuple[str, ...] = ("src/omnimarket/models/delegation/wire",)

#: Where the importable package tree lives, relative to the repository root.
SRC_DIR = "src"

OUTCOME_PASS = "WIRE_COMPAT_PASS"
OUTCOME_NO_WIRE_CHANGE = "WIRE_COMPAT_NO_WIRE_CHANGE"
OUTCOME_REFUSED = "WIRE_COMPAT_REFUSED"
OUTCOME_RELEASE_UNRESOLVABLE = "WIRE_COMPAT_RELEASE_UNRESOLVABLE"
OUTCOME_RELEASE_UNREADABLE = "WIRE_COMPAT_RELEASE_UNREADABLE"
OUTCOME_PROBE_FAILED = "WIRE_COMPAT_PROBE_FAILED"

_PROBE = Path(__file__).with_name("_wire_compat_probe.py")


class GateError(Exception):
    """A condition the gate cannot grade, carrying its own outcome token."""

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


@dataclass(frozen=True)
class Finding:
    """One refusal by a released consumer, in the terms the reader needs."""

    module: str
    model: str
    field: str
    error_type: str
    message: str

    def render(self, released: str) -> str:
        if self.error_type == "extra_forbidden":
            reason = (
                f"the released consumer {self.model} at {released} REFUSES the "
                f"key {self.field!r}: {self.message}"
            )
        else:
            reason = (
                f"the released consumer {self.model} at {released} REQUIRES the "
                f"key {self.field!r}, which this tree no longer emits: "
                f"{self.message}"
            )
        return f"  {self.module}: {reason}"


#: Git location variables that OVERRIDE both ``cwd=`` and ``git -C``.
#:
#: Git exports these into every hook environment, so a gate invoked from a
#: pre-push hook -- or a test that calls :func:`main` in-process under one --
#: would operate on the REAL invoking worktree however carefully the caller set
#: ``cwd``. This gate reads tags and extracts archives, so a silent retarget
#: would resolve "the last released consumer" out of a different repository
#: altogether. Scrubbed locally rather than imported from ``omnibase_core``
#: because this module is a CI gate and stays importable with only
#: ``packaging`` (OMN-14891 / OMN-18434).
_GIT_LOCATION_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _scrubbed_env() -> dict[str, str]:
    """Return the ambient environment with git's location overrides removed."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}


def _git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_scrubbed_env(),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        stderr = os.fsdecode(result.stderr).strip()
        raise ValueError(stderr or f"git {' '.join(args)} failed")
    return os.fsdecode(result.stdout).strip()


def _published_tags(root: Path, anchor: str) -> list[str]:
    """Return the release tags this tree DESCENDS FROM (the OMN-18443 rule).

    Anchoring on ``git tag --merged`` rather than ``git tag --list`` is not a
    style choice, it is a correctness one this repository already paid for:
    on a ``pull_request`` event the tree is pinned at trigger time while the
    tag list is fetched at run time, so the plain list can contain releases cut
    from lineages the tree does not contain. ``check_release_identity.py``
    carries the measured incident. A gate that resolved "the released consumer"
    from a lineage this tree never touched would replay against a model that
    was never the one skewed against it.

    **This gate does NOT take that module's shallow-clone fallback, and the
    difference is deliberate.** There, a shallow clone's thinner ``--merged``
    answer is the PERMISSIVE direction, so falling back to the full tag list is
    stricter. Here the arithmetic inverts: fewer reachable tags resolve an
    OLDER released consumer, an older consumer knows FEWER fields, and the
    replay is therefore MORE likely to refuse. ``git tag --list`` is the
    permissive direction for this gate -- it can resolve a NEWER consumer than
    anything this tree descends from and pass a payload nothing deployed can
    decode. So ``--merged`` is used unconditionally, and an anchor git cannot
    resolve at all raises rather than widening the set.
    """
    return _git(["tag", "--merged", anchor], cwd=root).splitlines()


def resolve_released_tag(root: Path, anchor: str) -> tuple[str, Version]:
    """Return the highest release tag this tree descends from.

    Raises:
        GateError: no parseable release tag exists. This FAILS CLOSED on
            purpose. "Nothing has been released, so nothing can refuse" is a
            true sentence and still the wrong behaviour here: it is
            indistinguishable at the gate from a fetch that dropped the tags,
            and a gate that reads a missing input as a pass is the precise
            failure class this epic exists to end.
    """
    try:
        tags = _published_tags(root, anchor)
    except ValueError as exc:
        raise GateError(
            OUTCOME_RELEASE_UNRESOLVABLE,
            f"git could not enumerate the release tags reachable from "
            f"{anchor!r} ({exc}); the released consumer model cannot be "
            "resolved, so wire compatibility is UNPROVEN rather than proven",
        ) from exc

    best: tuple[str, Version] | None = None
    for line in tags:
        tag = line.strip()
        if not tag:
            continue
        try:
            version = Version(tag[1:] if tag.startswith("v") else tag)
        except InvalidVersion:
            continue
        if best is None or version > best[1]:
            best = (tag, version)
    if best is None:
        raise GateError(
            OUTCOME_RELEASE_UNRESOLVABLE,
            f"no parseable release tag is reachable from {anchor!r}; the "
            "released consumer model cannot be resolved, so wire compatibility "
            "is UNPROVEN rather than proven",
        )
    return best


def changed_wire_modules(
    root: Path,
    *,
    base: str | None,
    explicit: list[str] | None,
    wire_roots: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Return ``(repo relative path, dotted module)`` for changed wire models.

    The three-dot diff is the house idiom here and is load bearing. The two-dot
    form describes the difference between two trees, so on a stale base it
    reports every file a PEER landed on the base branch as this branch's
    change -- which would arm this gate against a pull request that touched no
    wire model at all.
    """
    if explicit is not None:
        files = explicit
    elif base:
        files = [
            line
            for line in _git(
                ["diff", "--name-only", f"{base}...HEAD"], cwd=root
            ).splitlines()
            if line.strip()
        ]
    else:
        raise GateError(
            OUTCOME_PROBE_FAILED,
            "neither --base nor --changed-file was given, so the changed set "
            "is unknown; refusing rather than grading an empty diff as clean",
        )

    modules: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        name = Path(path).name
        if not name.startswith("model_"):
            continue
        if not any(path.startswith(f"{wire_root}/") for wire_root in wire_roots):
            continue
        relative = Path(path).relative_to(SRC_DIR).with_suffix("")
        dotted = ".".join(relative.parts)
        if dotted in seen:
            continue
        seen.add(dotted)
        modules.append((path, dotted))
    return modules


def materialise_release(root: Path, tag: str, destination: Path) -> Path:
    """Extract the released ``src/`` tree out of git, without a checkout.

    ``git archive`` reads the tag's tree directly, so it neither disturbs the
    working tree nor needs a second clone. If the release carries no ``src/``
    the gate FAILS CLOSED: an empty extraction would otherwise make every model
    read as "absent from the release", which the grader treats as new and
    therefore ungradeable -- a silent pass on a release we could not read.
    """
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", tag, SRC_DIR],
        cwd=str(root),
        env=_scrubbed_env(),
        capture_output=True,
        check=False,
    )
    if archive.returncode:
        raise GateError(
            OUTCOME_RELEASE_UNREADABLE,
            f"could not read {SRC_DIR!r} out of release {tag}: "
            f"{os.fsdecode(archive.stderr).strip()}",
        )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(destination)],
        input=archive.stdout,
        capture_output=True,
        check=False,
    )
    if extract.returncode:
        raise GateError(
            OUTCOME_RELEASE_UNREADABLE,
            f"could not unpack release {tag}: {os.fsdecode(extract.stderr).strip()}",
        )
    released_src = destination / SRC_DIR
    if not released_src.is_dir():
        raise GateError(
            OUTCOME_RELEASE_UNREADABLE,
            f"release {tag} contains no {SRC_DIR!r} directory",
        )
    return released_src


def _run_probe(
    *,
    src_root: Path,
    module: str,
    mode: str,
    workdir: Path,
    payloads: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Run the probe subprocess and return its JSON answer.

    Raises:
        GateError: the probe did not produce an answer. A probe that crashed
            tells us nothing about compatibility, and the one thing it must
            never do is resolve to "compatible".
    """
    out = workdir / f"{mode}-{module.replace('.', '_')}.json"
    argv = [
        sys.executable,
        str(_PROBE),
        "--src-root",
        str(src_root),
        "--module",
        module,
        "--mode",
        mode,
        "--out",
        str(out),
    ]
    if payloads is not None:
        payload_file = workdir / f"payload-{module.replace('.', '_')}.json"
        payload_file.write_text(json.dumps(payloads), encoding="utf-8")
        argv += ["--payloads", str(payload_file)]

    # A clean import environment. Inheriting the caller's PYTHONPATH is how the
    # released probe would end up importing the working tree -- the one outcome
    # that turns this gate into the same-commit test it replaces.
    env = {k: v for k, v in _scrubbed_env().items() if k != "PYTHONPATH"}
    result = subprocess.run(argv, capture_output=True, check=False, env=env)
    if result.returncode or not out.exists():
        raise GateError(
            OUTCOME_PROBE_FAILED,
            f"probe {mode} of {module} under {src_root} failed "
            f"(exit {result.returncode}): "
            f"{os.fsdecode(result.stderr).strip() or 'no diagnostic'}",
        )
    answer: dict[str, object] = json.loads(out.read_text(encoding="utf-8"))
    return answer


def evaluate(
    *,
    root: Path,
    tree_src: Path,
    released_src: Path,
    released_tag: str,
    modules: list[tuple[str, str]],
    workdir: Path,
) -> tuple[list[Finding], list[str]]:
    """Replay every changed wire model against its released counterpart."""
    findings: list[Finding] = []
    notes: list[str] = []
    for path, module in modules:
        described = _run_probe(
            src_root=tree_src, module=module, mode="describe", workdir=workdir
        )
        if not described:
            notes.append(f"{path}: defines no wire model of its own; nothing to grade")
            continue

        payloads: dict[str, list[str]] = {}
        for class_name, surface in described.items():
            assert isinstance(surface, dict)
            keys = surface.get("emitted_keys") or []
            assert isinstance(keys, list)
            payloads[class_name] = [str(key) for key in keys]

        try:
            replayed = _run_probe(
                src_root=released_src,
                module=module,
                mode="replay",
                workdir=workdir,
                payloads=payloads,
            )
        except GateError:
            if _module_exists(released_src, module):
                raise
            notes.append(
                f"{path}: no module {module} in release {released_tag}; the whole "
                "module is new, so no released consumer can be skewed against it"
            )
            continue

        for class_name, outcome in replayed.items():
            assert isinstance(outcome, dict)
            if outcome.get("absent"):
                notes.append(
                    f"{path}: {class_name} is absent from release {released_tag}; "
                    "a model no release defines has no deployed consumer to refuse it"
                )
                continue
            errors = outcome.get("errors") or []
            assert isinstance(errors, list)
            for error in errors:
                assert isinstance(error, dict)
                findings.append(
                    Finding(
                        module=path,
                        model=class_name,
                        field=str(error.get("field", "")),
                        error_type=str(error.get("type", "")),
                        message=str(error.get("message", "")),
                    )
                )
    return findings, notes


def _module_exists(src_root: Path, module: str) -> bool:
    """True when *module* resolves to a file under *src_root*."""
    relative = Path(*module.split("."))
    return (src_root / relative).with_suffix(".py").is_file() or (
        src_root / relative / "__init__.py"
    ).is_file()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="Repository to grade.")
    parser.add_argument(
        "--base",
        default=None,
        help="Base ref; the changed set is 'git diff --name-only <base>...HEAD'.",
    )
    parser.add_argument(
        "--changed-file",
        action="append",
        default=None,
        help="Repo-relative path, repeatable; replaces the git diff (tests).",
    )
    parser.add_argument(
        "--tag-anchor",
        default="HEAD",
        help="Commit whose reachable release tags count as published.",
    )
    parser.add_argument(
        "--wire-root",
        action="append",
        default=None,
        help="Repo-relative wire directory, repeatable; defaults to the "
        "declared WIRE_MODEL_ROOTS.",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    wire_roots = tuple(args.wire_root) if args.wire_root else WIRE_MODEL_ROOTS

    try:
        modules = changed_wire_modules(
            root, base=args.base, explicit=args.changed_file, wire_roots=wire_roots
        )
        if not modules:
            print(
                f"{OUTCOME_NO_WIRE_CHANGE}: this change touches no wire payload "
                f"model under {', '.join(wire_roots)}."
            )
            return 0

        released_tag, released_version = resolve_released_tag(root, args.tag_anchor)
        with tempfile.TemporaryDirectory(prefix="wire-compat-") as raw:
            workdir = Path(raw)
            released_src = materialise_release(root, released_tag, workdir / "release")
            findings, notes = evaluate(
                root=root,
                tree_src=root / SRC_DIR,
                released_src=released_src,
                released_tag=released_tag,
                modules=modules,
                workdir=workdir,
            )
    except GateError as exc:
        print(f"{exc.outcome}: {exc.detail}", file=sys.stderr)
        return 1

    graded = ", ".join(module for _, module in modules)
    for note in notes:
        print(f"note: {note}")

    if findings:
        print(
            f"{OUTCOME_REFUSED}: the last released consumer ({released_tag}, "
            f"version {released_version}) cannot decode this tree's wire "
            f"payloads.",
            file=sys.stderr,
        )
        for finding in findings:
            print(finding.render(released_tag), file=sys.stderr)
        print(
            "\nConsumer-first is the remedy, not a waiver: release a consumer "
            "that accepts the new shape BEFORE merging the producer that emits "
            "it. Until that release exists, every deployed consumer running "
            f"{released_tag} dead-letters this payload at its decode boundary, "
            "which is OMN-18852 verbatim.",
            file=sys.stderr,
        )
        return 1

    print(
        f"{OUTCOME_PASS}: released consumer {released_tag} decodes every wire "
        f"payload emitted by {graded}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
