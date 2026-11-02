# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevTagPinTrain - one night of the sibling-pin train, resolved before it opens a PR (OMN-21895).

The existing nightly train (OMN-19998) is extended, not copied: the pin rewrite
is still HandlerDevTagPinMover, and this handler adds the two steps the train
lacked. The tag set is chosen so the siblings co-resolve (HandlerDevTagPinSet),
and the candidate is locked in a scratch copy of the consumer before anything
is pushed. A set that cannot co-resolve, and a lock that fails, end the night
with a skipped row and no branch, no PR.

Every effect is a port the caller passes in: ``gather`` reads the siblings,
``resolve`` locks the candidate, ``publish`` pushes the one fixed branch and
opens or updates the one PR. The handler calls ``publish`` last and only after
``resolve`` returned ok, which is the property the tests pin.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_mover import (
    HandlerDevTagPinMover,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_set import (
    HandlerDevTagPinSet,
    render_pin_set_body,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_mover import (
    EnumConsumerTestOutcome,
    EnumPinMoveRefusal,
    ModelDevTagPinMoveRequest,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_set import (
    EnumPinSetOutcome,
    ModelPinSetRequest,
    ModelPinSetResult,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_train import (
    EnumTrainStatus,
    ModelResolveOutcome,
    ModelTrainReport,
)


@dataclass(frozen=True)
class ModelTrainPorts:
    """The three effects of a night."""

    gather: Callable[[str], ModelPinSetRequest]
    resolve: Callable[[str], ModelResolveOutcome]
    publish: Callable[[str, str, str], str]


class TrainMoveRefusedError(Exception):
    """The pin mover refused a move the selection chose."""


def _move_all(pyproject_text: str, selection: ModelPinSetResult) -> str:
    mover = HandlerDevTagPinMover()
    text = pyproject_text
    for move in selection.moves:
        result = mover.handle(
            ModelDevTagPinMoveRequest(
                pyproject_text=text,
                distribution=move.distribution,
                repository_url=move.repository_url,
                new_tag=move.new_tag,
                consumer_tests=EnumConsumerTestOutcome.NOT_RUN,
            )
        )
        if result.refusal is not EnumPinMoveRefusal.CONSUMER_TESTS_NOT_RUN:
            raise TrainMoveRefusedError(
                f"{move.distribution}: {result.detail or result.refusal}"
            )
        text = result.candidate_text
    return text


def _skipped_row(selection: ModelPinSetResult) -> str:
    blocks = [item.conflict.render() for item in selection.held_back]
    return "\n".join(["SKIPPED: no PR opened -- " + selection.detail, *blocks]).rstrip()


class HandlerDevTagPinTrain:
    """One night: select, rewrite, resolve, then (and only then) publish."""

    def handle(
        self,
        *,
        pyproject_text: str,
        lock_text: str,
        ports: ModelTrainPorts,
        publish_enabled: bool,
    ) -> tuple[ModelTrainReport, str, str]:
        """Return the report and the pyproject.toml and uv.lock to write.

        The returned texts equal the inputs unless the night moved and the
        candidate resolved; the caller writes them only when it is applying.
        """
        selection = HandlerDevTagPinSet().handle(ports.gather(pyproject_text))
        if selection.outcome is EnumPinSetOutcome.UNCHANGED:
            return (
                ModelTrainReport(
                    status=EnumTrainStatus.UNCHANGED,
                    selection=selection,
                    body=render_pin_set_body(selection),
                ),
                pyproject_text,
                lock_text,
            )
        if selection.outcome is EnumPinSetOutcome.NO_CO_RESOLVING_SET:
            return (
                ModelTrainReport(
                    status=EnumTrainStatus.SKIPPED_NO_CO_RESOLVING_SET,
                    selection=selection,
                    body=render_pin_set_body(selection),
                    skipped_row=_skipped_row(selection),
                ),
                pyproject_text,
                lock_text,
            )

        candidate = _move_all(pyproject_text, selection)
        body = render_pin_set_body(selection)
        resolved = ports.resolve(candidate)
        if not resolved.ok:
            row = "\n".join(
                [
                    "SKIPPED: no PR opened -- uv lock cannot resolve the candidate pins",
                    *(f"- {url}" for url in resolved.conflicting_urls),
                    resolved.stderr.strip(),
                ]
            ).rstrip()
            return (
                ModelTrainReport(
                    status=EnumTrainStatus.SKIPPED_RESOLVE_FAILED,
                    selection=selection,
                    body=body,
                    resolve=resolved,
                    skipped_row=row,
                ),
                pyproject_text,
                lock_text,
            )

        if not publish_enabled:
            return (
                ModelTrainReport(
                    status=EnumTrainStatus.DRY_RUN,
                    selection=selection,
                    body=body,
                    resolve=resolved,
                ),
                candidate,
                resolved.lock_text,
            )
        publication = ports.publish(candidate, resolved.lock_text, body)
        return (
            ModelTrainReport(
                status=EnumTrainStatus.PUBLISHED,
                selection=selection,
                body=body,
                resolve=resolved,
                publication=publication,
            ),
            candidate,
            resolved.lock_text,
        )


__all__ = [
    "HandlerDevTagPinTrain",
    "ModelTrainPorts",
    "TrainMoveRefusedError",
]
