# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persistent refusal parking inside the fill's existing lock; no third send."""

import json
import os
from pathlib import Path

from omnimarket.models.model_rule_premise import ModelBatchPolicy, ModelSendRefusal


class SendRefusalStore:
    def __init__(self, path: Path, policy: ModelBatchPolicy) -> None:
        self.path, self.policy = path, policy
        self.rows = {
            ticket: ModelSendRefusal.model_validate(row)
            for ticket, row in (
                json.loads(path.read_text()) if path.exists() else {}
            ).items()
        }

    def parked(self, ticket: str, now: float) -> bool:
        row = self.rows.get(ticket)
        if row is None:
            return False
        if row.parked_until and now >= row.parked_until:
            self.rows.pop(ticket)
            self._save()
            return False
        return row.parked_until > now

    def refuse(self, ticket: str, reason: str, now: float) -> ModelSendRefusal:
        previous = self.rows.get(ticket)
        count = (previous.attempts if previous else 0) + 1
        row = ModelSendRefusal(
            ticket=ticket,
            reason=reason,
            attempts=count,
            parked_until=now + self.policy.park_s
            if count >= self.policy.park_after
            else 0,
        )
        self.rows[ticket] = row
        self._save()
        return row

    def clear(self, ticket: str) -> None:
        if self.rows.pop(ticket, None) is not None:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({key: row.model_dump() for key, row in self.rows.items()})
        )
        os.replace(temporary, self.path)
