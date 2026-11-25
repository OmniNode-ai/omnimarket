# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The public-safety guard of a published record: refuse a payload that names a private repository, host or person.

The record's fields are typed (counts, dates, versions, run ids, commit ids), so the only places a name can enter are the
repository and the two version strings. The guard checks the repository against the overlay's public list (an empty list
refuses everything), then scans every text field against the policy's denied patterns and the overlay's private terms.
The error names the field and the reason class, never the offending value.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_attribution_record import (
    ModelPublishedAttributionRecord,
    PublicSafetyError,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelMissCollectPolicy,
    ModelMissCollectSettings,
)


def assert_public_safe(
    record: ModelPublishedAttributionRecord,
    settings: ModelMissCollectSettings,
    policy: ModelMissCollectPolicy,
) -> None:
    """Raise PublicSafetyError unless the record can be published into a public repository."""
    if record.repository not in settings.public_repositories:
        raise PublicSafetyError("repository", "not a declared public repository")
    patterns = [re.compile(p, re.IGNORECASE) for p in policy.record_denied_patterns]
    terms = [t.lower() for t in settings.private_terms if t.strip()]
    for field in ("repository", "selector_version", "policy_version"):
        value: str = getattr(record, field)
        if any(p.search(value) for p in patterns):
            raise PublicSafetyError(
                field, "matches a denied host, address or path pattern"
            )
        lowered = value.lower()
        if any(term in lowered for term in terms):
            raise PublicSafetyError(field, "names a private term")


__all__: list[str] = ["assert_public_safe"]
