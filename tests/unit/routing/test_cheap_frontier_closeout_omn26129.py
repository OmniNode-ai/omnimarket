# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129 AC2 and AC5: the withdrawn rung is gone everywhere it could run, and the smoke is recorded.

OpenRouter stopped listing cohere/north-mini-code:free, cheap_frontier's only rung, and
omnimarket#6491 replaced it with nvidia/nemotron-3.5-super-vl:free. The checks the ticket
first bound covered half of AC2 and AC5 (closeout sweep of 2026-11-20 14:28Z):

* AC2 also asks that no packaged *judge* configuration names the withdrawn rung, and that
  the tests which pinned it now name the new one. The judge picks its backend from the
  routing contract (``select_independent_judge``, the selector the delegate dispatch port
  calls), so the judge check drives that selector over the packaged backend list for
  every model family it knows, not only the text of the judge files. North-mini-code never
  declared the judge capability, so the judge half held before #6491 too; the planted
  control proves the check would see a withdrawn judge. The pinning tests are the eight
  #6491 re-pointed to ``openrouter-nemotron-3-5-super``; other tests that still name the
  old id assert it against their own fixture contracts and never read the packaged rung.
* AC5 also asks for each candidate's p50 and for the ten task prompts stored beside the
  receipt rows. The 2026-11-20 candidate set is pinned: one measured candidate (the placed
  Nemotron 3.5 Super) and two that were never measured because OpenRouter answered 429
  when the key was registered for them, recorded under ``not_measured`` with that reason.
  An unmeasured candidate has no accepted count or p50 to assert.

