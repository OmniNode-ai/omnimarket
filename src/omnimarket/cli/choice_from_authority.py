# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ``click`` choice whose values are read from their authority when used (OMN-19407).

A ``click.Choice`` built from a literal list is a second copy of a vocabulary
somebody else owns, and it drifts: ``onex cloud delegate --task-type`` carried a
list "transcribed from" the gateway's contract. This type holds only the loader.
The choices are read the first time the command renders help, completes or
validates a value, so importing the command reads nothing and ``onex --help``
never touches the authority.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

import click

__all__ = ["ChoiceFromAuthority"]


class ChoiceFromAuthority(click.Choice[str]):
    """``click.Choice`` over the values an authority loader returns, read lazily.

    A loader that raises is reported as a usage error naming the authority, so
    the command fails loudly instead of offering a stale or empty list.
    """

    def __init__(self, authority: str, load: Callable[[], Iterable[str]]) -> None:
        self._authority = authority
        self._load = load
        self._resolved: tuple[str, ...] | None = None
        super().__init__((), case_sensitive=True)

    @property  # type: ignore[override]
    def choices(self) -> Sequence[str]:
        """Return the authority's values, read once per process."""
        if self._resolved is None:
            try:
                values = tuple(sorted(str(value) for value in self._load()))
            except (ImportError, OSError, ValueError) as exc:
                raise click.UsageError(
                    f"{self._authority} could not be read, so the accepted "
                    f"values are unknown: {exc}"
                ) from exc
            if not values:
                raise click.UsageError(f"{self._authority} declares no values")
            self._resolved = values
        return self._resolved

    @choices.setter
    def choices(self, value: Sequence[str]) -> None:
        """Refuse any list but the empty one ``click.Choice.__init__`` assigns.

        The loader is the only source. A caller handing this type a literal
        list would reintroduce the copy it exists to remove.
        """
        if tuple(value):
            raise TypeError(
                f"{type(self).__name__} reads its choices from {self._authority}; "
                "it does not accept a literal list"
            )
