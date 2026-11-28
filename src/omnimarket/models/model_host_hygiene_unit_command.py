# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab work unit the host hygiene tick route puts on the lab work command topic.

The runtime publishes a handler's output event by the class of its payload (the contract's ``published_events``
map), so the unit needs a class of its own to reach the command topic and not the run topic. It is the lab work
request unchanged on the wire, with one rule: an unset ``code_draft`` stays off the wire, as ``LabWorkCaller``
sends it, because a pool host still on a request model from before that field (``extra="forbid"``) refuses a
unit that carries ``code_draft: null``.
"""

from __future__ import annotations

from typing import Any

from pydantic import SerializerFunctionWrapHandler, model_serializer

from omnimarket.models.model_lab_work_unit import ModelLabWorkUnitRequest


class ModelHostHygieneUnitCommand(ModelLabWorkUnitRequest):
    """A lab work request for one host hygiene cleanup, published by the tick route."""

    @model_serializer(mode="wrap")
    def _drop_unset_code_draft(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("code_draft") is None:
            data.pop("code_draft", None)
        return data


__all__: list[str] = ["ModelHostHygieneUnitCommand"]
