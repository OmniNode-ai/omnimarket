# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden tests for node_context_pack_render_compute (OMN-21109, plan task TA.7).

Each test names the acceptance criterion it binds (AC1 to AC7 of the ticket).
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.models.context_pack_render import (
    EnumContextPackRenderFailure,
    EnumContextPackRenderStatus,
    ModelContextPackCarriedPrompt,
    ModelContextPackRenderChunkRef,
    ModelContextPackRenderRequest,
    ModelContextPackRenderResult,
)
from omnimarket.nodes.node_context_pack_render_compute.handlers.handler_context_pack_render import (
    HandlerContextPackRender,
    is_stable_factor,
    provenance_label,
)
from tests.nodes.node_context_pack_render_compute.conftest import (
    INVARIANT_TEXT,
    LANE_RULES_TEXT,
    OPEN_WORK_TEXT,
    RULING_TEXT,
    TAIL_TEXT,
    WORDS_TEXT,
    Fixture,
    chunk,
    identity,
    pack,
    prompts,
    ref,
)

pytestmark = pytest.mark.unit

LEGEND_LABELS = (
    "ledger row",
    "operator quote",
    "registry or document excerpt",
    "observed state",
    "verified extract",
    "model output",
)

GOLDEN_TRAILER = "\n".join(
    [
        "identity trailer",
        "candidate_id: cand-aaaa",
        "assembly_hash: asm-bbbb",
        "final_pack_hash: fin-cccc",
        "version_vector_hash: vec-dddd",
        "profile: compaction_orchestrator@1",
        "generated_at: 2026-10-15T14:14:59Z",
        "unrecorded_prompts: 7",
        "reviewed_prompts: 11",
        "scrubbed_spans: 13",
        "transcript_extract_candidates: 17",
        "uncited_prompt_ids: p-1, p-3, p-5",
    ]
)

GOLDEN_LEGEND = "\n".join(
    [
        "Context pack, render contract context-pack-render/1.",
        "Each block opens with a header, [factor: provenance] and the block's source.",
        "Provenance labels:",
        "- ledger row: a work-ledger row quoted in full, referenced by its timestamp and lane.",
        "- operator quote: operator words verbatim as captured.",
        "- registry or document excerpt: text copied verbatim from a pinned source and cited path@sha.",
        "- observed state: state read from a system at a pinned point, not authored.",
        "- verified extract: a span a model proposed and a deterministic verifier admitted as a "
        "verbatim quote of captured operator words; a candidate, never a ruling.",
        "- model output: text a model wrote; not verbatim and not evidence.",
        "Blocks run stable first, then volatile, then model output. "
        "The last block is the identity trailer.",
    ]
)

GOLDEN_BODY_HASH = "1eaeaee169b707379f1b0cbee0e7f609062dc20ee5162d80d1e5c3192d100c70"
GOLDEN_STABLE_PREFIX_HASH = (
    "76f4a88435a9340ad643f7bb499203dbba4931af14a31b6a33eace1d89602829"
)


def _render(request: ModelContextPackRenderRequest) -> ModelContextPackRenderResult:
    return HandlerContextPackRender().handle(request)


def _ok(request: ModelContextPackRenderRequest) -> ModelContextPackRenderResult:
    result = _render(request)
    assert result.status is EnumContextPackRenderStatus.OK, result.errors
    return result


def _texts(result: ModelContextPackRenderResult) -> list[str]:
    return [m.text for m in result.messages]


def test_the_render_is_the_golden_message_list() -> None:
    """AC1, AC2: the whole message list for the fixture pack, byte for byte."""
    result = _ok(Fixture().request())
    assert [m.role for m in result.messages] == ["user"] * 7
    texts = _texts(result)
    assert texts[0] == GOLDEN_LEGEND
    assert texts[1:] == [
        "[lane_rules: registry or document excerpt] "
        "lane_rules/lane-rules-block.md@1111aaa\n" + LANE_RULES_TEXT,
        "[invariants: registry or document excerpt] "
        "reference/invariants.yaml@2222bbb#INV-005:12-14\n" + INVARIANT_TEXT,
        "[operator_rulings: ledger row] 2026-10-15T08:14:15Z "
        "lane=orchestrator-bb1a\n" + RULING_TEXT,
        "[open_work: ledger row] sha256:open_work\n" + OPEN_WORK_TEXT,
        "[operator_words_unrecorded: operator quote] "
        "sha256:operator_words_unrecorded\n" + WORDS_TEXT,
        GOLDEN_TRAILER,
    ]


