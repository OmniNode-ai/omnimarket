# SPDX-License-Identifier: MIT
"""Repository contract extensions consumed by the DoD verifier."""

from __future__ import annotations

from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem


class ModelRepoContractDodItem(ModelContractDodItem):
    """The core execution schema the verifier reads repository contracts with.

    The core model declares the criterion binding records and the
    binding-retirement field; the existing criteria and retirement services
    validate the records. Reading the field set from the core model makes the
    accepted item shape independent of a checkout of another repository while
    retaining the core model's extra-field refusal.
    """
