# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one ticket store call under a deadline: a call that runs past it is abandoned, never waited for."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError


class CallDeadlineError(Exception):
    """The call did not answer inside its deadline; it may still complete later."""


class CallFailedError(Exception):
    """The call raised; ``cause`` is the exception's type name (its message may hold a secret)."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def bounded[Request, Result](
    call: Callable[[Request], Result], request: Request, seconds: float
) -> Result:
    """Return ``call(request)``, or raise ``CallDeadlineError`` when it has not answered after ``seconds``."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(call, request)
    try:
        return future.result(timeout=seconds)
    except FutureTimeoutError as error:
        raise CallDeadlineError from error
    except Exception as error:
        raise CallFailedError(type(error).__name__) from error
    finally:
        executor.shutdown(wait=False)
