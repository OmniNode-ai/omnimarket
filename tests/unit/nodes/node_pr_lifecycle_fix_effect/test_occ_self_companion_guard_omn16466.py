# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two defects behind the OCC#10360 / OCC#10365 red autobind checks (OMN-16466).

Both were diagnosed live on the lab dev lane on 2026-09-19 and neither had an
owner. They are independent, and each is pinned separately here.

**Defect 1 — git's stderr is captured and then never rendered.**
:func:`omnimarket.occ_git_transport.run_git` re-raises a
:class:`subprocess.CalledProcessError` carrying credential-scrubbed ``output``
and ``stderr``, so git's own message IS on the exception object. Every consumer
rendered ``str(exc)``, and ``CalledProcessError.__str__`` prints only the argv
and the exit status. The ERROR outcome posted on a product PR therefore said
``returned non-zero exit status 1`` and nothing about WHY — which is why the
same question could not be closed twice in a row: the only other copy of the
string lives in a container that is replaced on every lane deploy.

**Defect 2 — the self-companion decline exists on one path only.**
``compute_companion_plan`` declines an OCC-internal PR with
:attr:`EnumCompanionSuppressionCode.OCC_SELF_COMPANION` (OMN-16440), and the
companion EFFECT logs that decline cleanly. The autobind path through
:class:`OccCompanionEmitter` carried no such branch: for the same PR, one second
later, it entered the authoring path and died inside an unguarded ``git
commit``. A deliberate policy decision surfaced as a red infrastructure ERROR on
a product PR whose evidence was already complete.

