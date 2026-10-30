# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20074: a downstream repository's core pin bump is pin-only.

Its ``check-handshake.yml`` pins omnibase_core through an env var
(``OMNIBASE_CORE_COMMIT: <sha>``, read by ``ref: ${{ env.OMNIBASE_CORE_COMMIT }}``),
the pin site omnibase_core's ``docs/downstream-repos.yaml`` declares for it. The
OMN-9050 bump moves that line and the "Auto-bumped by" banner and nothing else.
``classify_workflow_core_pin_only`` normalised only an inline checkout ``ref:``,
so it refused three real bump pull requests ("changes outside the
omnibase_core checkout pin and its banner") and both dod-verify gates failed.

The fixtures are the real bump diff. The refusals keep the exemption narrow:
only the ``OMNIBASE_CORE_COMMIT`` key, only a 40-hex value, the trailing comment
unchanged, and no other line moving.
"""

from __future__ import annotations

import pytest

from omnimarket.occ_content_probe import classify_workflow_core_pin_only

pytestmark = pytest.mark.unit

OLD = "35664cc30ca4bab02da4cac1d55786623db9a452"
NEW = "f99cc10f48a129837127e63a3fe79016ac41ad5a"
OTHER = "9b083ad32228aa1d1d6a3c1ab0b2f1c1a5e0d4c7"
HS = ".github/workflows/check-handshake.yml"


def _handshake(
    sha: str,
    *,
    key: str = "OMNIBASE_CORE_COMMIT",
    tail: str = " # pinned",
    timeout: int = 5,
) -> str:
    return (
        "name: Check Architecture Handshake\n"
        "jobs:\n"
        "  check-handshake:\n"
        "    name: Check architecture handshake\n"
        "    runs-on: ${{ fromJSON(vars.OMNI_INFRA_FLEET_RUNS_ON_JSON) }}\n"
        f"    timeout-minutes: {timeout}\n"
        "    env:\n"
        f"      # Auto-bumped by omnibase_core publish-downstream-pin-bump.yml to {sha[:12]}.\n"
        f"      {key}: {sha}{tail}\n"
        "\n"
        "    steps:\n"
        "      - name: Checkout repo\n"
        "        uses: actions/checkout@v7\n"
        "\n"
        "      - name: Checkout omnibase_core\n"
        "        uses: actions/checkout@v7\n"
        "        with:\n"
        "          repository: OmniNode-ai/omnibase_core\n"
        "          ref: ${{ env.OMNIBASE_CORE_COMMIT }}\n"
        "          path: omnibase_core\n"
    )


def _classify(head: str, base: str) -> tuple[bool, str]:
    return classify_workflow_core_pin_only([HS], contents={HS: (head, base)})


def test_downstream_env_core_pin_bump_is_admitted() -> None:
    ok, reason = _classify(_handshake(NEW), _handshake(OLD))
    assert ok, reason
    assert NEW[:12] in reason
    assert HS in reason


def test_env_core_pin_without_trailing_comment_is_admitted() -> None:
    ok, reason = _classify(_handshake(NEW, tail=""), _handshake(OLD, tail=""))
    assert ok, reason


def test_env_pin_under_another_key_is_refused() -> None:
    ok, reason = _classify(
        _handshake(NEW, key="OMNIBASE_INFRA_COMMIT"),
        _handshake(OLD, key="OMNIBASE_INFRA_COMMIT"),
    )
    assert not ok
    assert "changes outside" in reason


def test_env_core_pin_with_a_changed_trailing_comment_is_refused() -> None:
    ok, reason = _classify(_handshake(NEW, tail=""), _handshake(OLD))
    assert not ok
    assert "changes outside" in reason


def test_env_core_pin_moved_to_a_non_sha_is_refused() -> None:
    ok, reason = _classify(
        _handshake(OLD).replace(
            f"OMNIBASE_CORE_COMMIT: {OLD}", "OMNIBASE_CORE_COMMIT: dev"
        ),
        _handshake(OLD),
    )
    assert not ok
    assert "changes outside" in reason


def test_env_core_pin_beside_another_edit_is_refused() -> None:
    ok, reason = _classify(_handshake(NEW, timeout=30), _handshake(OLD))
    assert not ok
    assert "changes outside" in reason


def test_env_core_pin_and_checkout_ref_naming_two_shas_are_refused() -> None:
    def with_inline_ref(text: str, sha: str) -> str:
        return text + (
            "      - uses: actions/checkout@v7\n"
            "        with:\n"
            "          repository: OmniNode-ai/omnibase_core\n"
            f"          ref: {sha}\n"
        )

    ok, reason = _classify(
        with_inline_ref(_handshake(NEW), OTHER), with_inline_ref(_handshake(OLD), OLD)
    )
    assert not ok
    assert "do not name one SHA" in reason


def test_unmoved_env_core_pin_is_refused() -> None:
    ok, reason = _classify(_handshake(OLD), _handshake(OLD))
    assert not ok
    assert "no omnibase_core checkout pin moved" in reason