def test_same_pack_same_body_hash() -> None:
    """AC1: one pack rendered twice gives one body_hash, pinned as a golden value."""
    first = _ok(Fixture().request())
    second = _ok(Fixture().request())
    assert first.body_hash == second.body_hash
    assert first.model_dump() == second.model_dump()
    assert first.body_hash == GOLDEN_BODY_HASH


def test_a_changed_pack_changes_the_body_hash() -> None:
    """AC1 control: the golden hash is not a constant."""
    fixture = Fixture()
    changed = fixture.request(identity=identity(candidate_id="cand-other"))
    assert _ok(changed).body_hash != GOLDEN_BODY_HASH


def test_the_legend_is_the_first_block_and_labels_every_provenance() -> None:
    """AC2: the fixed legend labels each factor's provenance."""
    legend = _ok(Fixture().request()).messages[0].text
    for label in LEGEND_LABELS:
        assert label in legend
    assert "path@sha" in legend


def test_the_legend_does_not_depend_on_the_pack() -> None:
    """AC2, AC5: the legend is constant text per render-contract version."""
    first = _ok(Fixture().request())
    second = _ok(
        Fixture().request(
            pack=pack((Fixture().lane_rules,), "2030-01-01T00:00:00Z"),
            chunk_refs=(Fixture().refs[0],),
            identity=identity(candidate_id="y", unrecorded_prompt_count=1),
        )
    )
    assert first.messages[0] == second.messages[0]


def test_the_identity_trailer_is_last_and_golden() -> None:
    """AC2: the trailer is the last block and names every identity field."""
    result = _ok(Fixture().request())
    assert result.messages[-1].text == GOLDEN_TRAILER


def test_the_trailer_names_the_compact_route_only_with_a_model_written_block() -> None:
    """AC2: the routed compact model rides in the trailer when a model section is present."""
    fixture = Fixture()
    with_tail = pack((fixture.lane_rules, fixture.tail))
    refs = (fixture.refs[0],)
    missing = _render(fixture.request(pack=with_tail, chunk_refs=refs))
    assert missing.status is EnumContextPackRenderStatus.FAILED
    assert (
        missing.failure_class
        is EnumContextPackRenderFailure.MODEL_ROUTE_IDENTITY_MISSING
    )
    routed = _ok(
        fixture.request(
            pack=with_tail,
            chunk_refs=refs,
            identity=identity(compact_route_identity="context_compaction@3"),
        )
    )
    assert "compact_route: context_compaction@3" in routed.messages[-1].text
    assert "compact_route" not in _ok(Fixture().request()).messages[-1].text


def test_the_trailer_lists_uncited_prompts_in_capture_order() -> None:
    """AC3: uncited carried prompts by id, capture order, none cited or reviewed."""
    shuffled = tuple(reversed(prompts()))
    trailer = _ok(Fixture().request(carried_prompts=shuffled)).messages[-1].text
    assert trailer.splitlines()[-1] == "uncited_prompt_ids: p-1, p-3, p-5"
    for gone in ("p-2", "p-4"):
        assert gone not in trailer


def test_the_trailer_says_none_when_every_prompt_is_cited_or_reviewed() -> None:
    """AC3 control: an empty list is stated, not omitted."""
    only_closed = (
        ModelContextPackCarriedPrompt(prompt_id="p-9", capture_sequence=1, cited=True),
        ModelContextPackCarriedPrompt(
            prompt_id="p-10", capture_sequence=2, in_reviewed_range=True
        ),
    )
    trailer = _ok(Fixture().request(carried_prompts=only_closed)).messages[-1].text
    assert trailer.splitlines()[-1] == "uncited_prompt_ids: none"


