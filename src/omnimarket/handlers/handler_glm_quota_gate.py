# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold GLM out of the lane delegation ladders while the Coding Plan's limit is exhausted (OMN-24651).

z.ai answers a call over a plan limit with a 429 that carries a business code and the reset time, after the claude CLI
has retried it for about three minutes. Receipt 018b9825-a3b3-4d5d-9338-1b49f4fa47b8 of 2026-11-14 took 188.989 s to
read ``claude-glm exited rc=1: API Error: Request rejected (429) . [1310][Weekly/Monthly Limit Exhausted. Your limit
will reset at 2026-11-15 01:38:18][2026111421065430b8c2c98d0a4d56]``. The lane delegation step then asked glm-5.3,
which draws on the same quota, and asked again on the next step of every lane: 58 receipts on one host carry the same
reset.

Which codes hold GLM out, what a stated reset is read as, and the cooldown for a notice that states none are the zai
rules of ``provider_quota_policy`` (``load_provider_quota_policy``), not constants here: a rule whose disposition is
``disable_until_reset`` (codes 1310, 1308 and 1316 to 1321) holds. The reset stamp is read by ``HandlerProviderQuotaReset`` with the
rule's ``reset_from`` value (``message_reset_timestamp_beijing``: the stamp is Beijing time, UTC+8) and the gate uses the
instant as returned. The zone rides in that value and not in a key of its own because ModelQuotaCodeRule is a wire
model and the omniclaude pin that `tests/fixtures/delegation/quota_rule_legacy_schema.json` stands for refuses an
undeclared key.

This handler is pure: it takes the clock, the text and the hold already on record, and answers. The command line the
lane delegation step runs, with its marker file under the state root, is ``runtime_glm_quota_gate`` in the node that
binds this handler.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.handler_provider_quota_reset import HandlerProviderQuotaReset
from omnimarket.inference.provider_quota_policy import load_provider_quota_policy
from omnimarket.models.delegation.model_glm_quota_gate import (
    ModelGlmQuotaGateRequest,
    ModelGlmQuotaGateResult,
)
from omnimarket.models.delegation.model_provider_quota_reset import (
    ModelProviderQuotaResetRequest,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaDisposition,
    ModelQuotaCodeRule,
)

PROVIDER_ID = "zai"
# A marker further out than a monthly plan can run is garbage, not a hold.
LONGEST_HOLD = timedelta(days=35)
# The claude CLI's refusal shape: "Request rejected (429) · [1310][...]". Text that only quotes a code does not match.
_REFUSAL = re.compile(r"\(429\)[^\[\]\n]{0,8}\[(\d{4})\]")
# A rule that declares no cooldown must still hold a cap it cannot time (fail closed, as the policy does).
_FALLBACK_FLOOR = timedelta(hours=1)


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _holding_refusals(error_text: str) -> list[tuple[ModelQuotaCodeRule, str]]:
    """Each ``(429) [code]`` refusal in the text that names a hold rule, in order, with the text of that refusal alone.

    A refusal's text runs from its own ``(429) [code]`` to the next refusal, so the reset stamp a rule reads is the one
    its own refusal states, never one that belongs to a refusal after it.
    """
    text = error_text or ""
    matches = list(_REFUSAL.finditer(text))
    if not matches:
        return []
    holding = {
        rule.code: rule
        for provider in load_provider_quota_policy().providers
        if provider.provider_id == PROVIDER_ID
        for rule in provider.codes
        if rule.disposition is EnumQuotaDisposition.DISABLE_UNTIL_RESET
    }
    found: list[tuple[ModelQuotaCodeRule, str]] = []
    for i, match in enumerate(matches):
        rule = holding.get(match.group(1))
        if rule is not None:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            found.append((rule, text[match.start() : end]))
    return found


def plan_limit_rule(error_text: str) -> ModelQuotaCodeRule | None:
    """The declared zai ``disable_until_reset`` rule the first refusal in ``error_text`` that names one matches, or None.

    Every ``(429) [code]`` refusal in the text is read in order, so a rate-limit refusal (1302) that precedes a plan-limit
    refusal (1310) does not hide it.
    """
    found = _holding_refusals(error_text)
    return found[0][0] if found else None


def _hold_for(
    rule: ModelQuotaCodeRule, refusal: str, now: datetime
) -> tuple[ModelQuotaCodeRule, datetime]:
    stated = (
        HandlerProviderQuotaReset()
        .handle(
            ModelProviderQuotaResetRequest(
                reset_from=rule.reset_from, reference=now, message=refusal
            )
        )
        .reset_at
    )
    if stated is None or stated <= now:
        unstated = max(timedelta(seconds=rule.fallback_cooldown_seconds), timedelta(0))
        return rule, now + (unstated or _FALLBACK_FLOOR)
    return rule, min(stated, now + LONGEST_HOLD)


def exhausted_until(
    error_text: str, now: datetime
) -> tuple[ModelQuotaCodeRule, datetime] | None:
    """The rule and the UTC instant GLM is held until for the refusals in ``error_text``, or None for any other text.

    Each refusal that names a hold rule is read on its own text: a stated reset that is after ``now`` and within
    ``LONGEST_HOLD`` is its hold. A refusal that states none, one whose stated reset is not after ``now`` (a wrong zone
    or a clock running ahead would read so), or one that cannot be read holds for the rule's
    ``fallback_cooldown_seconds``; a stamp past ``LONGEST_HOLD`` is cut to it. The latest of the refusals' holds is the
    answer, so a 1308 that states no reset followed by a 1310 that states one holds until the 1310 stamp.
    """
    holds = [
        _hold_for(rule, refusal, now) for rule, refusal in _holding_refusals(error_text)
    ]
    if not holds:
        return None
    return max(holds, key=lambda hold: hold[1])


class HandlerGlmQuotaGate:
    def handle(self, request: ModelGlmQuotaGateRequest) -> ModelGlmQuotaGateResult:
        now = request.now
        until = (
            request.marker_until
            if request.marker_until is None
            or request.marker_until <= now + LONGEST_HOLD
            else None
        )
        write_until = None
        code = ""
        if request.operation == "note":
            noted = exhausted_until(request.error_text, now)
            if noted is not None:
                rule, reset = noted
                if until is None or reset > until:
                    until = write_until = reset
                    code = rule.code
        if until is None or until <= now:
            return ModelGlmQuotaGateResult(held=False)
        return ModelGlmQuotaGateResult(
            held=True,
            until=until,
            code=code,
            write_until=write_until,
            reason=f"GLM skipped: the Coding Plan's limit is exhausted until {_stamp(until)} (z.ai plan limit); this lane did not call it",
        )
