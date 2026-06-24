# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance corpora for G2 mass-produced mechanical scanner validators (OMN-13294).

Each module here exports a typed ``ModelValidatorCorpus`` that is the acceptance
authority for one generated validator. The corpus — NOT the LLM's self-report —
decides whether a generated scanner is accepted (``corpus_acceptance``, G0). A
corpus is seeded from the hand-authored ground-truth invariant (regex patterns +
suppression marker) it encodes, and it MUST carry at least one adversarial
mutation case (``ModelCorpusFixture.mutation_of``) — a gate that passes only
curated examples is not proven (OMN-13289).

These corpora are the durable, committed evidence of what each G2 validator was
required to flag and pass, so a generation run is replayable: same corpus + same
generated scanner => same verdict.
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.validator_corpora.corpus_doc_content_scan import (
    DOC_CONTENT_SCAN_CORPUS,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora.corpus_hardcoded_ip import (
    HARDCODED_IP_CORPUS,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora.corpus_hardcoded_localhost_url import (
    HARDCODED_LOCALHOST_URL_CORPUS,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora.corpus_hardcoded_topic import (
    HARDCODED_TOPIC_CORPUS,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora.corpus_no_faked_boundary import (
    NO_FAKED_BOUNDARY_CORPUS,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora.corpus_pin_hygiene import (
    PIN_HYGIENE_CORPUS,
)
from omnimarket.nodes.node_generation_consumer.validator_corpora.corpus_todo_marker import (
    TODO_MARKER_CORPUS,
)

__all__ = [
    "CORPORA",
    "DOC_CONTENT_SCAN_CORPUS",
    "HARDCODED_IP_CORPUS",
    "HARDCODED_LOCALHOST_URL_CORPUS",
    "HARDCODED_TOPIC_CORPUS",
    "NO_FAKED_BOUNDARY_CORPUS",
    "PIN_HYGIENE_CORPUS",
    "TODO_MARKER_CORPUS",
]

# Registry of every G2 corpus keyed by its target validator name. The generation
# driver iterates this map so adding a corpus here is the only edit needed to
# enrol a new mechanical scanner in the G2 batch.
CORPORA = {
    "doc-content-scan": DOC_CONTENT_SCAN_CORPUS,
    "hardcoded-private-ip": HARDCODED_IP_CORPUS,
    "hardcoded-localhost-url": HARDCODED_LOCALHOST_URL_CORPUS,
    "hardcoded-topic-string": HARDCODED_TOPIC_CORPUS,
    "todo-fixme-marker": TODO_MARKER_CORPUS,
    "no-faked-boundary": NO_FAKED_BOUNDARY_CORPUS,
    "pin-hygiene": PIN_HYGIENE_CORPUS,
}
