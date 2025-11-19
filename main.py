import asyncio
import urllib.parse
import os
import re
from bs4 import BeautifulSoup

from quart import Quart, request, jsonify
from quart_cors import cors

# curl_cffi (impersonates browsers' TLS/JA3)
import curl_cffi

from playwright.async_api import async_playwright

app = Quart(__name__)
app = cors(app, allow_origin="*")

# Playwright globals (keep for fallback)
_playwright = None
_browser = None
_context = None

# Playwright args (you already had these; keep them)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CHROME_ARGS = [
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-zygote",
    "--single-process",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-web-security",
    "--blink-settings=imagesEnabled=false",
]


# ------------------------------
# Helper: extract redirect from HTML (same heuristics you used)
# ------------------------------
def extract_redirect_from_html(html):
    # 1) search obvious inline JS patterns
    patterns = [
        re.compile(r'c\.setAttribute\("href","([^"]+)"\)'),
        re.compile(r'window\.location(?:\.href)?\s*=\s*"([^"]+)"'),
        re.compile(r'location\.assign\(["\']([^"\']+)["\']\)'),
    ]
    for p in patterns:
        m = p.search(html)
        if m:
            return m.group(1)

    # 2) search for element with id="c" and href attribute
    soup = BeautifulSoup(html, "html.parser")
    c_el = soup.find(id="c")
    if c_el and c_el.get("href"):
        return c_el.get("href")

    # 3) search anchors that look like driveseed links
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if any(x in h for x in ("driveseed.org", "/zfile/", "/wfile/", "/file/")):
            return h

    # 4) fallback: first anchor
    a = soup.find("a", href=True)
    if a:
        return a["href"]

    return None


# ------------------------------
# curl_cffi fetch (synchronous call, fast)
# ------------------------------
def fetch_with_curl_cffi(url, timeout=30):
    """
    Returns tuple (status_code, final_url, html_text) or (None, None, None) on failure.
    We use impersonate="chrome" which makes TLS/JA3 and HTTP2 look like Chrome.
    """
    try:
        # curl_cffi exposes get() at top-level API
        r = curl_cffi.get(url, impersonate="chrome", timeout=timeout)
        # r behaves like requests.Response in many docs: r.status_code, r.text, r.url
        return getattr(r, "status_code", None), getattr(r, "url", url), getattr(r, "text", "")
    except Exception as e:
        # log to file for Render debugging
        with open("curl_cffi_error.log", "a") as f:
            f.write(f"curl_cffi error for {url}: {repr(e)}\n")
        return None, None, None


# ------------------------------
# Playwright init / fallback (unchanged, but keep safer flags)
# ------------------------------
@app.before_serving
async def init_browser():
    global _playwright, _browser, _context
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True, args=CHROME_ARGS)
    _context = await _browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
    # no pre-warm required, but you can if you want
    try:
        page = await _context.new_page()
        await page.goto("https://tech.unblockedgames.world/", timeout=30000)
        await asyncio.sleep(1)
        await page.close()
    except Exception:
        pass


@app.after_serving
async def close_browser():
    global _playwright, _browser, _context
    try:
        if _context: await _context.close()
        if _browser: await _browser.close()
        if _playwright: await _playwright.stop()
    except Exception:
        pass


