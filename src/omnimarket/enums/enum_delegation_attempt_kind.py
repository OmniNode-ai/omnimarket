# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kinds of delegation attempts recorded in the terminal ladder (OMN-20168)."""

from enum import StrEnum


class EnumDelegationAttemptKind(StrEnum):
    """The relationship of an attempt to the work that preceded it.

    first_try: the first attempt at the whole request.
    split_child: an attempt at a child unit produced by a split.
    unit_escalation: a split unit attempted again after an earlier attempt failed.
    recombine_check: an attempt to check the recombined split results.
    whole_escalation: the whole request attempted again after an earlier attempt failed, on the next responder or as a same-tier redraw.
    config_swap: the same model and backend retried once with another configuration.
    """

    FIRST_TRY = "first_try"
    SPLIT_CHILD = "split_child"
    UNIT_ESCALATION = "unit_escalation"
    RECOMBINE_CHECK = "recombine_check"
    WHOLE_ESCALATION = "whole_escalation"
    CONFIG_SWAP = "config_swap"
