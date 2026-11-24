# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket runs the hard-coded model guard it hosts, against a baseline that only shrinks.

The guard is ``check-hardcoded-model-config-compute`` of omnibase_core. Four
surfaces carry it here and each is asserted: the pre-commit hook, the
``hardcoded-model-config`` job of ``ci.yml`` (a local call of the reusable
workflow this repository hosts), that job's row in the CI Summary strict table,
and ``config/hardcoded_model_config_baseline.yaml``. The planted cases hand the
guard a source file per form of model choice and require a finding; the
registry-wide rule is a pure function over a repository tree, applied to this
tree and to planted trees, so it needs no other clone and runs on every runner.

The guard is pinned at the omnibase_core commit that judges families M, E, E-LAN,
T and N (model ids, inference endpoints, private addresses, tier aliases, engine
names). Families O, G, F, P, S, H and B (operational literals) arrive with a later
core commit and are not enrolled here.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.handlers.handler_private_vocabulary_resolution import (
    build_container,
    capture_environment,
    resolve_private_vocabulary,
)
from omnibase_core.validation.hardcoded_model_config import (
    runtime_hardcoded_model_config as runtime,
)
from omnibase_core.validation.hardcoded_model_config.handler import (
    added_entries,
    scan,
)
from omnibase_core.validation.hardcoded_model_config.models import (
    ModelHardcodedModelConfigBaselineEntry,
    ModelHardcodedModelConfigPolicy,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REUSABLE = REPO_ROOT / ".github" / "workflows" / "hardcoded-model-config-reusable.yml"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
RULES = REPO_ROOT / "scripts" / "ci" / "ci_summary_rules.json"
BASELINE = REPO_ROOT / "config" / "hardcoded_model_config_baseline.yaml"

HOOK_ID = "check-hardcoded-model-config-compute"
JOB_KEY = "hardcoded-model-config"
STRICT_ROW = "hardcoded-model-config / hardcoded-model-config"
REUSABLE_USE = "./.github/workflows/hardcoded-model-config-reusable.yml"
CORE_REPO = "https://github.com/OmniNode-ai/omnibase_core"

# The families the pinned guard judges and the baseline may therefore hold.
ENROLLED_FAMILIES = frozenset({"M", "E", "E-LAN", "T", "N"})
# The families a later core commit adds; they are not judged at the pin.
UNENROLLED_FAMILIES = frozenset({"O", "G", "F", "P", "S", "H", "B"})


def _precommit_repos() -> list[dict[str, Any]]:
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    repos: list[dict[str, Any]] = config["repos"]
    return repos


def _hook_rev() -> str:
    revs = [
        str(repo["rev"])
        for repo in _precommit_repos()
        if repo.get("repo") == CORE_REPO
        and any(hook["id"] == HOOK_ID for hook in repo["hooks"])
    ]
    assert len(revs) == 1, revs
    return revs[0]


def _job() -> dict[str, Any]:
    job: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        JOB_KEY
    ]
    return job


def _strict_jobs() -> tuple[str, ...]:
    doc = json.loads(RULES.read_text(encoding="utf-8"))
    return tuple(e["name"] for e in doc["rules"]["STRICT_GATE_JOBS"]["entries"])


def _baseline_entries() -> list[ModelHardcodedModelConfigBaselineEntry]:
    return runtime._parse_baseline(BASELINE.read_text(encoding="utf-8"), str(BASELINE))


# --- AC1: the guard runs on every pull request, inside CI Summary -------------


def test_the_ci_job_calls_the_hosted_reusable_unconditionally() -> None:
    job = _job()
    assert job["uses"] == REUSABLE_USE
    assert "if" not in job
    assert "needs" not in job
    assert "continue-on-error" not in job
    assert REUSABLE.is_file()


def test_the_job_is_a_strict_gate_of_ci_summary() -> None:
    assert STRICT_ROW in _strict_jobs()


def test_the_ci_pin_and_the_hook_rev_name_one_core_commit() -> None:
    core_ref = str(_job()["with"]["core_ref"])
    assert re.fullmatch(r"[0-9a-f]{40}", core_ref)
    # The hook rev is a dev-merge tag whose last segment is the commit's first 12 hex.
    assert core_ref.startswith(_hook_rev().rsplit("-", 1)[-1])


