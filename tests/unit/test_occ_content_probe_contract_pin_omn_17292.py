# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-proven content-bound candidate for a bot contract-pin advance (OMN-17292).

The omnimarket-contract-pin-refresh bot PR in omnibase_infra rewrites one line
of ``.github/omnimarket-contract-pin.yaml`` (omnibase_infra#4278, #4249, #4228).
It carries no Python declaration, no uv.lock line and no release artefact, so
autobind declined every one (``occ-autobind-no-red-derivable``) and each needed
a hand-authored companion (OCC#11783, OCC#11741). The claim is falsifiable: the
new 40-hex ``omnimarket_contract_ref`` is present at head and absent at the
merge base. Same RED/GREEN bar as every other grammar; nothing is exempted, and
it is offered only for a pin-advance diff. Zero network.
"""

from __future__ import annotations

import pytest

from omnimarket.occ_content_probe import (
    SymbolCandidate,
    build_content_read_check,
    describe_uncandidated_path,
    extract_contract_pin_candidates,
    is_contract_pin_advance_diff,
    select_asserted_check,
)

pytestmark = pytest.mark.unit

_PIN = ".github/omnimarket-contract-pin.yaml"
_REPO = "OmniNode-ai/omnibase_infra"
# omnibase_infra#4278, real refs.
_HEAD = "09bad4c9a3d6d60246cc5966b4d09afed1e08ec8"
_BASE = "40865946e02646bf2178b9e61102a3669f2a8a79"
_OLD_REF = "67cae0fe3b806a3b738ca618d555e0046fdf7841"
_NEW_REF = "29dc6ae208db4c0e5a1f5b1b6a3f0a7d9e8c1b2a"

_PIN_BASE = (
    "# OMN-17292: the omnimarket contract set.\n"
    "repository: OmniNode-ai/omnimarket\n"
    f"omnimarket_contract_ref: {_OLD_REF}\n"
)
_PIN_HEAD = _PIN_BASE.replace(_OLD_REF, _NEW_REF)


class TestIsContractPinAdvanceDiff:
    def test_pin_file_alone(self) -> None:
        assert is_contract_pin_advance_diff([_PIN])

    def test_pin_with_derived_outputs(self) -> None:
        assert is_contract_pin_advance_diff(
            [
                _PIN,
                "src/omnibase_infra/topology/instances/onex_dev.yaml",
                "docker/catalog/database-topology/x.json",
            ]
        )

    def test_any_other_path_disqualifies(self) -> None:
        assert not is_contract_pin_advance_diff(
            [_PIN, "src/omnibase_infra/topology/table_grant_derivation.py"]
        )
        assert not is_contract_pin_advance_diff([_PIN, ".github/workflows/ci.yml"])

    def test_derived_outputs_without_the_pin_are_not_a_pin_advance(self) -> None:
        assert not is_contract_pin_advance_diff(
            ["src/omnibase_infra/topology/instances/onex_dev.yaml"]
        )

    def test_empty_is_not_a_pin_advance(self) -> None:
        assert not is_contract_pin_advance_diff([])


class TestExtractContractPinCandidates:
    def test_yields_the_new_ref(self) -> None:
        assert extract_contract_pin_candidates(
            path=_PIN, head_content=_PIN_HEAD, base_content=_PIN_BASE
        ) == (SymbolCandidate(path=_PIN, kind="text_line", symbol=_NEW_REF),)

    def test_unchanged_pin_yields_nothing(self) -> None:
        assert (
            extract_contract_pin_candidates(
                path=_PIN, head_content=_PIN_BASE, base_content=_PIN_BASE
            )
            == ()
        )

    def test_other_files_yield_nothing(self) -> None:
        assert (
            extract_contract_pin_candidates(
                path="README.md", head_content=_PIN_HEAD, base_content=_PIN_BASE
            )
            == ()
        )

    def test_non_sha_value_is_never_proposed(self) -> None:
        head = _PIN_BASE.replace(_OLD_REF, "main")
        assert (
            extract_contract_pin_candidates(
                path=_PIN, head_content=head, base_content=_PIN_BASE
            )
            == ()
        )

    def test_none_head_yields_nothing(self) -> None:
        assert (
            extract_contract_pin_candidates(
                path=_PIN, head_content=None, base_content=_PIN_BASE
            )
            == ()
        )


class TestDryRunAgainstPr4278:
    def test_selected_check_is_red_at_base_and_green_at_head(self) -> None:
        candidates = extract_contract_pin_candidates(
            path=_PIN, head_content=_PIN_HEAD, base_content=_PIN_BASE
        )
        contents = {(_PIN, _HEAD): _PIN_HEAD, (_PIN, _BASE): _PIN_BASE}
        check = select_asserted_check(
            candidates,
            repo=_REPO,
            head_sha=_HEAD,
            base_sha=_BASE,
            fetch_content=lambda p, r: contents.get((p, r)),
        )
        assert check == build_content_read_check(
            repo=_REPO, path=_PIN, kind="text_line", symbol=_NEW_REF, head_sha=_HEAD
        )
        assert _NEW_REF in _PIN_HEAD
        assert _NEW_REF not in _PIN_BASE


def test_pin_file_decline_names_the_pin_grammar_not_a_generic_one() -> None:
    reason = describe_uncandidated_path(
        path=_PIN,
        status="modified",
        patch_present=True,
        release_only_diff=False,
        head_readable=True,
    )
    assert "no candidate grammar reads this file type" not in reason
    assert "contract" in reason
