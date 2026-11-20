# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASCII welcome screen for human onboarding sessions."""

from __future__ import annotations

from textwrap import wrap
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict


class ModelSetupBanner(BaseModel):
    """A deterministic banner that also fits narrow terminals."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str

    _FONT: ClassVar[dict[str, tuple[str, ...]]] = {
        " ": ("     ",) * 5,
        "A": (" ### ", "#   #", "#####", "#   #", "#   #"),
        "B": ("#### ", "#   #", "#### ", "#   #", "#### "),
        "C": (" ####", "#    ", "#    ", "#    ", " ####"),
        "D": ("#### ", "#   #", "#   #", "#   #", "#### "),
        "E": ("#####", "#    ", "#### ", "#    ", "#####"),
        "G": (" ####", "#    ", "# ###", "#   #", " ####"),
        "I": ("#####", "  #  ", "  #  ", "  #  ", "#####"),
        "L": ("#    ", "#    ", "#    ", "#    ", "#####"),
        "M": ("#   #", "## ##", "# # #", "#   #", "#   #"),
        "N": ("#   #", "##  #", "# # #", "#  ##", "#   #"),
        "O": (" ### ", "#   #", "#   #", "#   #", " ### "),
        "R": ("#### ", "#   #", "#### ", "#  # ", "#   #"),
        "T": ("#####", "  #  ", "  #  ", "  #  ", "  #  "),
        "W": ("#   #", "#   #", "# # #", "# # #", " # # "),
    }

    @classmethod
    def build(cls, width: int = 80) -> Self:
        """Render the requested welcome words without terminal escape codes."""
        if width < 16:
            raise ValueError("banner width must be at least 16")
        title = "Welcome To OmniNode Delegation Onboarding"
        if width < 60:
            border = "+" + "-" * (width - 2) + "+"
            body = [
                "| " + line.ljust(width - 4) + " |" for line in wrap(title, width - 4)
            ]
            return cls(text="\n".join((border, *body, border)) + "\n")
        lines: list[str] = []
        for words in ("WELCOME TO", "OMNINODE", "DELEGATION", "ONBOARDING"):
            for row in range(5):
                art = " ".join(cls._FONT[letter][row] for letter in words).rstrip()
                lines.append(art.center(width).rstrip())
            lines.append("")
        lines.append(title.center(width).rstrip())
        return cls(text="\n".join(lines) + "\n")
