"""Throwaway probe: capture raw responses from candidate sources on a GitHub runner."""
import base64, json, os, re, time, traceback
from pathlib import Path
import requests

OUT = Path("probe_out"); OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
H = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
CANDIDATES = {
  "hdfc_pdf_current": "https://www.hdfc.bank.in/content/dam/hdfcbankpws/in/en/personal-banking/discover-products/interest-rates/hdfc-bank-treasury-forex-card-rates.pdf",
}
summary = {}
for name, url in CANDIDATES.items():
    s = {"url": url}
    try:
        t = time.time(); r = requests.get(url, headers=H, timeout=30, allow_redirects=True)
        s["req"] = {"status": r.status_code, "final_url": r.url, "ctype": r.headers.get("Content-Type"), "len": len(r.content), "secs": round(time.time()-t, 2)}
        ext = "pdf" if r.content[:4] == b"%PDF" else "html"
        (OUT / f"{name}.requests.{ext}").write_bytes(r.content)
    except Exception as e:
        s["req"] = {"error": repr(e)}
    summary[name] = s

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
o = Options()
for a in ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1920,1080", f"user-agent={UA}", "--disable-blink-features=AutomationControlled"]:
    o.add_argument(a)
d = webdriver.Chrome(options=o)
for name, url in CANDIDATES.items():
    try:
        if url.endswith(".pdf"):
            d.get(url.rsplit("/", 1)[0] + "/")  # establish origin/cookies
            time.sleep(3)
            b64 = d.execute_async_script("""
              const cb = arguments[arguments.length-1];
              fetch(arguments[0], {credentials:'include'}).then(r => r.arrayBuffer()).then(buf => {
                let s=''; const u=new Uint8Array(buf); for (let i=0;i<u.length;i++) s+=String.fromCharCode(u[i]); cb(btoa(s));
              }).catch(e => cb('ERR:'+e));""", url)
            if b64.startswith("ERR:"):
                summary[name]["sel"] = {"error": b64}
            else:
                data = base64.b64decode(b64)
                (OUT / f"{name}.selenium.{'pdf' if data[:4]==b'%PDF' else 'html'}").write_bytes(data)
                summary[name]["sel"] = {"len": len(data), "is_pdf": data[:4] == b"%PDF"}
        else:
            d.get(url); time.sleep(15)
            html = d.page_source
            (OUT / f"{name}.selenium.html").write_text(html)
            d.save_screenshot(str(OUT / f"{name}.png"))
            pdfs = sorted(set(re.findall(r'["\'](\S+?\.pdf)["\']', html)))[:30]
            summary[name]["sel"] = {"title": d.title, "final_url": d.current_url, "len": len(html), "tables": html.count("<table"), "usd_hits": len(re.findall("USD|United States", html)), "pdf_links": pdfs}
    except Exception as e:
        summary[name]["sel"] = {"error": traceback.format_exc()[-400:]}
d.quit()
(OUT / "summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
