# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""render: blind batches, shortening, determinism and the second-judge sample."""

from collections import Counter

import pytest

from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.render_batches import (
    shorten,
)
from tests.nodes.node_delegation_acceptance_judge_compute.builders import (
    EDIT_LOOP_TASK,
    RUBRIC,
    item,
    render,
    render_capped,
)

pytestmark = pytest.mark.unit


def _corpus(per_cell: int = 18) -> list:
    cells = [("model-a", "document"), ("model-a", "test"), ("model-b", "document")]
    return [
        item(f"{model}-{task_type}-{n}", model=model, task_type=task_type)
        for model, task_type in cells
        for n in range(per_cell)
    ]


def test_render_batches_hold_at_most_the_rubric_batch_size() -> None:
    result = render(_corpus())
    primary = [b for b in result.batches if b.role == "primary"]
    assert max(len(b.item_ids) for b in primary) == RUBRIC.batching.max_items_per_batch
    assert sum(len(b.item_ids) for b in primary) == 54
    assert len({i for b in primary for i in b.item_ids}) == 54


def test_render_prompt_is_blind_to_the_model_name() -> None:
    result = render([item("x1", model="Qwen3.8-27B"), item("x2", model="gpt-oss-120b")])
    for batch in result.batches:
        assert "Qwen3.8-27B" not in batch.prompt
        assert "gpt-oss-120b" not in batch.prompt


def test_render_is_deterministic_and_the_seed_changes_the_order() -> None:
    items = _corpus()
    first, second = render(items, "seed-a"), render(items, "seed-a")
    assert first == second
    other = render(items, "seed-b")
    assert [b.item_ids for b in first.batches] != [b.item_ids for b in other.batches]
    assert first.double_judge_item_ids != other.double_judge_item_ids


def test_render_second_judge_sample_takes_ten_percent_with_a_floor_per_cell() -> None:
    result = render(_corpus(18))
    cells = Counter(i.rsplit("-", 1)[0] for i in result.double_judge_item_ids)
    assert set(cells.values()) == {2}
    assert len(result.double_judge_item_ids) == 6
    big = render(_corpus(40))
    big_cells = Counter(i.rsplit("-", 1)[0] for i in big.double_judge_item_ids)
    assert set(big_cells.values()) == {4}


def test_render_sample_never_exceeds_a_small_cell() -> None:
    result = render([item("only", model="m", task_type="test")])
    assert result.double_judge_item_ids == ("only",)


def test_render_secondary_batches_cover_exactly_the_sample() -> None:
    result = render(_corpus())
    secondary = {i for b in result.batches if b.role == "secondary" for i in b.item_ids}
    assert secondary == set(result.double_judge_item_ids)
    primary = {i for b in result.batches if b.role == "primary" for i in b.item_ids}
    assert secondary <= primary


def test_render_shortens_a_long_task_and_says_so() -> None:
    long_task = "a" * 20000 + "b" * 20000
    result = render([item("big", task=long_task)])
    prompt = result.batches[0].prompt
    assert "characters omitted" in prompt
    assert "the task was shortened for judging (10000 characters omitted)" in prompt
    assert len(prompt) < 40000


def test_render_shorten_keeps_head_and_tail() -> None:
    text = "H" * 10 + "m" * 100 + "T" * 5
    shown, omitted = shorten(text, cap=50, head=10, tail=5)
    assert omitted == 100
    assert shown.startswith("H" * 10)
    assert shown.endswith("T" * 5)
    assert shorten("short", cap=50, head=10, tail=5) == ("short", 0)


def test_render_marks_an_edit_loop_turn_from_its_opening_line() -> None:
    result = render([item("e1", task_type="code_generation", task=EDIT_LOOP_TASK)])
    assert "kind: edit_loop_turn" in result.batches[0].prompt


def test_render_refuses_duplicate_ids_and_a_probe_only_corpus() -> None:
    with pytest.raises(ValueError, match="unique"):
        render([item("a"), item("a")])
    with pytest.raises(ValueError, match="every item is a probe"):
        render([item("p", task="say ok")])


def test_render_needs_a_seed() -> None:
    with pytest.raises(ValueError, match="seed"):
        render([item("a")], seed="")


def test_render_prompt_asks_for_the_json_shape() -> None:
    prompt = render([item("a")]).batches[0].prompt
    assert '"judgments"' in prompt
    assert "ITEMS (1):" in prompt
    assert "=== ITEM a | routing class: document ===" in prompt


def test_render_cell_cap_keeps_the_first_n_per_cell_in_seeded_order() -> None:
    items = _corpus(30)
    result = render_capped(items, cap=18)
    kept = {i for b in result.batches if b.role == "primary" for i in b.item_ids}
    assert len(kept) == 54
    assert len(result.dropped_item_ids) == 90 - 54
    assert kept.isdisjoint(result.dropped_item_ids)
    assert render_capped(items, cap=18) == result
    assert render_capped(items, cap=0).dropped_item_ids == ()


def test_render_cell_cap_counts_edit_loop_turns_as_their_own_cell() -> None:
    items = [
        *[item(f"t{n}", task_type="code_generation") for n in range(5)],
        *[
            item(f"e{n}", task_type="code_generation", task=EDIT_LOOP_TASK)
            for n in range(5)
        ],
    ]
    result = render_capped(items, cap=3)
    kept = {i for b in result.batches if b.role == "primary" for i in b.item_ids}
    assert sum(i.startswith("t") for i in kept) == 3
    assert sum(i.startswith("e") for i in kept) == 3
