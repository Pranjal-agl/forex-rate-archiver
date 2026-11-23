"""Source.run: retries, fallbacks, freshness. Uses canned bytes, never the network."""

from __future__ import annotations

import random

from conftest import GOOD_ROUTES, HDFC_PDF, HDFC_STALE_PDF, ICICI_HTML, RUN_DATE, SBI_HTML, fake_fetchers, load

from forex_archiver.errors import FetchError, SchemaDriftError
from forex_archiver.retry import backoff_delays, with_retries
from forex_archiver.sources import HDFC, ICICI, SBINewYork

NOSLEEP = lambda s: None  # noqa: E731


def test_backoff_is_exponential_jittered_and_capped():
    rng = random.Random(0)
    d = backoff_delays(6, base=2, cap=10, rng=rng)
    assert len(d) == 5
    assert all(0 <= x <= min(10, 2 * 2**i) for i, x in enumerate(d))


def test_with_retries_retries_fetch_errors_only():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise FetchError("503")
        return "ok"

    assert with_retries(flaky, attempts=3, sleep=NOSLEEP) == ("ok", 3)

    drift_calls = []

    def drifting():
        drift_calls.append(1)
        raise SchemaDriftError("layout changed")

    try:
        with_retries(drifting, attempts=5, sleep=NOSLEEP)
    except SchemaDriftError:
        pass
    assert len(drift_calls) == 1, "schema drift must not be retried"


def test_sbi_falls_back_from_browser_to_http():
    f = fake_fetchers({})
    calls = f["calls"]

    def browser(url, timeout):
        calls.append(("browser", url))
        raise FetchError("chrome not installed")

    f["browser"] = browser
    f["http"] = lambda url, timeout: (calls.append(("http", url)), load(SBI_HTML))[1]
    res = SBINewYork().run(today=RUN_DATE, attempts=2, fetchers=f, sleep=NOSLEEP)
    assert res.ok and len(res.records) == 4
    assert [c[0] for c in calls] == ["browser", "browser", "http"]


def test_hdfc_skips_stale_url_and_uses_next():
    src = HDFC()
    src.urls = ("https://x/forex-rates/forex-rates.pdf", "https://x/hdfc-bank-treasury-forex-card-rates.pdf")
    res = src.run(today=RUN_DATE, fetchers=fake_fetchers(GOOD_ROUTES), sleep=NOSLEEP)
    assert res.ok
    assert res.records[0].rate_date.isoformat() == "2026-11-23"


def test_hdfc_only_stale_sheet_fails_loudly_and_keeps_snapshot():
    """The exact failure that froze the old archive at 94.24: must now be an error, not data."""
    res = HDFC().run(today=RUN_DATE, fetchers=fake_fetchers({"hdfc": load(HDFC_STALE_PDF)}), sleep=NOSLEEP)
    assert not res.ok
    assert res.error_type == "StaleDataError"
    assert "2025-01-13" in res.error
    assert res.snapshot is not None and res.snapshot.content.startswith(b"%PDF")


def test_drift_fails_without_retry_and_keeps_bad_bytes():
    f = fake_fetchers({"icici": b"<html><table><tr><td>redesigned</td></tr></table>" + b" " * 1000 + b"</html>"})
    res = ICICI().run(today=RUN_DATE, attempts=3, fetchers=f, sleep=NOSLEEP)
    assert not res.ok and res.error_type == "SchemaDriftError"
    assert len(f["calls"]) == 1
    assert res.snapshot is not None


def test_network_failure_exhausts_retries_on_every_strategy():
    f = fake_fetchers({"icici": FetchError("timeout")})
    res = ICICI().run(today=RUN_DATE, attempts=3, fetchers=f, sleep=NOSLEEP)
    assert not res.ok and res.error_type == "FetchError"
    assert len(f["calls"]) == 6  # 3 http + 3 browser


def test_future_dated_sheet_is_drift():
    res = ICICI().run(today=RUN_DATE.replace(year=2025), fetchers=fake_fetchers({"icici": load(ICICI_HTML)}),
                      sleep=NOSLEEP)
    assert res.error_type == "SchemaDriftError"


def test_hdfc_current_ok():
    res = HDFC().run(today=RUN_DATE, fetchers=fake_fetchers({"hdfc": load(HDFC_PDF)}), sleep=NOSLEEP)
    assert res.ok and res.attempts == 1