Live specimens, read from the product PRs' own check runs rather than from a
container log: ``onex_change_control#10360`` correlation
``4ade72d5-be61-477f-9e7f-48b78c7feb3d`` at 2026-09-19T11:42:39Z,
``onex_change_control#10365`` correlation
``0df6e365-4b48-4fba-b6cf-62e9bfad5b03`` at 2026-09-19T13:34:03Z.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from omnimarket.events.occ_companion import EnumCompanionSuppressionCode
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.occ_git_transport import (
    PROCESS_OUTPUT_RENDER_LIMIT,
    format_process_error,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

# The literal git emits for the leading hypothesis behind both specimens: a
# self-companion has nothing to add, so the staged tree is empty.
_NOTHING_TO_COMMIT = (
    "On branch occ-companion-onex-change-control-10365\n"
    "nothing to commit, working tree clean\n"
)


def _commit_failure(
    stderr: str = _NOTHING_TO_COMMIT, stdout: str = ""
) -> subprocess.CalledProcessError:
    """The exact exception shape ``run_git`` re-raises for the live specimens."""
    return subprocess.CalledProcessError(
        1,
        ["git", "commit", "-m", "evidence(OMN-18426): author OCC companion"],
        output=stdout or None,
        stderr=stderr or None,
    )


class _RaisingAutobindAdapter:
    """An autobind adapter that fails exactly the way the live path failed."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        raise self._exc


def _autobind_command() -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=10365,
        repo="OmniNode-ai/onex_change_control",
        block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
        ticket_id="OMN-18426",
        requested_at=datetime.now(tz=UTC),
    )


def _handler(exc: BaseException) -> HandlerPrLifecycleFix:
    # outcome_token_resolver -> None keeps the OMN-18069 reporter a no-op: this
    # suite is about what the reason STRING says, not about posting it.
    return HandlerPrLifecycleFix(
        occ_autobind_adapter=_RaisingAutobindAdapter(exc),
        outcome_token_resolver=lambda: None,
    )


# ---------------------------------------------------------------------------
# Defect 1 — the captured stderr reaches the surfaces a human reads
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGitStderrIsRendered:
    def test_formatter_renders_stderr_that_str_exc_drops(self) -> None:
        exc = _commit_failure()

        # The premise, asserted rather than assumed: the default rendering
        # really does drop the string, so this is a renderer fix and not a
        # capture fix.
        assert "nothing to commit" not in str(exc)

        detail = format_process_error(exc)
        assert "nothing to commit, working tree clean" in detail
        assert "returned non-zero exit status 1" in detail

    def test_formatter_renders_stdout_too(self) -> None:
        detail = format_process_error(
            _commit_failure(stderr="", stdout="nothing added to commit")
        )
        assert "nothing added to commit" in detail

    def test_formatter_bounds_each_stream(self) -> None:
        detail = format_process_error(_commit_failure(stderr="x" * 50_000))
        assert len(detail) < PROCESS_OUTPUT_RENDER_LIMIT * 3
        assert "truncated" in detail

    def test_formatter_scrubs_an_embedded_credential(self) -> None:
        detail = format_process_error(
            _commit_failure(
                stderr=(
                    "fatal: could not read from "
                    "https://x-access-token:ghs_SUPERSECRETVALUE@github.com/o/r"
                )
            )
        )
        assert "ghs_SUPERSECRETVALUE" not in detail
        assert "x-access-token" in detail

    def test_formatter_is_a_no_op_for_a_plain_exception(self) -> None:
        assert format_process_error(ValueError("plain")) == "plain"

    def test_formatter_handles_a_timeout(self) -> None:
        exc = subprocess.TimeoutExpired(
            ["git", "push"], 300.0, output=None, stderr="remote hung up"
        )
        assert "remote hung up" in format_process_error(exc)

    async def test_handler_error_surface_carries_the_stderr(self) -> None:
        result = await _handler(_commit_failure()).handle(_autobind_command())

        assert result.error is not None
        # Both surfaces: `error` is what the result model carries onto the bus,
        # `fix_action` is what the OMN-18069 outcome check-run renders as its
        # machine-readable reason. The live specimens were undiagnosable because
        # neither carried this string.
        assert "nothing to commit, working tree clean" in result.error
        assert "nothing to commit, working tree clean" in result.fix_action
        assert result.fix_action.startswith("failed: ")

    async def test_handler_never_leaks_a_credential_into_the_reason(self) -> None:
        result = await _handler(
            _commit_failure(
                stderr="https://x-access-token:ghs_LEAKED@github.com/OmniNode-ai/x"
            )
        ).handle(_autobind_command())

        assert result.error is not None
        assert "ghs_LEAKED" not in result.error
        assert "ghs_LEAKED" not in result.fix_action


# ---------------------------------------------------------------------------
# Defect 2 — the autobind path declines a self-companion before it mutates
# ---------------------------------------------------------------------------


def _tripwired_emitter(
    occ_repo: str = "OmniNode-ai/onex_change_control",
) -> OccCompanionEmitter:
    return OccCompanionEmitter(occ_repo=occ_repo)


def _no_side_effects(emitter: OccCompanionEmitter) -> Any:
    """Every mutating and network seam raises, so a decline proves zero I/O."""

    def _boom(name: str) -> Any:
        def _raise(*_a: object, **_k: object) -> None:
            raise AssertionError(f"self-companion must not reach {name}")

        return _raise

    return (
        patch(f"{_MOD}.rest_json", side_effect=_boom("the GitHub REST API")),
        patch(
            f"{_MOD}._resolve_github_token",
            side_effect=_boom("the credential resolver"),
        ),
        patch.object(emitter, "_clone_and_branch", side_effect=_boom("git clone")),
        patch.object(emitter, "_run_git", side_effect=_boom("git")),
    )


@pytest.mark.unit
class TestSelfCompanionDeclinedOnTheAutobindPath:
    @pytest.mark.parametrize(
        "repo",
        [
            "OmniNode-ai/onex_change_control",
            # GitHub slugs are case-insensitive and the seam carries whatever
            # the caller wrote, so the compute path compares casefolded. Parity.
            "OmniNode-ai/ONEX_Change_Control",
            "  OmniNode-ai/onex_change_control  ",
        ],
    )
    def test_declines_with_zero_side_effects(self, repo: str) -> None:
        emitter = _tripwired_emitter()
        patches = _no_side_effects(emitter)

        with patches[0], patches[1], patches[2], patches[3]:
            action = emitter._emit_companion_sync(repo, 10365, "OMN-18426")

        assert action.startswith("skip:OCC_SELF_COMPANION"), action
        # The decline names the same machine-readable code the companion-effect
        # path already prints, derived from the enum so the two cannot drift.
        assert EnumCompanionSuppressionCode.OCC_SELF_COMPANION.value in action
        assert "10365" in action

    def test_decline_token_is_derived_from_the_shared_enum(self) -> None:
        emitter = _tripwired_emitter()
        patches = _no_side_effects(emitter)

        with patches[0], patches[1], patches[2], patches[3]:
            action = emitter._emit_companion_sync(
                "OmniNode-ai/onex_change_control", 1, None
            )

        expected = EnumCompanionSuppressionCode.OCC_SELF_COMPANION.value.upper()
        assert action.startswith(f"skip:{expected}")

    def test_guard_follows_the_configured_occ_repo_not_a_literal(self) -> None:
        # A deployment pointing the seam at a different OCC repo suppresses ITS
        # own PRs, and stops suppressing the default one.
        emitter = _tripwired_emitter(occ_repo="OmniNode-ai/some_other_occ")
        patches = _no_side_effects(emitter)

        with patches[0], patches[1], patches[2], patches[3]:
            action = emitter._emit_companion_sync("OmniNode-ai/some_other_occ", 7, None)
        assert action.startswith("skip:OCC_SELF_COMPANION"), action

    def test_a_product_pr_is_not_declined_by_this_branch(self) -> None:
        # Positive control for the zero above: the same tripwires prove the
        # ordinary path still reaches the PR fetch, so the decline is not
        # swallowing every emit.
        emitter = _tripwired_emitter()
        with (
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            patch(
                f"{_MOD}.rest_json",
                side_effect=RuntimeError("reached the product PR fetch"),
            ),
            pytest.raises(RuntimeError, match="reached the product PR fetch"),
        ):
            emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)

    def test_decline_names_the_inherited_stamp_remedy(self) -> None:
        """The one behaviour this guard takes away, said out loud.

        OMN-16386 repaired a cascade-template PR that inherited an
        Evidence-Source stamp naming a sibling's companion by minting a fresh
        one. Its live casualties were OCC-internal PRs, and those are now
        declined before that repair is reached. The decline is the only thing
        a human will see, so it carries the remedy: remove the stamp. Safe
        because an OCC-internal PR does not need a companion to pass its own
        Receipt Gate — onex_change_control#10356 merged with no
        Evidence-Source and ``verify / verify`` green.
        """
        emitter = _tripwired_emitter()
        patches = _no_side_effects(emitter)

        with patches[0], patches[1], patches[2], patches[3]:
            action = emitter._emit_companion_sync(
                "OmniNode-ai/onex_change_control", 6850, None
            )

        assert "remove the stamp" in action
        assert "needs no companion" in action

    async def test_the_arm_reports_declined_not_error(self) -> None:
        # End to end through the handler: the whole point is that this stops
        # being a red ERROR check on a product PR.
        emitter = _tripwired_emitter()
        handler = HandlerPrLifecycleFix(
            occ_autobind_adapter=emitter,
            outcome_token_resolver=lambda: None,
        )
        patches = _no_side_effects(emitter)

        with patches[0], patches[1], patches[2], patches[3]:
            result = await handler.handle(_autobind_command())

        assert result.error is None
        assert result.occ_companion_verified is False
        assert result.fix_action.startswith("skip:OCC_SELF_COMPANION")
        assert (
            handler._classify_autobind_outcome(
                errored=result.error is not None,
                companion_verified=result.occ_companion_verified,
            ).value
            == "DECLINED"
        )
