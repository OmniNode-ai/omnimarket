# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The weekly retrospective ends the Friday stand-up (OMN-21286).

RULING 2026-10-20T13:51:27Z, amended to Friday by RULING 2026-10-20T13:58:45Z. Entries are a JSON list of
``{role, product_use, idea | friction, process_change}``. Only those fields are read, so a name an entry carries never
reaches the sheet; credit is a role of the policy's vocabulary. Drafts are never filed here. The weekday, the keys,
the role vocabulary, the draft state, the credit grammar and every sentence are the injected policy's
(``config.standup_retrospective`` of the contract, overlaid), none is a literal here.
"""

import json
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_standup_retrospective_compute.models.model_standup_retrospective import (
    ModelStandupRetrospectivePolicy,
    ModelStandupRetrospectiveRequest,
    ModelStandupRetrospectiveResult,
)
from omnimarket.nodes.node_standup_retrospective_compute.overlay import (
    load_standup_retrospective_policy,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class HandlerStandupRetrospective:
    def __init__(self, policy: ModelStandupRetrospectivePolicy | None = None) -> None:
        # the runtime builds a handler from its contract with no policy argument: the contract's
        # config with the deployment overlay laid over it is the default; a caller injects its own
        self.policy = (
            policy if policy is not None else load_standup_retrospective_policy()
        )
        self._roles = {
            role.strip().lower(): role for role in self.policy.role_vocabulary
        }

    def _error(self, detail: str) -> str:
        first = (
            detail.splitlines()[0] if detail else self.policy.messages.unparsable_detail
        )
        return f"{self.policy.messages.error_prefix}{first}"

    def render(
        self, retrospective: dict[str, Any], drafts: list[dict[str, Any]]
    ) -> list[str]:
        """The text sheet's retrospective and ticket-draft sections; none when there is no retrospective."""
        if not retrospective:
            return []
        labels = self.policy.labels
        people = retrospective["people"]
        lines = [labels.section_heading.format(count=len(people))]
        for row in people:
            parts = [
                labels.use_part.format(text=row["product_use"] or labels.not_reported),
                labels.detail_part.format(
                    kind=row["kind"] or labels.default_kind_label,
                    text=row["idea_or_friction"] or labels.not_reported,
                ),
                labels.change_part.format(
                    text=row["process_change"] or labels.not_reported
                ),
            ]
            lines.append(
                labels.person_line.format(
                    role=row["role"], parts=labels.part_separator.join(parts)
                )
            )
        lines.append(labels.drafts_heading.format(count=len(drafts)))
        lines += [
            labels.draft_line.format(
                state=draft["state"], title=draft["title"], credit=draft["credit"]
            )
            for draft in drafts
        ]
        return lines

    def handle(
        self, request: ModelStandupRetrospectiveRequest
    ) -> ModelStandupRetrospectiveResult:
        policy, messages = self.policy, self.policy.messages
        notes: list[str] = []
        errors: list[str] = []
        drafts: list[dict[str, Any]] = []
        retrospective: dict[str, Any] = {}
        if datetime.fromisoformat(request.until).weekday() != policy.retro_weekday:
            if request.entries is not None:
                notes.append(messages.not_a_retro_day_note)
            return ModelStandupRetrospectiveResult(notes=notes)
        people: list[dict[str, Any]] = []
        retrospective.update(friday=True, people=people)
        entries: Any = None
        if request.entries is None:
            notes.append(messages.no_input_note)
        else:
            try:
                entries = json.loads(request.entries)
            except ValueError as error:
                errors.append(self._error(str(error)))
            else:
                if not isinstance(entries, list):
                    errors.append(self._error(messages.not_a_list_detail))
                    entries = []
        for number, entry in enumerate(entries or [], 1):
            raw = entry if isinstance(entry, dict) else {}
            said = _text(raw.get(policy.role_key))
            if not said:
                errors.append(
                    self._error(messages.no_role_detail.format(number=number))
                )
                continue
            role = self._roles.get(said.lower())
            if role is None:
                errors.append(
                    self._error(messages.unknown_role_detail.format(number=number))
                )
                continue
            idea, friction = (
                _text(raw.get(policy.idea_key)),
                _text(raw.get(policy.friction_key)),
            )
            row: dict[str, Any] = {
                "role": role,
                "product_use": _text(raw.get(policy.product_use_key)),
                "idea_or_friction": idea or friction,
                "kind": policy.idea_kind
                if idea
                else policy.friction_kind
                if friction
                else "",
                "process_change": _text(raw.get(policy.process_change_key)),
            }
            row["missing"] = [
                key
                for key, value in row.items()
                if key not in ("role", "kind", "missing") and not value
            ]
            people.append(row)
            if row["idea_or_friction"]:
                credit = policy.credit_template.format(role=role)
                drafts.append(
                    {
                        "title": row["idea_or_friction"].splitlines()[0][
                            : policy.draft_title_max_chars
                        ],
                        "state": policy.draft_state,
                        "kind": row["kind"],
                        "credit": credit,
                        "description": policy.draft_description_template.format(
                            text=row["idea_or_friction"],
                            until=request.until,
                            credit=credit,
                        ),
                        "filed": False,
                    }
                )
        return ModelStandupRetrospectiveResult(
            retrospective=retrospective,
            ticket_drafts=drafts,
            section_lines=self.render(retrospective, drafts),
            errors=errors,
            notes=notes,
        )