def test_a_prompt_id_appearing_twice_is_refused() -> None:
    """AC3: ids are the operator's review handles; a duplicate is ambiguous."""
    twice = (
        ModelContextPackCarriedPrompt(prompt_id="p-1", capture_sequence=1),
        ModelContextPackCarriedPrompt(prompt_id="p-1", capture_sequence=2),
    )
    with pytest.raises(ValueError, match="p-1"):
        Fixture().request(carried_prompts=twice)


def test_a_ruling_renders_its_row_reference_and_full_text() -> None:
    """AC4: timestamp and lane in the header, the row text unaltered."""
    message = _ok(Fixture().request()).messages[3].text
    header, _, body = message.partition("\n")
    assert (
        header
        == "[operator_rulings: ledger row] 2026-10-15T08:14:15Z lane=orchestrator-bb1a"
    )
    assert body == RULING_TEXT


@pytest.mark.parametrize("missing", ["row_timestamp", "row_lane"])
def test_a_ruling_without_its_timestamp_or_lane_is_refused(missing: str) -> None:
    """AC4 falsifier: a ruling is never rendered without its timestamp and lane."""
    fixture = Fixture()
    reference = {
        "chunk_id": fixture.ruling.chunk_id,
        "row_timestamp": "2026-10-15T08:14:15Z",
        "row_lane": "orchestrator-bb1a",
    }
    del reference[missing]
    bad = ModelContextPackRenderChunkRef.model_validate(reference)
    result = _render(
        fixture.request(chunk_refs=(fixture.refs[0], fixture.refs[1], bad))
    )
    assert result.status is EnumContextPackRenderStatus.FAILED
    assert result.failure_class is EnumContextPackRenderFailure.MISSING_RULING_REFERENCE
    assert result.messages == ()


def test_two_packs_over_the_same_stable_sources_share_the_stable_prefix() -> None:
    """AC5: different generated_at, ledger rows and tail, same prefix bytes and hash."""
    fixture = Fixture()
    first = _ok(fixture.request())
    other_ruling = chunk(EnumContextFactor.OPERATOR_RULINGS, "another row")
    other_tail = chunk(EnumContextFactor.CONVERSATION_TAIL, "a different tail")
    second_pack = pack(
        (fixture.lane_rules, fixture.invariant, other_ruling, other_tail),
        generated_at="2031-02-03T04:05:06Z",
    )
    second = _ok(
        fixture.request(
            pack=second_pack,
            chunk_refs=(
                fixture.refs[0],
                fixture.refs[1],
                ref(other_ruling, row_timestamp="2030-01-01T00:00:00Z", row_lane="l2"),
            ),
            identity=identity(candidate_id="cand-zzzz", assembly_hash="asm-zzzz"),
        )
    )
    assert first.stable_prefix_hash == second.stable_prefix_hash
    assert first.stable_prefix_message_count == second.stable_prefix_message_count == 3
    n = first.stable_prefix_message_count
    assert first.messages[:n] == second.messages[:n]
    assert first.body_hash != second.body_hash
    assert first.stable_prefix_hash == GOLDEN_STABLE_PREFIX_HASH


def test_no_per_build_value_precedes_the_last_stable_block() -> None:
    """AC6: per-build values live in the trailer only; stable blocks keep their source ids."""
    result = _ok(Fixture().request())
    prefix = _texts(result)[: result.stable_prefix_message_count]
    per_build = (
        "cand-aaaa",
        "asm-bbbb",
        "fin-cccc",
        "vec-dddd",
        "2026-10-15T14:14:59Z",
        "unrecorded_prompts",
        "reviewed_prompts",
        "scrubbed_spans",
        "transcript_extract_candidates",
        "compaction_orchestrator",
    )
    for message in prefix:
        for value in per_build:
            assert value not in message
    assert "lane_rules/lane-rules-block.md@1111aaa" in prefix[1]
    assert "reference/invariants.yaml@2222bbb#INV-005:12-14" in prefix[2]
    for value in per_build[:5]:
        assert value in result.messages[-1].text


