"""The Source interface. Adding a bank means writing one subclass of this."""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from ..errors import FetchError, SchemaDriftError, StaleDataError
from ..fetchers import FETCHERS
from ..models import RateRecord, RawSnapshot, SourceResult
from ..retry import with_retries

logger = logging.getLogger(__name__)

Fetcher = Callable[[str, float], bytes]


@dataclass(frozen=True)
class ParsedSheet:
    as_of: date
    records: list[RateRecord]


class Source(ABC):
    """One bank publication.

    Subclasses declare *where* (``urls``), *how* (``strategies``) and *what*
    (``parse``). Fetching and parsing are deliberately separate: ``parse`` is a
    pure function of bytes, so it can be tested on saved fixtures and replayed
    on archived snapshots after a parser fix.
    """

    name: str            # short id, e.g. "sbi"
    bank: str            # value written to the Bank column
    kind: str            # "html" | "pdf"
    urls: tuple[str, ...]
    strategies: tuple[str, ...] = ("http",)   # tried in order per URL
    headline_label: str  # the rate compared across banks
    max_age_days: int = 5  # weekends + a holiday; anything older is a stale sheet

    # --- to implement -------------------------------------------------
    @abstractmethod
    def parse(self, content: bytes) -> ParsedSheet:
        """Bytes -> records. Raise SchemaDriftError rather than guess."""

    # --- shared machinery ---------------------------------------------
    def run(
        self,
        *,
        today: date | None = None,
        attempts: int = 3,
        timeout: float = 30.0,
        fetchers: dict[str, Fetcher] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> SourceResult:
        """Fetch -> parse -> freshness check, never raising. Returns a SourceResult."""
        today = today or date.today()
        fetchers = fetchers or FETCHERS
        t0 = time.monotonic()
        total_attempts = 0
        last_exc: Exception | None = None
        last_snap: RawSnapshot | None = None

        for url in self.urls:
            for strategy in self.strategies:
                fetch = fetchers[strategy]
                try:
                    content, n = with_retries(
                        lambda f=fetch, u=url: f(u, timeout), attempts=attempts, sleep=sleep, label=f"{self.name}/{strategy}"
                    )
                    total_attempts += n
                    snap = RawSnapshot(source=self.name, url=url, content=content, kind=self.kind)
                    sheet = self.parse(content)
                    age = (today - sheet.as_of).days
                    if age > self.max_age_days:
                        raise StaleDataError(f"sheet dated {sheet.as_of} is {age} days old (max {self.max_age_days})")
                    if age < -1:
                        raise SchemaDriftError(f"sheet dated {sheet.as_of} is in the future")
                    logger.info("%s: %d record(s) as of %s via %s", self.name, len(sheet.records), sheet.as_of, strategy)
                    return SourceResult(self.name, True, sheet.records, time.monotonic() - t0, total_attempts, snapshot=snap)
                except FetchError as exc:
                    total_attempts += attempts
                    last_exc = exc
                    logger.warning("%s: fetch failed via %s for %s: %s", self.name, strategy, url, exc)
                except (SchemaDriftError, StaleDataError) as exc:
                    # bytes arrived but are wrong; another URL may still be good
                    last_exc = exc
                    logger.error("%s: %s: %s", self.name, type(exc).__name__, exc)
                    if isinstance(exc, SchemaDriftError):
                        return self._fail(exc, t0, total_attempts, snap)
                    last_snap = snap
                    break  # stale: try next URL, the other strategy would fetch the same file

        assert last_exc is not None
        return self._fail(last_exc, t0, total_attempts, last_snap)

    def _fail(self, exc: Exception, t0: float, attempts: int, snap: RawSnapshot | None) -> SourceResult:
        return SourceResult(
            self.name, False, [], time.monotonic() - t0, attempts,
            error_type=type(exc).__name__, error=str(exc)[:500], snapshot=snap,
        )


# --- parsing helpers shared by sources ------------------------------------

_NUM = re.compile(r"^-?\d{1,4}(?:,\d{3})*(?:\.\d+)?$")


def to_rate(text: str | None, *, where: str) -> float:
    """Strict numeric parse. '91.28' ok; '', '-', 'N/A', '9l.28' -> SchemaDriftError."""
    s = (text or "").strip().replace("₹", "").replace("Rs.", "").strip()
    if not _NUM.match(s):
        raise SchemaDriftError(f"non-numeric rate {text!r} at {where}")
    return float(s.replace(",", ""))


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def check_pair(buy: float, sell: float, *, where: str) -> None:
    """Banks always sell above what they buy. If not, we picked the wrong columns."""
    if not buy < sell:
        raise SchemaDriftError(f"buy {buy} >= sell {sell} at {where}: columns probably shifted")


