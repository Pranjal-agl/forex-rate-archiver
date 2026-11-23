"""Exponential backoff with full jitter."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from .errors import FetchError

T = TypeVar("T")
logger = logging.getLogger(__name__)


def backoff_delays(attempts: int, base: float = 2.0, cap: float = 30.0, rng: random.Random | None = None) -> list[float]:
    """Delays to sleep *between* attempts: full jitter over min(cap, base * 2**i)."""
    rng = rng or random.Random()
    return [rng.uniform(0, min(cap, base * 2**i)) for i in range(attempts - 1)]


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base: float = 2.0,
    cap: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (FetchError,),
    sleep: Callable[[float], None] = time.sleep,
    label: str = "",
) -> tuple[T, int]:
    """Call ``fn`` until it succeeds; return (result, attempts_used).

    Only exceptions in ``retry_on`` are retried. Anything else (notably
    SchemaDriftError) propagates immediately, because retrying a parse of a
    changed layout just wastes time and hides the real problem.
    """
    delays = backoff_delays(attempts, base, cap)
    for i in range(attempts):
        try:
            return fn(), i + 1
        except retry_on as exc:
            if i == attempts - 1:
                raise
            logger.warning("%s attempt %d/%d failed: %s; retrying in %.1fs", label, i + 1, attempts, exc, delays[i])
            sleep(delays[i])
    raise AssertionError("unreachable")  # pragma: no cover