def test_a_stable_block_without_its_source_identifier_is_refused() -> None:
    """AC6 falsifier: a stable block is never rendered without path@sha."""
    fixture = Fixture()
    result = _render(fixture.request(chunk_refs=(fixture.refs[2],)))
    assert result.status is EnumContextPackRenderStatus.FAILED
    assert (
        result.failure_class is EnumContextPackRenderFailure.MISSING_SOURCE_IDENTIFIER
    )
    assert "lane_rules" in result.errors[0]


def test_a_pinned_commit_change_changes_exactly_the_blocks_of_that_source() -> None:
    """AC7: the lane-rules source moves to a new commit; no other block moves."""
    fixture = Fixture()
    before = _ok(fixture.request())
    moved = chunk(EnumContextFactor.LANE_RULES, LANE_RULES_TEXT + "\nOne more rule.")
    after = _ok(
        fixture.request(
            pack=pack(
                (
                    moved,
                    fixture.invariant,
                    fixture.ruling,
                    fixture.open_work,
                    fixture.words,
                )
            ),
            chunk_refs=(
                ref(moved, "lane_rules/lane-rules-block.md@9999fff"),
                fixture.refs[1],
                fixture.refs[2],
            ),
        )
    )
    changed = [
        i
        for i, (a, b) in enumerate(zip(before.messages, after.messages, strict=True))
        if a != b
    ]
    assert changed == [1]
    assert "@9999fff" in after.messages[1].text
    assert "@1111aaa" not in after.messages[1].text
    assert before.stable_prefix_hash != after.stable_prefix_hash


def test_blocks_render_stable_then_volatile_then_model_output_whatever_the_pack_order() -> (
    None
):
    """Section 4.6 item 1: the legend, stable, volatile, model output, then the trailer."""
    fixture = Fixture()
    scrambled = pack(
        (
            fixture.tail,
            fixture.words,
            fixture.invariant,
            fixture.open_work,
            fixture.lane_rules,
        )
    )
    result = _ok(
        fixture.request(
            pack=scrambled,
            chunk_refs=(fixture.refs[0], fixture.refs[1]),
            identity=identity(compact_route_identity="context_compaction@3"),
        )
    )
    headers = [t.split("\n", 1)[0].split("]")[0] for t in _texts(result)[1:-1]]
    assert headers == [
        "[invariants: registry or document excerpt",
        "[lane_rules: registry or document excerpt",
        "[operator_words_unrecorded: operator quote",
        "[open_work: ledger row",
        "[tail_summary: model output",
    ]
    assert result.messages[-1].text.startswith("identity trailer")
    assert TAIL_TEXT in result.messages[-2].text


def test_every_context_factor_has_a_provenance_and_a_stability_class() -> None:
    """A factor core adds later must fail this test, not render unlabelled."""
    for factor in EnumContextFactor:
        assert provenance_label(factor) in LEGEND_LABELS, factor


def test_the_stable_factors_are_the_plan_rows_one_to_five() -> None:
    """Plan section 4.2: rows 1 to 5 are the cached prefix."""
    stable = {f for f in EnumContextFactor if is_stable_factor(f)}
    assert stable == {
        EnumContextFactor.LANE_RULES,
        EnumContextFactor.INVARIANTS,
        EnumContextFactor.ADRS,
        EnumContextFactor.CANONICAL_SHAPES,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
        EnumContextFactor.EXEMPLAR,
    }


def test_a_chunk_ref_for_a_chunk_not_in_the_pack_is_refused() -> None:
    """A stray reference is a caller error, not something to drop silently."""
    fixture = Fixture()
    stray = ref(chunk(EnumContextFactor.ADRS, "not in the pack"), "adrs/x.md@abc")
    result = _render(fixture.request(chunk_refs=(*fixture.refs, stray)))
    assert result.status is EnumContextPackRenderStatus.FAILED
    assert result.failure_class is EnumContextPackRenderFailure.UNKNOWN_CHUNK_REF