def test_the_hook_scans_the_baseline_and_rewrites_nothing() -> None:
    hooks = [
        hook
        for repo in _precommit_repos()
        for hook in repo["hooks"]
        if hook["id"] == HOOK_ID
    ]
    assert len(hooks) == 1
    assert "--fix" not in str(hooks[0])
    assert hooks[0].get("stages", ["pre-commit"]) == ["pre-commit"]


def test_the_reusable_judges_the_whole_tree_and_refuses_a_grown_baseline() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "--all" in text
    assert "config/hardcoded_model_config_baseline.yaml" in text
    assert '--base "${PR_BASE_SHA}"' in text or "--base ${PR_BASE_SHA}" in text
    job = yaml.safe_load(text)["jobs"][JOB_KEY]
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])
    assert all("if" not in step for step in job["steps"] if "Run the" in step["name"])


# --- AC3: the baseline only shrinks -------------------------------------------


def test_the_baseline_holds_only_enrolled_families_and_no_retired_value() -> None:
    entries = _baseline_entries()
    assert entries, "an empty baseline means the guard was never run over this tree"
    families = {e.family for e in entries}
    assert families <= ENROLLED_FAMILIES, families - ENROLLED_FAMILIES
    assert "R" not in families
    assert not families & UNENROLLED_FAMILIES


def test_the_baseline_is_in_the_shape_the_guard_writes() -> None:
    entries = _baseline_entries()
    assert [e.key() for e in entries] == sorted(e.key() for e in entries)


def _entry(path: str, family: str, sha: str) -> ModelHardcodedModelConfigBaselineEntry:
    return ModelHardcodedModelConfigBaselineEntry(
        path=path,
        family=family,
        content_sha1=sha,
    )


def test_a_baseline_with_one_more_entry_than_its_base_is_refused() -> None:
    base = [_entry("src/a.py", "M", "1" * 40), _entry("src/b.py", "M", "2" * 40)]
    grown = [*base, _entry("src/c.py", "M", "3" * 40)]
    assert [e.path for e in added_entries(base, grown)] == ["src/c.py"]


def test_a_re_frozen_entry_is_refused_and_a_removed_entry_is_not() -> None:
    base = [_entry("src/a.py", "M", "1" * 40), _entry("src/b.py", "M", "2" * 40)]
    refrozen = [_entry("src/a.py", "M", "9" * 40), base[1]]
    assert [e.content_sha1 for e in added_entries(base, refrozen)] == ["9" * 40]
    assert added_entries(base, base[:1]) == []
    assert added_entries(base, base) == []


# --- AC2: a planted model choice is refused, in every form ---------------------


@pytest.fixture(scope="module")
def policy() -> ModelHardcodedModelConfigPolicy:
    container = build_container()
    return runtime.load_policy(
        resolve_private_vocabulary(
            container,
            "hardcoded_model_config_lab_denylist",
            environment=capture_environment(container),
            now=datetime.now(UTC),
        )
    )


SOURCE_PATH = "src/omnimarket/nodes/node_planted_effect/handler_planted.py"
PLANTED_FORMS_FILE = (
    Path(__file__).parent / "fixtures" / "omn26651" / "planted_forms.fixture"
)

# form -> {path, text, family}: one model choice per form, kept in a file the guard does not
# scan because the file exists to hold the choices the guard must refuse.
PLANTED_FORMS: dict[str, dict[str, str]] = yaml.safe_load(
    PLANTED_FORMS_FILE.read_text(encoding="utf-8")
)


@pytest.mark.parametrize("form", sorted(PLANTED_FORMS))
def test_a_planted_model_choice_is_refused(
    form: str, policy: ModelHardcodedModelConfigPolicy
) -> None:
    planted = PLANTED_FORMS[form]
    path, text, family = planted["path"], planted["text"], planted["family"]
    found = {f.family for f in scan(path, text, policy)}
    assert family in found, f"{form}: planted {text!r} gave {found}"


@pytest.mark.parametrize("form", sorted(PLANTED_FORMS))
def test_the_same_file_without_the_choice_is_clean(
    form: str, policy: ModelHardcodedModelConfigPolicy
) -> None:
    assert scan(PLANTED_FORMS[form]["path"], "VALUE = 1\n", policy) == []


def test_the_bare_local_alias_is_not_yet_refused_by_the_pinned_guard(
    policy: ModelHardcodedModelConfigPolicy,
) -> None:
    """Tripwire for the one planted form the pinned guard does not yet refuse.

    A bare ``local-qwen`` alias has no digit or dash after the family name, so the
    family M pattern of the pinned omnibase_core commit does not match it; the
    pattern gains it in omnibase_core#2364. When this test fails, the installed
    core carries that change: move the alias into ``PLANTED_FORMS`` as an ``M``
    form and delete this test.
    """
    alias = 'BACKEND_LABEL = "local-qwen"\n'
    assert "local-qwen" not in policy.model_id_pattern
    assert scan(SOURCE_PATH, alias, policy) == []


