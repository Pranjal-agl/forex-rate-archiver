"""Core data types shared across the library."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime


@dataclass(frozen=True, order=True)
class RateRecord:
    """One USD->INR quote from one bank for one rate type on one day."""

    bank: str
    label: str
    rate: float
    rate_date: date = field(default_factory=date.today, compare=False)

    def as_row(self) -> list:
        return [self.bank, self.label, self.rate, self.rate_date.isoformat()]

    @staticmethod
    def csv_header() -> list[str]:
        return ["Bank", "Slab_Type", "Rate", "Date"]


@dataclass(frozen=True)
class RawSnapshot:
    """The exact bytes a source returned, kept so parsing can be replayed later."""

    source: str
    url: str
    content: bytes
    kind: str  # "html" | "pdf"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class SourceResult:
    """Outcome of one source for one run, which feeds the source_health table."""

    source: str
    ok: bool
    records: list[RateRecord] = field(default_factory=list)
    latency_s: float = 0.0
    attempts: int = 0
    error_type: str | None = None  # FetchError | SchemaDriftError | ...
    error: str | None = None
    snapshot: RawSnapshot | None = None
