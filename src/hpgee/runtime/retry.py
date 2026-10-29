"""Bounded retries for explicitly idempotent Earth Engine reads."""

from __future__ import annotations

import builtins
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout

T = TypeVar("T")
LOGGER = logging.getLogger(__name__)


def is_transient(error: Exception, _seen: set[int] | None = None) -> bool:
    if isinstance(
        error,
        (
            ConnectionError,
            Timeout,
            ChunkedEncodingError,
            builtins.ConnectionError,
            builtins.TimeoutError,
        ),
    ):
        return True
    status = getattr(getattr(error, "resp", None), "status", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if status is not None:
        return status in (408, 429, 500, 502, 503, 504)
    # Explicit ``raise ... from`` chains can form cycles; visit each error once.
    seen = (_seen or set()) | {id(error)}
    cause = error.__cause__ or error.__context__
    return cause is not None and id(cause) not in seen and is_transient(cause, seen)


def retry_read(
    operation: Callable[[], T],
    *,
    attempts: int = 8,
    base_seconds: float = 5,
    cap_seconds: float = 60,
) -> T:
    """Retry transport and retryable HTTP failures; permission/validation errors propagate.

    The callable must be idempotent. This helper does not retry task submissions,
    mutations, or arbitrary exceptions. Logs omit URLs and credential details.
    """
    if attempts < 1 or base_seconds < 0 or cap_seconds < 0:
        raise ValueError("Invalid retry limits")
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            if attempt + 1 == attempts or not is_transient(error):
                raise
            delay = min(cap_seconds, base_seconds * 2**attempt)
            delay = min(cap_seconds, delay + random.random() * min(1, delay / 4))
            LOGGER.warning(
                "Read retry %s/%s: %s; waiting %.1fs",
                attempt + 1,
                attempts,
                type(error).__name__,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("Unreachable")