# ------------------------------
# Primary endpoint
# ------------------------------
@app.route("/getlink")
async def get_link():
    global _context
    start_url = request.args.get("url")
    if not start_url:
        return jsonify({"error": "Missing ?url parameter"}), 400
    if _context is None:
        return jsonify({"error": "Browser not ready"}), 500

    # 1) Try fast curl_cffi fetch with TLS impersonation
    status, final_url, html = fetch_with_curl_cffi(start_url)
    if status is not None and html:
        # write debug output (optional)
        try:
            with open("debug_curl_cffi.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        redirect_candidate = extract_redirect_from_html(html)
        if redirect_candidate:
            # if candidate is relative, resolve it against final_url
            redirect_candidate = urllib.parse.urljoin(final_url, redirect_candidate)
            # follow it using curl_cffi (to get the final resolved URL)
            s2, final2, html2 = fetch_with_curl_cffi(redirect_candidate)
            if final2:
                final_url_used = final2
            else:
                final_url_used = redirect_candidate

            file_id = urllib.parse.urlparse(final_url_used).path.split("/")[-1]
            # Build the same variants you had
            variants = {
                "zfile": f"https://driveseed.org/zfile/{file_id}",
                "wfile_type1": f"https://driveseed.org/wfile/{file_id}?type=1",
                "wfile_type2": f"https://driveseed.org/wfile/{file_id}?type=2",
            }
            # Try to fetch variants with curl_cffi to find direct links
            results = {}
            for k, v in variants.items():
                st, fu, h = fetch_with_curl_cffi(v)
                if h:
                    results[k] = extract_redirect_from_html(h) or fu or None
                else:
                    results[k] = None

            return jsonify({"final_url": final_url_used, "file_id": file_id, "download_links": results})

    # 2) If curl_cffi route failed to find redirect, FALL BACK to Playwright (browser)
    page = await _context.new_page()
    try:
        # stealth-ish: set navigator.webdriver undefined
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)  # give site more time on Render
        try:
            await page.evaluate("()=>{const f=document.getElementById('landing');if(f&&f.submit)f.submit();}")
        except Exception:
            pass
        await asyncio.sleep(4)

        # reuse your existing JS-based extractor (keeps parity with local)
        async def snap():
            return await page.evaluate(
                r"""(() => {
    for (const s of document.scripts) {
        const t = s.textContent || "";
        let m;
        if ((m = /c\.setAttribute\("href","([^"]+)"\)/.exec(t))) return m[1];
        if ((m = /window\.location(?:\.href)?\s*=\s*"([^"]+)"/.exec(t))) return m[1];
        if ((m = /location\.assign\(["']([^"']+)["']\)/.exec(t))) return m[1];
    }
    const cEl = document.getElementById("c");
    if (cEl && cEl.getAttribute("href")) return cEl.getAttribute("href");
    for (const a of document.querySelectorAll("a[href]")) {
        const h = a.getAttribute("href");
        if (!h) continue;
        if (h.includes("driveseed.org") || h.includes("/zfile/") ||
            h.includes("/wfile/") || h.includes("/file/")) return h;
    }
    return null;
})();"""
            )

        redirect_url = None
        for _ in range(20):
            candidate = await snap()
            if candidate:
                redirect_url = candidate
                break
            await asyncio.sleep(1)

        if not redirect_url:
            # also dump page HTML for debugging
            try:
                html = await page.content()
                with open("debug_playwright.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass
            return jsonify({
                "error": "Redirect link not found",
                "debug_url": page.url,
                "debug_title": await page.title(),
            }), 504

        # resolve and follow redirect
        redirect_url = urllib.parse.urljoin(page.url, redirect_url)
        await page.goto(redirect_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        final_url = page.url
        file_id = urllib.parse.urlparse(final_url).path.split("/")[-1]

        variants = {
            "zfile": f"https://driveseed.org/zfile/{file_id}",
            "wfile_type1": f"https://driveseed.org/wfile/{file_id}?type=1",
            "wfile_type2": f"https://driveseed.org/wfile/{file_id}?type=2",
        }
        selectors = {
            "zfile": "#cf_captcha > div.card-body > div > a",
            "wfile_type1": "body > div > div > div.card-body > div > div > a",
            "wfile_type2": "body > div > div > div.card-body > div > div > a",
        }

        async def fetch_variant(context, key, url, selector):
            pg = await context.new_page()
            try:
                await pg.goto(url, wait_until="domcontentloaded", timeout=25000)
                el = await pg.query_selector(selector)
                if el:
                    href = await el.get_attribute("href")
                    if href:
                        return key, href
                html = await pg.content()
                soup = BeautifulSoup(html, "html.parser")
                a = soup.find("a", href=True)
                return key, (a["href"] if a else None)
            except Exception:
                return key, None
            finally:
                await pg.close()

        tasks = [fetch_variant(_context, k, v, selectors[k]) for k, v in variants.items()]
        results = dict(await asyncio.gather(*tasks))

        return jsonify({"final_url": final_url, "file_id": file_id, "download_links": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        await page.close()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
