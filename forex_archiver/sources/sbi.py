"""SBI New York remittance rates: Selenium-rendered page, parsed with BeautifulSoup.

The page shows one table headed "Exchange rate for MM/DD/YYYY (Updated at ...)"
with a "Remittance Amount | Rupees" row and one row per USD slab.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

from ..errors import SchemaDriftError
from ..models import RateRecord
from .base import ParsedSheet, Source, norm, to_rate

_DATE = re.compile(r"Exchange rate for\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
EXPECTED_SLABS = {
    "less than $2,500",
    "$2,500 to less than $5,000",
    "$5,000 to less than $25,000",
    "$25,000 to less than $100,000",
}


class SBINewYork(Source):
    name = "sbi"
    bank = "SBI"
    kind = "html"
    urls = ("https://sbinewyork.statebank/exchange-rate",)
    strategies = ("browser", "http")   # Selenium first; plain HTTP if Chrome is unavailable
    headline_label = "less than $2,500"

    def parse(self, content: bytes) -> ParsedSheet:
        soup = BeautifulSoup(content, "lxml")
        table = next(
            (t for t in soup.find_all("table") if "remittance amount" in t.get_text(" ", strip=True).lower()),
            None,
        )
        if table is None:
            raise SchemaDriftError("SBI: no table containing 'Remittance Amount'")

        m = _DATE.search(table.get_text(" ", strip=True))
        if not m:
            raise SchemaDriftError("SBI: 'Exchange rate for <date>' caption missing")
        as_of = datetime.strptime(m.group(1), "%m/%d/%Y").date()

        records: list[RateRecord] = []
        for tr in table.find_all("tr"):
            cells = [norm(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(cells) != 2 or cells[0] not in EXPECTED_SLABS:
                continue
            records.append(RateRecord(self.bank, cells[0], to_rate(cells[1], where=f"SBI {cells[0]}"), as_of))

        found = {r.label for r in records}
        if found != EXPECTED_SLABS:
            raise SchemaDriftError(f"SBI: slabs changed; missing={sorted(EXPECTED_SLABS - found)}")
        rates = [r.rate for r in sorted(records, key=lambda r: _slab_order(r.label))]
        if rates != sorted(rates):
            raise SchemaDriftError(f"SBI: slab rates not non-decreasing {rates}; rows probably misaligned")
        return ParsedSheet(as_of, records)


def _slab_order(label: str) -> int:
    return ["less than $2,500", "$2,500 to less than $5,000", "$5,000 to less than $25,000",
            "$25,000 to less than $100,000"].index(label)
