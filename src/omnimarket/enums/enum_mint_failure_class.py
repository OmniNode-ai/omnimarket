# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Failure taxonomy for the OCC companion mint's network legs (OMN-15447)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumMintFailureClass(StrEnum):
    """What went wrong on a network leg of the OCC companion mint.

    Deliberately semantic rather than exception-typed: the contract declares a
    disposition per member of THIS enum, so a contract stays readable by a
    person and cannot drift into naming Python classes that a refactor renames.
    Classification from a live exception is
    :func:`omnimarket.nodes.node_occ_companion_effect.mint_retry_policy.classify_mint_failure`.
    """

    # A git subprocess (clone / push over HTTPS) exceeded the contract's
    # per-op bound. The mint is idempotent by construction, so a fresh attempt
    # is the correct response -- this is the class OMN-15447 was filed on.
    GIT_TIMEOUT = "git_timeout"
    # The GitHub API call never reached a status: a network error, a dropped
    # connection, or a decode failure (``GitHubApiError.status_code is None``).
    GITHUB_TRANSPORT = "github_transport"
    # GitHub answered 5xx. Transient on GitHub's side; a fresh attempt is free
    # of side effects because the mint is idempotent.
    GITHUB_SERVER_ERROR = "github_server_error"
    # The credential's GitHub API budget is exhausted (primary or secondary
    # rate limit). Retrying spends the SAME exhausted budget and makes the
    # condition worse, so this class must never be retried in-handler.
    GITHUB_RATE_LIMITED = "github_rate_limited"
    # Any other 4xx: a permissions, validation or not-found answer that a
    # fresh identical attempt cannot change.
    GITHUB_CLIENT_ERROR = "github_client_error"
    # The compute plan would bind a supersession to a check that is not the
    # superseded item's own -- the OMN-15459 AC(d) producer-side refusal,
    # raised as ``SupersessionCheckBindingError``. NOT a network leg, and the
    # only member of this taxonomy that is not: it is declared here because
    # the disposition question is identical (this request must be preserved
    # and replayed, never retried) and because leaving it OUTSIDE the taxonomy
    # is what produced the OMN-18881 dev-lane outage. Unclassified exceptions
    # propagate raw, the boundary's sanitizer blanks any message containing
    # ``auth`` -- which ``author``/``authored`` matches -- and the operator is
    # handed a dead letter reading only ``[REDACTED]``. A fresh attempt
    # reproduces the collision byte for byte, so this is ``park``, never
    # ``retry``.
    EVIDENCE_BINDING_COLLISION = "evidence_binding_collision"
