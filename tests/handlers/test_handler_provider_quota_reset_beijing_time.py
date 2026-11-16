# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24651: z.ai states its reset stamp in Beijing time, and the shared reset handler reads it as such.

The recorded text is receipt 018b9825-a3b3-4d5d-9338-1b49f4fa47b8 of 2026-11-14: the call began 13:03:45Z, lasted
188.989 s, and carries request id 20261114210654, the end of the call plus eight hours. Read as UTC the stamp
2026-11-15 01:38:18 holds the provider out eight hours past its reset.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator

from omnimarket.handlers.handler_provider_quota_reset import (
    HandlerProviderQuotaReset,
    parse_reset_instant,
)
from omnimarket.inference.provider_quota_policy import (
    EnumQuotaDisposition,
    classify_quota_response,
    load_provider_quota_policy,
)
from omnimarket.models.delegation.model_provider_quota_reset import (
    ModelProviderQuotaResetRequest,
)

RECORDED = (
    "claude-glm exited rc=1: API Error: Request rejected (429) · [1310][Weekly/Monthly Limit Exhausted. "
    "Your limit will reset at 2026-11-15 01:38:18][2026111421065430b8c2c98d0a4d56]"
)
CALL_ENDED = datetime(2026, 11, 14, 13, 6, 54, tzinfo=UTC)
RESET_UTC = datetime(2026, 11, 14, 17, 38, 18, tzinfo=UTC)
ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"


def test_the_recorded_beijing_stamp_is_eight_hours_before_the_same_digits_in_utc() -> (
    None
):
    assert parse_reset_instant(RECORDED, utc_offset_minutes=480) == RESET_UTC
    # With no offset declared the stamp stays UTC, so a provider that states UTC is unchanged.
    assert parse_reset_instant(RECORDED) == datetime(
        2026, 11, 15, 1, 38, 18, tzinfo=UTC
    )


def test_the_reset_handler_applies_the_offset_the_reset_from_value_names() -> None:
    result = HandlerProviderQuotaReset().handle(
        ModelProviderQuotaResetRequest(
            reset_from="message_reset_timestamp_beijing",
            reference=CALL_ENDED,
            message=RECORDED,
        )
    )
    assert result.reset_at == RESET_UTC
    # A rule that names no zone reads the same stamp as UTC, so a provider that states UTC is unchanged.
    as_utc = HandlerProviderQuotaReset().handle(
        ModelProviderQuotaResetRequest(
            reset_from="message_reset_timestamp",
            reference=CALL_ENDED,
            message=RECORDED,
        )
    )
    assert as_utc.reset_at == datetime(2026, 11, 15, 1, 38, 18, tzinfo=UTC)


def test_the_zai_rule_names_beijing_time_and_the_bifrost_path_holds_until_the_utc_reset() -> (
    None
):
    """The bifrost path (zai rules, reset_from: message_reset_timestamp) is the behaviour that held eight hours too long."""
    body = {
        "error": {
            "code": "1310",
            "message": "Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-11-15 01:38:18",
        }
    }
    verdict = classify_quota_response(
        status_code=429, endpoint_url=ZAI_URL, body=body, now=CALL_ENDED
    )
    assert verdict is not None
    assert verdict.disposition is EnumQuotaDisposition.DISABLE_UNTIL_RESET
    assert verdict.disabled_until == RESET_UTC
    for code in ("1310", "1308"):
        rule = next(
            r
            for p in load_provider_quota_policy().providers
            if p.provider_id == "zai"
            for r in p.codes
            if r.code == code
        )
        assert rule.reset_from == "message_reset_timestamp_beijing"


def test_every_zai_rule_that_reads_its_reset_from_the_message_names_beijing_time() -> (
    None
):
    """Codes 1316 to 1321 held the same Beijing-time stamp eight hours too long while they read it as UTC."""
    utc_readers = [
        (p.provider_id, r.code)
        for p in load_provider_quota_policy().providers
        if p.provider_id.startswith("zai")
        for r in p.codes
        if r.reset_from == "message_reset_timestamp"
    ]
    assert utc_readers == []
    declared = {
        r.code
        for p in load_provider_quota_policy().providers
        if p.provider_id.startswith("zai")
        for r in p.codes
        if r.reset_from == "message_reset_timestamp_beijing"
    }
    assert declared == {"1310", "1308"} | {str(c) for c in range(1316, 1322)}


def test_the_bifrost_path_reads_a_1316_stamp_as_beijing_time() -> None:
    body = {
        "error": {
            "code": "1316",
            "message": "Plan window spent. Your limit will reset at 2026-11-15 01:38:18",
        }
    }
    verdict = classify_quota_response(
        status_code=429, endpoint_url=ZAI_URL, body=body, now=CALL_ENDED
    )
    assert verdict is not None
    assert verdict.disabled_until == RESET_UTC


LEGACY_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "delegation"
    / "quota_rule_legacy_schema.json"
)


def test_every_quota_rule_of_every_provider_decodes_on_the_pinned_consumer_shape() -> (
    None
):
    """The Wire Compatibility Gate's consumer, the omniclaude pin dev-20261112062638-179aa1280722.

    tests/fixtures/delegation/quota_rule_legacy_schema.json stands for that pin's ModelQuotaCodeRule: it forbids extra
    keys. The z.ai rules carry the Beijing offset in their ``reset_from`` value, so no rule of any provider dumps a key
    the pin refuses.
    """
    schema = json.loads(LEGACY_FIXTURE.read_text())["schema"]
    validator = Draft202012Validator(schema)
    rules = [
        (p.provider_id, r)
        for p in load_provider_quota_policy().providers
        for r in p.codes
    ]
    assert any(pid == "zai" for pid, _ in rules)
    for _pid, rule in rules:
        validator.validate(rule.model_dump(mode="json"))
