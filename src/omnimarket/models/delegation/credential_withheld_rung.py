# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18696: a ladder rung the routing authority withheld for a missing credential.

``ModelLocalCredentialRefusal`` reports a credential fact discovered at the
EFFECT boundary, after a backend has been selected. This model reports the same
class of fact discovered one step earlier, at ROUTING time, where a backend
whose declared credential resolves to nothing is not eligible and is therefore
never selected at all.

Both detection points are real and neither replaces the other:

* the effect boundary fires when routing DID select the backend and the value
  disappeared between the two -- reachable today through the contract model
  pin, which bypasses tier-order selection for the initial attempt;
* routing fires on the ordinary cheapest-first ladder, which is every run a
  customer makes.

Measured on this Mac, 2026-09-19, one command run twice with nothing else
changed. With an (invalid) value registered for ``llm.openrouter.api_key`` the
ladder climbed to ``cheap_frontier`` and refused with a typed
``credential_rejected``. With the same reference deleted from the store, the
identical command never reached that tier at all: three local attempts,
``escalation_count`` 0, ``credential_refusal`` null, and not one line anywhere
naming the credential. The customer whose key is simply not registered yet got
strictly LESS information than the one whose key is wrong.

This model is what closes that asymmetry. It carries reference NAMES only,
never a secret VALUE, on the same terms as ``ModelLocalCredentialRefusal``:
every field here is safe to log, publish and display by construction.

It lives under ``models.delegation`` for the same mechanical reason that model
does -- it is reachable from the wire response, which the api-server import
graph pulls in, and anything under ``omnimarket.inference`` reaches a database
driver through that package's eager imports.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCredentialWithheldRung(BaseModel):
    """A tier the ladder skipped because the credential it declares is absent.

    "Withheld" is deliberately narrower than "not selected". A rung reaches
    this model only when relaxing the credential term ALONE makes the routing
    authority select it -- so a rung the authority declined for a quota state,
    an unknown task class, a context ceiling, or any eligibility term added
    later is not reported here. The point of the distinction is that this
    model produces a sentence telling a customer to register a key, and that
    sentence is wrong if anything else was also in the way.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: str = Field(
        description="The routing tier that would have served this task class."
    )
    backend_ref: str = Field(
        description="The bifrost backend id the tier would have selected."
    )
    endpoint_url: str = Field(
        description="The endpoint that backend declares, for the refusal payload."
    )
    model_id: str = Field(description="The model the withheld rung would have run.")
    credential_ref: str | None = Field(
        default=None,
        description=(
            "The credential REFERENCE the backend declares, by name. Never a "
            "secret value."
        ),
    )
    credential_env: str | None = Field(
        default=None,
        description=(
            "The environment-variable NAME the backend declares as its "
            "fallback, when it declares one. Never a secret value."
        ),
    )

    @property
    def named_credential(self) -> str:
        """The credential this rung needs, named for a human.

        Names the reference and the env-var fallback when both are declared,
        on the same terms as ``ModelLocalCredentialRefusal.named_credential``:
        a customer whose reference is right and whose fallback is wrong cannot
        act on either name alone.
        """
        names = [name for name in (self.credential_ref, self.credential_env) if name]
        if not names:
            return "a provider credential this backend declares (no reference)"
        return " / ".join(names)

    @property
    def message(self) -> str:
        """One human-readable line, safe to surface verbatim.

        Deliberately phrased as a rung that was NOT ATTEMPTED rather than as a
        refusal. This run failed for its own reasons; what this sentence adds
        is that a cheaper route was unavailable and why. Telling a customer
        their delegation was "refused" when it was graded and rejected
        somewhere else would send them after the wrong fact.
        """
        return (
            f"Tier {self.tier!r} was not attempted: no value resolves for "
            f"{self.named_credential} on this machine, so backend "
            f"{self.backend_ref!r} (model {self.model_id!r}) was not eligible. "
            f"Register a value for this reference in the secret store to make "
            f"this route available."
        )


__all__ = ["ModelCredentialWithheldRung"]
