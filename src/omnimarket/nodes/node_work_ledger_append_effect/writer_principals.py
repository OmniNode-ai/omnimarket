# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer principals the work-ledger append handler attributes rows to (OMN-21536)."""

from collections.abc import Iterable


class WriterPrincipals:
    """The known writers; the operator principal stays one through every replacement (OMN-21622).

    The serve process replaces the set when the deployment's principal overlay declares a new
    machine, so a row a new machine publishes is attributed without reinstalling the serve. The
    set lives here, in a collaborator the handler is given, so the handler's own public methods
    hold no state.
    """

    def __init__(
        self,
        writers: Iterable[str] = (),
        operator_principal: str | None = None,
    ) -> None:
        self._operator_principal = operator_principal
        self._writers = self._with_operator(writers)

    def _with_operator(self, writers: Iterable[str]) -> frozenset[str]:
        return frozenset(writers) | (
            {self._operator_principal} if self._operator_principal else set()
        )

    def replace(self, writers: Iterable[str]) -> None:
        self._writers = self._with_operator(writers)

    def __contains__(self, principal: object) -> bool:
        return principal in self._writers


__all__ = ["WriterPrincipals"]
