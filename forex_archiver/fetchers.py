"""Transport layer: turn a URL into bytes, or raise FetchError.

Two strategies share one signature, ``(url, timeout) -> bytes``, so a source
can declare a primary and a fallback without caring how either works.
"""

from __future__ import annotations

import logging
import re

import requests

from .errors import FetchError

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Signatures of bot walls / interstitials that come back as HTTP 200
_BOT_WALL = re.compile(rb"cf-chl-|Just a moment\.\.\.|Attention Required|Access Denied|captcha", re.I)


def check_body(body: bytes, url: str) -> bytes:
    if not body or len(body) < 512:
        raise FetchError(f"empty or tiny body ({len(body)} bytes) from {url}")
    if not body.startswith(b"%PDF") and _BOT_WALL.search(body[:20000]):
        raise FetchError(f"bot wall / challenge page from {url}")
    return body


def http_get(url: str, timeout: float = 30.0) -> bytes:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} from {resp.url}")
    return check_body(resp.content, url)


def browser_get(url: str, timeout: float = 45.0) -> bytes:
    """Render with headless Chrome and return the DOM after scripts run.

    Selenium Manager (bundled with selenium>=4.11) resolves a chromedriver that
    matches the installed Chrome, so there is no driver version to pin.
    """
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    opts = Options()
    for arg in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1920,1080",
        f"--user-agent={UA}",
        "--disable-blink-features=AutomationControlled",
    ):
        opts.add_argument(arg)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = None
    try:
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(timeout)
        driver.get(url)
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "table")))
        return check_body(driver.page_source.encode("utf-8"), url)
    except WebDriverException as exc:
        raise FetchError(f"browser: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}") from exc
    finally:
        if driver is not None:
            driver.quit()


FETCHERS = {"http": http_get, "browser": browser_get}
