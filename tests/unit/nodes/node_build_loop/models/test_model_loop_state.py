"""Focused proof for build-loop FSM phase sequencing."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    TERMINAL_PHASES,
    EnumBuildLoopMode,
    EnumBuildLoopPhase,
    next_phase,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "sequence"),
    [
        (
            EnumBuildLoopMode.BUILD,
            (
                EnumBuildLoopPhase.CLOSING_OUT,
                EnumBuildLoopPhase.VERIFYING,
                EnumBuildLoopPhase.FILLING,
                EnumBuildLoopPhase.CLASSIFYING,
                EnumBuildLoopPhase.BUILDING,
            ),
        ),
        (
            EnumBuildLoopMode.CLOSE_OUT,
            (
                EnumBuildLoopPhase.CLOSING_OUT,
                EnumBuildLoopPhase.VERIFYING,
                EnumBuildLoopPhase.RELEASING,
                EnumBuildLoopPhase.DEPLOYING,
                EnumBuildLoopPhase.POST_VERIFY,
            ),
        ),
        (
            EnumBuildLoopMode.FULL,
            (
                EnumBuildLoopPhase.CLOSING_OUT,
                EnumBuildLoopPhase.VERIFYING,
                EnumBuildLoopPhase.FILLING,
                EnumBuildLoopPhase.CLASSIFYING,
                EnumBuildLoopPhase.BUILDING,
                EnumBuildLoopPhase.RELEASING,
                EnumBuildLoopPhase.DEPLOYING,
                EnumBuildLoopPhase.POST_VERIFY,
            ),
        ),
        (
            EnumBuildLoopMode.OBSERVE,
            (EnumBuildLoopPhase.VERIFYING,),
        ),
    ],
)
def test_next_phase_matches_mode_owned_sequence(
    mode: EnumBuildLoopMode,
    sequence: tuple[EnumBuildLoopPhase, ...],
) -> None:
    """Each mode's reducer-owned sequence is explicit and stable."""
    observed = [next_phase(EnumBuildLoopPhase.IDLE, mode=mode)]
    while observed[-1] != EnumBuildLoopPhase.COMPLETE:
        observed.append(next_phase(observed[-1], mode=mode))

    assert tuple(observed) == (*sequence, EnumBuildLoopPhase.COMPLETE)


@pytest.mark.unit
def test_skip_closeout_starts_at_verifying_for_closeout_modes() -> None:
    """skip_closeout removes only the closeout phase, not the rest of the mode."""
    assert (
        next_phase(
            EnumBuildLoopPhase.IDLE,
            skip_closeout=True,
            mode=EnumBuildLoopMode.BUILD,
        )
        == EnumBuildLoopPhase.VERIFYING
    )
    assert (
        next_phase(
            EnumBuildLoopPhase.IDLE,
            skip_closeout=True,
            mode=EnumBuildLoopMode.FULL,
        )
        == EnumBuildLoopPhase.VERIFYING
    )


@pytest.mark.unit
@pytest.mark.parametrize("terminal_phase", sorted(TERMINAL_PHASES))
def test_next_phase_rejects_terminal_phases(terminal_phase: EnumBuildLoopPhase) -> None:
    """Terminal states do not silently re-enter the reducer sequence."""
    with pytest.raises(ValueError, match="No next phase from terminal state"):
        next_phase(terminal_phase)


@pytest.mark.unit
def test_next_phase_rejects_phase_outside_mode_sequence() -> None:
    """A phase from a different mode is not treated as authoritative state."""
    with pytest.raises(ValueError, match="not in tuple"):
        next_phase(EnumBuildLoopPhase.RELEASING, mode=EnumBuildLoopMode.BUILD)
