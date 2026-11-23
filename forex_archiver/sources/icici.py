"""ICICI Bank forex card rates: server-rendered HTML, parsed with BeautifulSoup.

Plain HTTP is enough (the table is in the initial HTML); headless Chrome is the
fallback if ICICI ever moves the table behind JavaScript.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

from ..errors import SchemaDriftError
from ..models import RateRecord
from .base import ParsedSheet, Source, check_pair, norm, to_rate

_DATE = re.compile(r"Last Updated on:\s*(\d{2})-(\d{2})-(\d{4})", re.I)
WANTED = {"TT Buying": "tt buying rate", "TT Selling": "tt selling rate"}


class ICICI(Source):
    name = "icici"
    bank = "ICICI"
    kind = "html"
    urls = ("https://www.icici.bank.in/business-banking/global-markets/forex/forex-card-rates",)
    strategies = ("http", "browser")
    headline_label = "TT Buying"

    def parse(self, content: bytes) -> ParsedSheet:
        soup = BeautifulSoup(content, "lxml")
        m = _DATE.search(soup.get_text(" ", strip=True))
        if not m:
            raise SchemaDriftError("ICICI: 'Last Updated on: dd-mm-yyyy' not found")
        as_of = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()

        table = soup.find("table", class_="exchange-rate-table")
        if table is None:
            raise SchemaDriftError("ICICI: table.exchange-rate-table not found")

        # Row 0 is the grouped header (Currency | Bank Buying | Bank Selling) with a
        # rowspan'd Currency cell, so the sub-header row has one cell fewer than data rows.
        sub = next((tr for tr in table.find_all("tr")
                    if any(norm(th.get_text()).lower() == "tt buying rate" for th in tr.find_all("th"))), None)
        if sub is None:
            raise SchemaDriftError("ICICI: 'TT Buying rate' header missing")
        headers = [norm(th.get_text()).lower() for th in sub.find_all("th")]
        cols = {}
        for label, text in WANTED.items():
            if text not in headers:
                raise SchemaDriftError(f"ICICI: header {text!r} missing; headers={headers}")
            cols[label] = headers.index(text) + 1  # +1 for the Currency cell

        usd = None
        for tr in table.find_all("tr"):
            tds = [norm(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if tds and "(USD)" in tds[0]:
                usd = tds
                break
        if usd is None:
            raise SchemaDriftError("ICICI: USD row missing")
        if len(usd) != len(headers) + 1:
            raise SchemaDriftError(f"ICICI: USD row has {len(usd)} cells, expected {len(headers) + 1}")

        vals = {label: to_rate(usd[i], where=f"ICICI USD {label}") for label, i in cols.items()}
        check_pair(vals["TT Buying"], vals["TT Selling"], where="ICICI TT")
        return ParsedSheet(as_of, [RateRecord(self.bank, k, v, as_of) for k, v in vals.items()])
