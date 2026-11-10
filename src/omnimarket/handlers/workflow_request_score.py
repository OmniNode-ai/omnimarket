# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Score a workflow request against the catalogue's request schemas (OMN-18339).

The delegation ladder's R3b rung asks a model for a workflow name on the first
line and a JSON object of its parameters after it. Scoring by substring credited
the name and two parameter values and never looked at whether the request would
validate, so a response with no JSON object at all passed. This handler parses
the response, validates the object against the request schema of the workflow it
names, compares the situation's held parameter values, and for a patch-carrying
workflow checks that the patch is present and applies.

No threshold lives in this code: the schemas are data (the ladder's
``workflow_request_schemas.json``), the held values and the patch check's timeout
are in the bundle's scorer config, and a test refuses any numeric literal in the
scoring functions beyond the zeros and ones of ordinary control flow.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from omnimarket.models.model_workflow_request_score import (
    ModelWorkflowRequestScore,
)


def same_literal(a: object, b: object) -> bool:
    """Equality that does not let ``True == 1`` or ``1 == 1.0`` through."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_literal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(
            same_literal(x, y) for x, y in zip(a, b, strict=True)
        )
    return bool(a == b)


def _text(config: dict[str, object], key: str) -> str:
    value = config.get(key, "")
    return value if isinstance(value, str) else ""


#
# OMN-18339. R3b asks for a workflow name on the first line and a JSON object of
# its parameters after it. Scoring by substring (``score_exact_match``) credited
# the name and two parameter values and never looked at whether the request
# would validate, so a response with no JSON object at all passed. This scorer
# parses the response, validates the object against the request schema of the
# workflow it names, compares the situation's held parameter values, and for a
# patch-carrying workflow checks that the patch is present and applies.
#
# No threshold lives in this code: the schemas are data in
# fixtures/workflow_request_schemas.json, the held values and the patch check's
# timeout are in the bundle's scorer_config, and a test refuses any numeric
# literal here beyond the zeros and ones of ordinary control flow.

_WORKFLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_WORKFLOW_SCHEMAS_FILE = "workflow_request_schemas.json"


def parse_workflow_request(answer: str) -> tuple[str | None, object | None, str]:
    """``(workflow_name, parameters, problem)`` from an answer region.

    The workflow name is the first non-empty line, with a code-fence opener
    above it, and backticks or emphasis around it, folded away. The parameters
    are the first JSON value that starts at a brace after that line. Nothing
    else is repaired: prose on the name line, a missing object, or JSON that
    does not parse is returned as a ``problem`` and scores as a failure.
    """
    lines = [ln for ln in answer.strip().splitlines() if ln.strip()]
    while lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if not lines:
        return None, None, "the answer is empty"
    name = lines[0].strip().strip("`*").strip()
    if not _WORKFLOW_NAME_RE.match(name):
        return None, None, "the first line is not a bare workflow name"
    rest = "\n".join(lines[1:])
    start = rest.find("{")
    if start < 0:
        return name, None, "no JSON object follows the workflow name"
    try:
        parameters, _ = json.JSONDecoder().raw_decode(rest[start:])
    except json.JSONDecodeError as exc:
        return name, None, f"the parameters do not parse as JSON: {exc.msg}"
    return name, parameters, ""


def _workflow_request_patch_applies(
    patch: str, patch_check: dict[str, object], fixtures: Path
) -> tuple[bool, str]:
    """Does ``patch`` apply, and only to the named file, on the held subject text?

    Runs ``git apply --check`` in a scratch directory holding the one file the
    patch is meant for. A diff that is syntactically plausible but whose context
    is not in the file fails here, which is the point.
    """
    target = _text(patch_check, "target_file")
    subject = (fixtures / _text(patch_check, "subject_file")).read_text(
        encoding="utf-8"
    )
    timeout = patch_check.get("timeout_s")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise TypeError("patch_check 'timeout_s' must be an integer")
    # A JSON string that carries a diff routinely drops the final newline, and
    # git refuses a hunk without one; adding it is the only repair made.
    text = patch if patch.endswith("\n") else patch + "\n"
    with tempfile.TemporaryDirectory(prefix="ladder-r3b-") as tmp:
        tree = Path(tmp)
        (tree / target).parent.mkdir(parents=True, exist_ok=True)
        (tree / target).write_text(subject, encoding="utf-8")
        try:
            listed = subprocess.run(
                ["git", "apply", "--numstat", "-"],
                cwd=tree,
                input=text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if listed.returncode != 0:
                return False, "not a unified diff: " + listed.stderr.strip()
            touched = {ln.split("\t")[-1] for ln in listed.stdout.splitlines() if ln}
            if touched != {target}:
                return False, f"the diff touches {sorted(touched)}, not only {target}"
            checked = subprocess.run(
                ["git", "apply", "--check", "-"],
                cwd=tree,
                input=text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "git apply timed out"
    if checked.returncode != 0:
        return False, "does not apply: " + checked.stderr.strip()
    return True, "applies"


def score_workflow_request(
    answer: str, config: dict[str, object], fixtures: Path
) -> ModelWorkflowRequestScore:
    """Parse a workflow request and validate it against the workflow's schema.

    Every check is mechanical and recorded: the workflow is named and is the
    held one, the parameters are a JSON object, the object validates against the
    request schema of the workflow it names (every required parameter present,
    every value type-correct, no parameter the catalogue lacks), the situation's
    held values are present with the same type, and where the bundle declares a
    ``patch_check``, a patch is present and applies.
    """
    name, parameters, problem = parse_workflow_request(answer)
    schemas = json.loads((fixtures / _WORKFLOW_SCHEMAS_FILE).read_text("utf-8"))[
        "workflows"
    ]
    is_object = isinstance(parameters, dict)

    problems: list[str] = [problem] if problem else []
    schema_ok = False
    if name in schemas and isinstance(parameters, dict):
        found = sorted(
            Draft202012Validator(schemas[name]).iter_errors(parameters),
            key=lambda err: list(err.absolute_path),
        )
        schema_ok = not found
        problems += [
            "/".join(str(part) for part in err.absolute_path) + f": {err.validator}"
            for err in found
        ]

    checklist: dict[str, bool] = {
        "names_a_workflow": name is not None,
        "names_a_catalogue_workflow": name in schemas,
        "names_the_held_workflow": name == _text(config, "expected_workflow"),
        "parameters_are_a_json_object": is_object,
        "parameters_validate_against_request_schema": schema_ok,
    }
    held = config.get("expected_parameters", {})
    if not isinstance(held, dict):
        raise TypeError("scorer config 'expected_parameters' must be an object")
    for key, value in held.items():
        checklist[f"parameter:{key}"] = (
            isinstance(parameters, dict)
            and key in parameters
            and same_literal(parameters[key], value)
        )

    patch_check = config.get("patch_check")
    if isinstance(patch_check, dict):
        patch = parameters.get("patch") if isinstance(parameters, dict) else None
        present = isinstance(patch, str) and bool(patch.strip())
        checklist["patch_is_present"] = present
        applies = False
        if isinstance(patch, str) and present:
            applies, why = _workflow_request_patch_applies(patch, patch_check, fixtures)
            if not applies:
                problems.append(why)
        checklist["patch_applies"] = applies

    passed = all(checklist.values())
    failed = [k for k, ok in checklist.items() if not ok]
    return ModelWorkflowRequestScore(
        outcome="pass" if passed else "fail",
        score=sum(1 for ok in checklist.values() if ok) / len(checklist),
        detail=(
            "valid request for the held workflow"
            if passed
            else "failed " + ", ".join(failed) + "; " + "; ".join(problems)
        ),
        checklist=checklist,
    )
