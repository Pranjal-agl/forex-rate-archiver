"""Parsers against real saved bank pages, plus fault injection on those same pages.

Every injected fault must raise SchemaDriftError (or FetchError for transport-level
junk) instead of returning a number. A parser that guesses is how an archive ends up
with plausible-looking wrong data.
"""

from __future__ import annotations

import io
import re
from datetime import date

import pdfplumber
import pytest
from conftest import HDFC_PDF, HDFC_STALE_PDF, ICICI_HTML, SBI_HTML, load

from forex_archiver.errors import FetchError, SchemaDriftError
from forex_archiver.fetchers import check_body
from forex_archiver.sources import HDFC, ICICI, SBINewYork

# ── happy path on real snapshots ────────────────────────────────────────────


def as_dict(sheet):
    return {r.label: r.rate for r in sheet.records}


def test_sbi_parses_real_page():
    sheet = SBINewYork().parse(load(SBI_HTML))
    assert sheet.as_of == date(2026, 11, 20)  # the page's own date, not the fetch date
    assert as_dict(sheet) == {
        "less than $2,500": 91.87,
        "$2,500 to less than $5,000": 91.87,
        "$5,000 to less than $25,000": 91.99,
        "$25,000 to less than $100,000": 92.10,
    }
    assert {r.rate_date for r in sheet.records} == {date(2026, 11, 20)}


def test_hdfc_parses_real_pdf():
    sheet = HDFC().parse(load(HDFC_PDF))
    assert sheet.as_of == date(2026, 11, 23)
    assert as_dict(sheet) == {"TT Buying": 90.93, "TT Selling": 94.22, "Cash Buying": 90.15, "Cash Selling": 95.00}


def test_hdfc_stale_pdf_parses_but_reports_its_old_date():
    """The legacy URL still serves a 2025 sheet. Parsing is fine; the freshness check must reject it."""
    sheet = HDFC().parse(load(HDFC_STALE_PDF))
    assert sheet.as_of == date(2025, 1, 13)


def test_hdfc_parses_sheet_published_without_spaces():
    """Regression: on 2026-11-24 HDFC's PDF had all spaces stripped ('CurrencyType', 'T.T.Buying(Inw')."""
    sheet = HDFC().parse(load("hdfc_nospaces_2026-11-24.pdf"))
    assert sheet.as_of == date(2026, 11, 24)
    d = as_dict(sheet)
    assert set(d) == {"TT Buying", "TT Selling", "Cash Buying", "Cash Selling"}
    assert d["TT Buying"] < d["TT Selling"] and 80 < d["TT Buying"] < 110


def test_icici_parses_real_page():
    sheet = ICICI().parse(load(ICICI_HTML))
    assert sheet.as_of == date(2026, 11, 23)
    assert as_dict(sheet) == {"TT Buying": 91.28, "TT Selling": 94.18}


# ── fault injection ─────────────────────────────────────────────────────────

def _sub(content: bytes, pattern: str, repl: str, count: int = 1) -> bytes:
    text = content.decode("utf-8", errors="replace")
    new, n = re.subn(pattern, repl, text, count=count, flags=re.S)
    assert n, f"fault injection pattern not found: {pattern}"
    return new.encode()


SBI = load(SBI_HTML)
ICI = load(ICICI_HTML)

HTML_FAULTS = [
    # (id, source, mutated bytes)
    ("sbi-header-renamed", SBINewYork, _sub(SBI, "Remittance Amount", "Transfer Value")),
    ("sbi-date-caption-gone", SBINewYork, _sub(SBI, r"Exchange rate for\s+\d+/\d+/\d+", "Exchange rate")),
    ("sbi-rate-not-numeric", SBINewYork, _sub(SBI, r">\s*91\.87\s*<", ">N/A<")),
    ("sbi-rate-typo", SBINewYork, _sub(SBI, r">\s*91\.99\s*<", ">9l.99<")),
    ("sbi-slab-row-missing", SBINewYork, _sub(SBI, r"<tr>(?:(?!</tr>).)*?\$5,000\s*to less than.*?</tr>", "")),
    ("sbi-rows-misaligned", SBINewYork, _sub(_sub(SBI, r">\s*92\.10\s*<", ">XX<"), r">\s*91\.87\s*<", ">92.10<")
     .replace(b">XX<", b">91.87<")),
    ("sbi-table-js-only", SBINewYork, _sub(SBI, r"<table.*?</table>", "<div id='app'></div>", count=0)),
    ("icici-date-gone", ICICI, _sub(ICI, r"(?i)Last Updated on:", "Updated:", count=0)),
    ("icici-table-class-changed", ICICI, _sub(ICI, r'class="exchange-rate-table"', 'class="fx-table"', count=0)),
    ("icici-header-renamed", ICICI, _sub(ICI, r"TT Buying rate", "Telegraphic Buy")),
    ("icici-usd-row-missing", ICICI, _sub(ICI, r"United States Dollar \(USD\)", "US Dollar", count=0)),
    ("icici-shifted-columns", ICICI, _sub(ICI, r"(United States Dollar \(USD\).*?</td>)\s*<td[^>]*>.*?</td>", r"\1")),
    ("icici-buy-sell-swapped", ICICI, _sub(_sub(ICI, r"(\(USD\).*?)91\.28", r"\1@@", ), r"(\(USD\).*?)94\.18", r"\g<1>91.28")
     .replace(b"@@", b"94.18")),
    ("icici-rate-dash", ICICI, _sub(ICI, r"(\(USD\).*?)91\.28", r"\1-")),
]


