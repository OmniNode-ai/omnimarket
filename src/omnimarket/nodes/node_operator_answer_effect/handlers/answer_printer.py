# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The prompt hook binds its own live output stream and session to this printer."""

from typing import TextIO


class TerminalAnswerPrinter:
    def __init__(self, session_id: str, stream: TextIO) -> None:
        self._session_id = session_id
        self._stream = stream

    def print_reply(self, session_id: str, text: str) -> str:
        if not session_id or session_id != self._session_id:
            return ""
        self._stream.write(text + "\n")
        self._stream.flush()
        return "printed:" + session_id