# --- AC4: a registry repository with source runs the guard ---------------------


def guard_declarations(repo_root: Path) -> tuple[bool, bool]:
    """(runs the hook, calls the reusable) for the repository tree at ``repo_root``."""
    precommit = repo_root / ".pre-commit-config.yaml"
    hook = False
    if precommit.is_file():
        config = yaml.safe_load(precommit.read_text(encoding="utf-8")) or {}
        hook = any(
            h.get("id") == HOOK_ID
            for repo in config.get("repos", [])
            for h in repo.get("hooks", [])
        )
    call = False
    for workflow in sorted((repo_root / ".github" / "workflows").glob("*.y*ml")):
        document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        for job in (document.get("jobs") or {}).values():
            uses = str(job.get("uses", "")) if isinstance(job, dict) else ""
            if uses.split("@")[0].endswith("hardcoded-model-config-reusable.yml"):
                call = True
    return hook, call


def has_source(repo_root: Path) -> bool:
    src = repo_root / "src"
    return src.is_dir() and any(
        p.suffix in {".py", ".ts", ".tsx"} for p in src.rglob("*")
    )


def refusal(repo_root: Path) -> str | None:
    """Why this tree is refused: it has source and declares neither the hook nor the call."""
    if not has_source(repo_root):
        return None
    hook, call = guard_declarations(repo_root)
    if hook or call:
        return None
    return "declares neither the check-hardcoded-model-config-compute hook nor the reusable call"


def _plant(root: Path, *, hook: bool, call: bool, source: bool) -> Path:
    (root / ".github" / "workflows").mkdir(parents=True)
    if source:
        (root / "src" / "pkg").mkdir(parents=True)
        (root / "src" / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    hooks = f"      - id: {HOOK_ID}\n" if hook else "      - id: other-hook\n"
    (root / ".pre-commit-config.yaml").write_text(
        f"repos:\n  - repo: {CORE_REPO}\n    rev: v1\n    hooks:\n{hooks}",
        encoding="utf-8",
    )
    uses = (
        "OmniNode-ai/omnimarket/.github/workflows/hardcoded-model-config-reusable.yml@abc"
        if call
        else "OmniNode-ai/omnimarket/.github/workflows/other.yml@abc"
    )
    (root / ".github" / "workflows" / "ci.yml").write_text(
        f"jobs:\n  guard:\n    uses: {uses}\n", encoding="utf-8"
    )
    return root


def test_this_repository_declares_the_hook_and_the_reusable_call() -> None:
    assert has_source(REPO_ROOT)
    assert guard_declarations(REPO_ROOT) == (True, True)
    assert refusal(REPO_ROOT) is None


def test_a_repository_with_source_and_neither_declaration_is_refused(
    tmp_path: Path,
) -> None:
    root = _plant(tmp_path / "bare", hook=False, call=False, source=True)
    assert refusal(root) is not None


@pytest.mark.parametrize(("hook", "call"), [(True, False), (False, True), (True, True)])
def test_a_repository_with_source_and_either_declaration_is_accepted(
    tmp_path: Path, hook: bool, call: bool
) -> None:
    root = _plant(tmp_path / "enrolled", hook=hook, call=call, source=True)
    assert guard_declarations(root) == (hook, call)
    assert refusal(root) is None


def test_a_repository_with_no_source_is_not_judged(tmp_path: Path) -> None:
    root = _plant(tmp_path / "docs-only", hook=False, call=False, source=False)
    assert refusal(root) is None


def test_removing_the_call_from_this_tree_is_refused(tmp_path: Path) -> None:
    """Control: the same rule, fed this repository's tree minus both declarations."""
    root = tmp_path / "market-without-guard"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    ci = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    del ci["jobs"][JOB_KEY]
    (root / ".github" / "workflows" / "ci.yml").write_text(
        yaml.safe_dump(ci), encoding="utf-8"
    )
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    for repo in config["repos"]:
        repo["hooks"] = [h for h in repo["hooks"] if h["id"] != HOOK_ID]
    (root / ".pre-commit-config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    assert guard_declarations(root) == (False, False)
    assert refusal(root) is not None
