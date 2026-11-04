# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pure, exhaustive presentation of the Wave 1 tracked-item report."""

from datetime import UTC, datetime

from omnimarket.models.model_ask_state import ModelAskEntry
from omnimarket.models.model_operator_item_render import (
    ModelOperatorItemRenderRequest,
    ModelOperatorItemRenderResult,
)


def item_age(said_at: datetime, now: datetime) -> str:
    minutes = max(0, int((now - said_at).total_seconds() // 60))
    days, remainder = divmod(minutes, 24 * 60)
    hours, mins = divmod(remainder, 60)
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


class HandlerOperatorItemRender:
    """No reads, clocks, writes, truncation of the list, or state of a previous tick."""

    def handle(
        self, request: ModelOperatorItemRenderRequest
    ) -> ModelOperatorItemRenderResult:
        if (
            request.source_error is not None
            or request.report is None
            or request.config is None
        ):
            why = " ".join(
                (
                    request.source_error
                    or (
                        "tracked-item report was not read"
                        if request.report is None
                        else "render overlay was not supplied"
                    )
                ).split()
            )
            return ModelOperatorItemRenderResult(
                lines=(f"UNKNOWN open-items | {why}",),
                known=False,
            )
        items = sorted(
            (
                item
                for item in request.report.asks
                if item.state not in request.config.closed_states
            ),
            key=lambda item: (item.said_at, item.ask_id),
        )
        groups: dict[str, list[ModelAskEntry]] = {}
        for item in items:
            groups.setdefault(item.owner_lane, []).append(item)
        oldest = item_age(items[0].said_at, request.now) if items else "none"
        unowned = sum(item.running_lane is None for item in items)
        stamp = request.now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"OPEN-ITEMS n={len(items)} unowned={unowned} oldest={oldest} as_of={stamp}"
        ]
        for owner, owned in groups.items():
            lines.append(f"owner={owner} ({len(owned)})")
            for item in owned:
                mark = (
                    " NEW"
                    if item.ask_id in request.new
                    or item.state in request.config.new_states
                    else ""
                )
                words = " ".join(item.words.split())[: request.config.words_chars]
                lines.append(
                    f"  {item_age(item.said_at, request.now)} kind={item.kind} owner={owner} "
                    f"state={item.state.value}{mark} id={item.ask_id}: {words}"
                )
        return ModelOperatorItemRenderResult(
            lines=tuple(lines),
            known=True,
            count=len(items),
            unowned=unowned,
            oldest=oldest,
            overdue=tuple(
                item.ask_id
                for item in items
                if (request.now - item.said_at).total_seconds()
                > request.config.age_limit_minutes * 60
            ),
        )