Each check below runs through a helper, and a control feeds that same helper the defect
it looks for.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.judge_selection import (
    answer_gate_contract,
    independent_judge_config,
    select_independent_judge,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_PACKAGE = _REPO / "src" / "omnimarket"
_CONFIGS = _PACKAGE / "configs"
_ROUTING = _CONFIGS / "bifrost_delegation.yaml"
_PLACEMENT = _CONFIGS / "routing_tiers.yaml"
_SMOKE = (
    _REPO / "tests" / "fixtures" / "receipts" / "cheap_frontier_smokes_2026_11_20.json"
)

WITHDRAWN = ("cohere/north-mini-code:free", "openrouter-north-mini-code")
NEW_RUNG = "openrouter-nemotron-3-5-super"
NEW_MODEL = "nvidia/nemotron-3.5-super-vl:free"
UNMEASURED = frozenset(
    {"google/gemma-4-31b-it:free", "inclusionai/ling-3.2-flash:free"}
)
JUDGE_CONFIGS = (
    "delegation_independent_judge.v1.yaml",
    "delegation_judge_rubrics.v1.yaml",
    "delegation_acceptance_judge_rubric.v1.yaml",
)
# The eight tests omnimarket#6491 (79f31e93) re-pointed from the old rung to the new one.
# The first seven no longer name the old rung at all.
PINNING_TESTS = (
    "tests/unit/delegation/test_active_cloud_tiers_omn16891.py",
    "tests/unit/delegation/test_delegation_tier_escalation.py",
    "tests/unit/delegation/test_openrouter_review_reasoning_omn19432.py",
    "tests/unit/delegation/test_review_judge_flag_omn20910.py",
    "tests/unit/routing/test_nemotron_ultra_withdrawn_omn20376.py",
    "tests/unit/routing/test_north_mini_spread_omn17427.py",
    "tests/unit/routing/test_placement_render_parity_omn17427.py",
)
# The eighth keeps the old ids as named constants, to assert the withdrawal itself.
PINNING_TESTS_KEEPING_THE_OLD_ID = (
    "tests/validators/test_provider_model_catalogue.py",
)
PROMPT_MIX = {"summarization": 7, "review": 1, "code_generation": 1, "test": 1}


def _strings(node: Any) -> Iterator[str]:
    """Every string key and value in a parsed YAML or JSON tree (comments are not parsed)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, str):
        yield node


def _parse(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return [json.loads(text)]
    return list(yaml.safe_load_all(text))


def _packaged_config_files(root: Path = _PACKAGE) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml", ".json"}
    )


def _named_withdrawn(paths: Iterable[Path]) -> list[tuple[str, str]]:
    """(path, withdrawn id) for every parsed value naming a withdrawn id.

    A file that does not parse raises: a config the scan cannot read has not been checked.
    """
    found: set[tuple[str, str]] = set()
    for path in paths:
        for document in _parse(path):
            for text in _strings(document):
                for withdrawn in WITHDRAWN:
                    if withdrawn in text:
                        found.add((str(path), withdrawn))
    return sorted(found)


def _packaged_backends() -> list[dict[str, Any]]:
    """The packaged routing contract's backends, read from the file the wheel ships."""
    return list(yaml.safe_load(_ROUTING.read_text(encoding="utf-8"))["backends"])


def _ladder(backends: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
    return [
        b for b in backends if b.get("tier") == tier and not b.get("explicit_pin_only")
    ]


def _producer_families() -> list[str]:
    return [
        token
        for family in independent_judge_config()["model_families"]
        for token in family["match"]
    ]


def _withdrawn_judges(backends: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """(how many producer families got a judge, the withdrawn judges any of them got)."""
    chosen = [
        select_independent_judge(token, backends) for token in _producer_families()
    ]
    picked = [judge for judge in chosen if judge is not None]
    withdrawn = sorted(
        {
            str(judge.get(field))
            for judge in picked
            for field in ("backend_id", "model_name")
            if str(judge.get(field)) in WITHDRAWN
        }
    )
    return len(picked), withdrawn


def _pinning_problems(
    texts: Mapping[str, str], *, must_drop_the_old_id: bool
) -> list[str]:
    problems = [
        f"{name} does not name {NEW_RUNG}"
        for name, text in texts.items()
        if NEW_RUNG not in text
    ]
    if must_drop_the_old_id:
        problems += [
            f"{name} still names {withdrawn}"
            for name, text in texts.items()
            for withdrawn in WITHDRAWN
            if withdrawn in text
        ]
    return problems


def _read(relatives: Iterable[str]) -> dict[str, str]:
    return {rel: (_REPO / rel).read_text(encoding="utf-8") for rel in relatives}


def _receipt() -> dict[str, Any]:
    receipt: dict[str, Any] = json.loads(_SMOKE.read_text(encoding="utf-8"))
    return receipt


def _candidate_problems(receipt: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    prompts = len(receipt.get("task_prompts") or [])
    smokes = receipt.get("smokes") or []
    unmeasured = receipt.get("not_measured") or []
    measured_models = {row.get("model") for row in smokes}
    unmeasured_models = {row.get("model") for row in unmeasured}
    if measured_models != {NEW_MODEL}:
        problems.append(
            f"measured candidates {sorted(map(str, measured_models))} are not [{NEW_MODEL}]"
        )
    if unmeasured_models != UNMEASURED:
        problems.append(
            f"unmeasured candidates {sorted(map(str, unmeasured_models))} are not {sorted(UNMEASURED)}"
        )
    for i, row in enumerate(smokes):
        accepted, tasks, p50 = (
            row.get("accepted"),
            row.get("tasks"),
            row.get("p50_seconds"),
        )
        if not isinstance(accepted, int) or isinstance(accepted, bool):
            problems.append(f"smokes[{i}] has no accepted count")
        elif not isinstance(tasks, int) or not 0 <= accepted <= tasks:
            problems.append(f"smokes[{i}] accepted {accepted} is outside 0..{tasks}")
        if tasks != prompts:
            problems.append(
                f"smokes[{i}] ran {tasks} tasks but {prompts} prompts are stored"
            )
        if not isinstance(p50, (int, float)) or isinstance(p50, bool) or p50 <= 0:
            problems.append(f"smokes[{i}] has no positive p50_seconds")
    for i, row in enumerate(unmeasured):
        reason = row.get("reason")
        if not isinstance(reason, str) or "429" not in reason:
            problems.append(
                f"not_measured[{i}] does not record the 429 that stopped it"
            )
    return problems


def _unmeasured_on_ladder(
    receipt: dict[str, Any], ladder_models: set[str]
) -> list[str]:
    return sorted(
        str(row.get("model"))
        for row in receipt.get("not_measured") or []
        if row.get("model") in ladder_models
    )


def _prompt_problems(receipt: dict[str, Any]) -> list[str]:
    prompts = receipt.get("task_prompts") or []
    if len(prompts) != 10:
        return [f"{len(prompts)} task prompts stored, not 10"]
    problems: list[str] = []
    for i, entry in enumerate(prompts):
        for field in ("file", "task_type", "prompt"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"task_prompts[{i}] has no {field}")
    names = [entry.get("file") for entry in prompts]
    if len(set(names)) != len(names):
        problems.append("task prompt names repeat")
    mix = dict(Counter(entry.get("task_type") for entry in prompts))
    if mix != PROMPT_MIX:
        problems.append(f"task mix {mix} is not {PROMPT_MIX}")
    return problems


# --- AC2 -------------------------------------------------------------------------------


def test_no_packaged_routing_placement_or_judge_config_names_the_withdrawn_rung() -> (
    None
):
    files = _packaged_config_files()
    names = {p.name for p in files}
    assert {_ROUTING, _PLACEMENT} <= set(files)
    assert set(JUDGE_CONFIGS) <= names, (
        f"judge configs not scanned: {set(JUDGE_CONFIGS) - names}"
    )
    assert _named_withdrawn(files) == []


def test_the_scan_catches_a_planted_withdrawn_rung_and_refuses_an_unreadable_file(
    tmp_path: Path,
) -> None:
    planted = tmp_path / "bifrost_delegation.yaml"
    planted.write_text(
        "backends:\n  - backend_id: openrouter-north-mini-code\n"
        "    model_name: cohere/north-mini-code:free\n",
        encoding="utf-8",
    )
    judge = tmp_path / "delegation_independent_judge.v1.yaml"
    judge.write_text("judge_backend: openrouter-north-mini-code\n", encoding="utf-8")
    assert {w for _, w in _named_withdrawn([planted])} == set(WITHDRAWN)
    assert _named_withdrawn([judge]) == [(str(judge), "openrouter-north-mini-code")]
    broken = tmp_path / "broken.yaml"
    broken.write_text("backends: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        _named_withdrawn([broken])


def test_the_judge_never_selects_the_withdrawn_rung() -> None:
    judged, withdrawn = _withdrawn_judges(_packaged_backends())
    assert judged > 0, "no producer family selects a judge: the check would be vacuous"
    assert withdrawn == []


def test_the_judge_check_catches_a_planted_withdrawn_judge() -> None:
    planted = {
        "backend_id": "openrouter-north-mini-code",
        "model_name": "cohere/north-mini-code:free",
        "capabilities": [answer_gate_contract()["judge_capability"]],
    }
    _, withdrawn = _withdrawn_judges([planted, *_packaged_backends()])
    assert withdrawn == sorted(WITHDRAWN)


def test_the_tests_that_pinned_the_old_rung_name_the_new_one() -> None:
    rung = _ladder(_packaged_backends(), "cheap_frontier")
    assert [(b["backend_id"], b["model_name"]) for b in rung] == [(NEW_RUNG, NEW_MODEL)]
    assert _pinning_problems(_read(PINNING_TESTS), must_drop_the_old_id=True) == []
    assert (
        _pinning_problems(
            _read(PINNING_TESTS_KEEPING_THE_OLD_ID), must_drop_the_old_id=False
        )
        == []
    )


def test_the_pinning_check_catches_a_test_still_pinning_the_old_rung() -> None:
    old = {"a.py": '_RUNG = "openrouter-north-mini-code"\n'}
    both = {"b.py": f'_RUNG = "{NEW_RUNG}"\n# was cohere/north-mini-code:free\n'}
    assert _pinning_problems(old, must_drop_the_old_id=False) == [
        f"a.py does not name {NEW_RUNG}"
    ]
    assert _pinning_problems(both, must_drop_the_old_id=True) == [
        "b.py still names cohere/north-mini-code:free"
    ]


# --- AC5 -------------------------------------------------------------------------------


def test_every_candidate_row_is_recorded_with_model_accepted_and_p50() -> None:
    receipt = _receipt()
    assert receipt["measured_on"] == "2026-11-20"
    assert _candidate_problems(receipt) == []
    placed = {row["model"]: row for row in receipt["smokes"]}[NEW_MODEL]
    assert placed["accepted"] >= receipt["bar_accepted_of_10"] == 7


def test_no_unmeasured_candidate_is_on_the_ladder() -> None:
    ladder = {b["model_name"] for b in _ladder(_packaged_backends(), "cheap_frontier")}
    assert ladder == {NEW_MODEL}
    assert _unmeasured_on_ladder(_receipt(), ladder) == []


def test_the_ladder_check_catches_an_unmeasured_model_placed() -> None:
    receipt = copy.deepcopy(_receipt())
    receipt["not_measured"][0]["model"] = NEW_MODEL
    assert _unmeasured_on_ladder(receipt, {NEW_MODEL}) == [NEW_MODEL]


def test_the_ten_prompts_are_stored_beside_the_rows() -> None:
    assert _prompt_problems(_receipt()) == []


def _drop(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda receipt: receipt.pop(key)


@pytest.mark.parametrize(
    ("label", "break_receipt", "check"),
    [
        (
            "p50 removed",
            lambda r: r["smokes"][0].pop("p50_seconds"),
            _candidate_problems,
        ),
        (
            "accepted removed",
            lambda r: r["smokes"][0].pop("accepted"),
            _candidate_problems,
        ),
        ("unmeasured rows dropped", _drop("not_measured"), _candidate_problems),
        (
            "unmeasured emptied",
            lambda r: r.update(not_measured=[]),
            _candidate_problems,
        ),
        (
            "reason without the 429",
            lambda r: r["not_measured"][0].update(reason="skipped"),
            _candidate_problems,
        ),
        (
            "measured candidate swapped",
            lambda r: r["smokes"][0].update(model="x/other:free"),
            _candidate_problems,
        ),
        ("prompt removed", lambda r: r["task_prompts"].pop(), _prompt_problems),
        (
            "prompt text blanked",
            lambda r: r["task_prompts"][3].update(prompt=""),
            _prompt_problems,
        ),
        (
            "prompt mix changed",
            lambda r: r["task_prompts"][0].update(task_type="review"),
            _prompt_problems,
        ),
    ],
)
def test_the_receipt_checks_catch_each_planted_gap(
    label: str,
    break_receipt: Callable[[dict[str, Any]], Any],
    check: Callable[[dict[str, Any]], list[str]],
) -> None:
    receipt = copy.deepcopy(_receipt())
    break_receipt(receipt)
    assert check(receipt), label
