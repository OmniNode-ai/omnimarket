# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt sanitiser: each secret class, a fixed point, and no harm to plain text."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers import (
    handler_onboarding_clean_vm_walk as walk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.receipt_redaction import (
    redact,
    secret_findings,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumWalkStepKind,
    ModelWalkRequest,
)

pytestmark = pytest.mark.unit


def fake(*parts: str) -> str:
    return "".join(parts)


VALUE = fake("fix", "ture", "Value", "7Hq")
CASES = [
    (
        "url_userinfo",
        f"git+ssh://deploy:{VALUE}@git.example.invalid:22/r",
        "git.example.invalid",
    ),
    ("url_userinfo", f"https://{VALUE}@git.example.invalid/r", "git.example.invalid"),
    ("url_query", f"http://127.0.0.1:8123/?token={VALUE}", "127.0.0.1:8123"),
    (
        "ssh_userinfo_password",
        f"deploy:{VALUE}@git.example.invalid:/srv",
        "deploy@git.example.invalid",
    ),
    ("authorization_header", f"Authorization: Basic {VALUE}", "Authorization: Basic"),
    ("bearer_token", f"bearer {VALUE}", "bearer"),
    ("secret_assignment", f"PASSWD={VALUE}", "PASSWD="),
    ("secret_assignment", f'"client_secret": "{VALUE}"', '"client_secret": "'),
    ("secret_assignment", f"x-api-key: {VALUE}", "x-api-key: "),
    ("secret_assignment", f"private_key='{VALUE}'", "private_key='"),
    ("openai_style_key", fake("sk", "-ant-", VALUE * 3), ""),
    ("github_token", fake("github", "_pat_", VALUE * 3), ""),
    ("slack_token", fake("xo", "xp-", VALUE), ""),
    ("aws_access_key_id", fake("AS", "IA", "FIXTUREFIXTURE00"), ""),
    ("gitlab_token", fake("gl", "pat-", VALUE * 2), ""),
    ("google_api_key", fake("AI", "za", VALUE * 3), ""),
    ("huggingface_token", fake("h", "f_", VALUE * 3), ""),
    ("linear_api_key", fake("lin", "_api_", VALUE * 3), ""),
    ("jwt", fake("ey", "J", VALUE, ".", VALUE, ".", VALUE), ""),
]


@pytest.mark.parametrize(("pattern_class", "text", "kept"), CASES)
def test_redact_each_secret_class(pattern_class: str, text: str, kept: str) -> None:
    assert pattern_class in {finding[0] for finding in secret_findings(text)}
    redacted = redact(text)
    assert VALUE not in redacted
    assert kept in redacted
    assert redact(redacted) == redacted
    assert secret_findings(redacted) == ()


def test_redact_findings_report_no_value() -> None:
    findings = secret_findings(f"password={VALUE}")
    assert findings == (("secret_assignment", len("password="), len(VALUE)),)


@pytest.mark.parametrize(
    "plain",
    [
        "git@github.com:OmniNode-ai/omniclaude.git",
        "21:09:28 ✘ Phase 3/6 FAILED: onex, local identity and your models (1m 30s)",
        '{"run_id":"success-1","status":"success","tokens":12}',
        "https://raw.githubusercontent.com/OmniNode-ai/omniclaude/abc/lab-onboarding.sh",
    ],
)
def test_redact_leaves_plain_text_alone(plain: str) -> None:
    assert redact(plain) == plain
    assert secret_findings(plain) == ()


def test_redact_leaves_every_documented_command_alone() -> None:
    req = walk.resolve_request(
        ModelWalkRequest.model_validate(
            {
                "script_sha": "a" * 40,
                "checksum_line": "b" * 64 + "  lab-onboarding.sh",
                "providers": ["local"],
                "local_url": "http://127.0.0.1:8000/v1",
                "local_model": "test model",
                "tart_host": "192.0.2.1",
                "vm_name": "onex-walk-1234abcd",
            }
        )
    )
    for kind in EnumWalkStepKind:
        for arg in walk.build_step_argv(kind, req):
            assert redact(arg) == arg, kind
