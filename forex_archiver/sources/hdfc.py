"""HDFC Bank Treasury Forex Card Rates: daily PDF parsed with pdfplumber.

Columns are located by header text, not position, and the sheet's own
"DATE: dd-mm-yyyy" is used as the rate date. The legacy URL that froze the
archive at 94.24 is kept as a second candidate on purpose: it still serves an
old sheet, and the freshness check in Source.run rejects it.
"""

from __future__ import annotations

import io
import re
from datetime import datetime

import pdfplumber

from ..errors import SchemaDriftError
from ..models import RateRecord
from .base import ParsedSheet, Source, check_pair, norm, to_rate

_DATE = re.compile(r"DATE:\s*(\d{2})[-/](\d{2})[-/](\d{4})", re.I)

# output label -> predicate on the normalised header text
COLUMNS = {
    "TT Buying": lambda h: h.startswith("t.t. buying"),
    "TT Selling": lambda h: h.startswith("t.t. selling"),
    "Cash Buying": lambda h: h == "cash buying",
    "Cash Selling": lambda h: h == "cash selling",
}


class HDFC(Source):
    name = "hdfc"
    bank = "HDFC"
    kind = "pdf"
    urls = (
        "https://www.hdfc.bank.in/content/dam/hdfcbankpws/in/en/personal-banking/discover-products/"
        "interest-rates/hdfc-bank-treasury-forex-card-rates.pdf",
        "https://www.hdfc.bank.in/content/dam/hdfcbankpws/in/en/personal-banking/resources/forex-rates/forex-rates.pdf",
    )
    strategies = ("http",)
    headline_label = "TT Buying"

    def parse(self, content: bytes) -> ParsedSheet:
        if not content.startswith(b"%PDF"):
            raise SchemaDriftError("HDFC: response is not a PDF")
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                page = pdf.pages[0]
                text = page.extract_text() or ""
                tables = page.extract_tables()
        except Exception as exc:  # pdfminer raises a zoo of types on corrupt input
            raise SchemaDriftError(f"HDFC: unreadable PDF ({type(exc).__name__}: {exc})") from exc
        return self.parse_extracted(text, tables)

    def parse_extracted(self, text: str, tables: list[list[list[str | None]]]) -> ParsedSheet:
        """Interpret pdfplumber output. Split out so tests can feed mutated tables."""
        m = _DATE.search(text)
        if not m:
            raise SchemaDriftError("HDFC: 'DATE: dd-mm-yyyy' not found")
        as_of = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()

        for table in tables:
            header_idx = next((i for i, row in enumerate(table) if row and norm(row[0]).lower() == "currency type"), None)
            if header_idx is None:
                continue
            headers = [norm(h).lower() for h in table[header_idx]]
            cols = {}
            for label, pred in COLUMNS.items():
                idx = next((i for i, h in enumerate(headers) if pred(h)), None)
                if idx is None:
                    raise SchemaDriftError(f"HDFC: column for {label!r} missing; headers={headers}")
                cols[label] = idx
            usd = next((row for row in table[header_idx + 1:] if len(row) > 1 and norm(row[1]) == "USD"), None)
            if usd is None:
                raise SchemaDriftError("HDFC: USD row missing")
            vals = {label: to_rate(usd[i], where=f"HDFC USD {label}") for label, i in cols.items()}
            check_pair(vals["TT Buying"], vals["TT Selling"], where="HDFC TT")
            check_pair(vals["Cash Buying"], vals["Cash Selling"], where="HDFC cash")
            return ParsedSheet(as_of, [RateRecord(self.bank, k, v, as_of) for k, v in vals.items()])

        raise SchemaDriftError("HDFC: rate table with 'Currency Type' header not found")