@pytest.mark.parametrize("case,source,content", HTML_FAULTS, ids=[c[0] for c in HTML_FAULTS])
def test_html_fault_is_caught(case, source, content):
    with pytest.raises(SchemaDriftError):
        source().parse(content)


def _hdfc_extract():
    with pdfplumber.open(io.BytesIO(load(HDFC_PDF))) as pdf:
        page = pdf.pages[0]
        return page.extract_text(), page.extract_tables()


def _drop_column(tables, col):
    return [[row[:col] + row[col + 1:] for row in t] for t in tables]


def _edit_usd(tables, col, value):
    out = []
    for t in tables:
        out.append([r[:col] + [value] + r[col + 1:] if len(r) > 1 and r[1] == "USD" else r for r in t])
    return out


TEXT, TABLES = _hdfc_extract()
HEADER = next(r for t in TABLES for r in t if r and r[0] == "Currency Type")
TT_BUY = next(i for i, h in enumerate(HEADER) if h and h.startswith("T.T. Buying"))

PDF_FAULTS = [
    ("hdfc-date-gone", TEXT.replace("DATE:", "DT"), TABLES),
    ("hdfc-header-column-dropped", TEXT, [[r[:TT_BUY] + r[TT_BUY + 1:] if r and r[0] == "Currency Type" else r for r in t]
                                          for t in TABLES]),
    ("hdfc-data-shifted-left", TEXT, [[r[:2] + r[3:] if len(r) > 1 and r[1] == "USD" else r for r in t] for t in TABLES]),
    ("hdfc-whole-table-shifted", TEXT, _drop_column(TABLES, 0)),
    ("hdfc-usd-row-missing", TEXT, [[r for r in t if not (len(r) > 1 and r[1] == "USD")] for t in TABLES]),
    ("hdfc-rate-blank", TEXT, _edit_usd(TABLES, TT_BUY, "")),
    ("hdfc-rate-dash", TEXT, _edit_usd(TABLES, TT_BUY, "-")),
    ("hdfc-buy-above-sell", TEXT, _edit_usd(TABLES, TT_BUY, "99.99")),
    ("hdfc-no-tables", TEXT, []),
]


@pytest.mark.parametrize("case,text,tables", PDF_FAULTS, ids=[c[0] for c in PDF_FAULTS])
def test_pdf_fault_is_caught(case, text, tables):
    with pytest.raises(SchemaDriftError):
        HDFC().parse_extracted(text, tables)


@pytest.mark.parametrize("name,content", [
    ("truncated-pdf", load(HDFC_PDF)[: len(load(HDFC_PDF)) // 3]),
    ("html-instead-of-pdf", load(SBI_HTML)),
    ("garbage-bytes", b"%PDF-1.7\n" + bytes(range(256)) * 50),
])
def test_corrupt_pdf_is_caught(name, content):
    with pytest.raises(SchemaDriftError):
        HDFC().parse(content)


@pytest.mark.parametrize("body", [
    b"",
    b"<html>short</html>",
    b"<html><title>Just a moment...</title>" + b" " * 2000 + b"</html>",
    b"<html><body>Access Denied" + b"." * 2000 + b"</body></html>",
])
def test_bot_walls_and_empty_bodies_are_fetch_errors(body):
    with pytest.raises(FetchError):
        check_body(body, "https://example.test")


def test_fault_suite_size():
    """Keeps the README claim honest: count of distinct injected parser faults."""
    assert len(HTML_FAULTS) + len(PDF_FAULTS) + 3 + 4 >= 30
