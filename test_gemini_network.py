"""
DEFINITIVE Gemini source-URL hunt — captures URLs at TWO layers:

  LAYER 1 (network): intercept every response body Gemini receives. Grounded
                     source URLs travel in the StreamGenerate/batchexecute API
                     payload BEFORE the DOM obfuscates them. Most bulletproof.

  LAYER 2 (DOM popover): click each chip slowly, wait longer, read the
                         cdk-overlay link (this worked once -> daxrm.com article).

Whichever layer reliably yields real article URLs becomes the production method.

NOTE on your question (knowledge vs web-search URLs):
  A source chip ONLY exists when Gemini grounded the answer via web search.
  Knowledge-only answers produce zero chips / zero network source payload, so
  this can never mislabel a training-knowledge URL as a source.

Usage:
    python test_gemini_network.py
"""
import asyncio
import os
import re

os.environ["DEBUG_HEADLESS"] = "0"

from camoufox.async_api import AsyncCamoufox

URL = "https://gemini.google.com/app?hl=en"
INPUT_SEL = 'div.ql-editor[contenteditable="true"], div[contenteditable="true"], p[data-placeholder]'

# Collect candidate URLs seen on the network
network_urls = set()
# Also capture WHICH endpoint each came from, for diagnosis
network_hits = []
JUNK = ('google.com', 'gstatic', 'googleapis', 'gemini.google', 'youtube.com',
        'ggpht', 'googleusercontent', 'gvt1', 'gvt2', 'schema.org', 'w3.org',
        'googletagmanager', 'google-analytics', 'doubleclick')


def harvest_from_text(text):
    """Pull http(s) URLs out of any blob (JSON/HTML/escaped)."""
    found = set()
    cleaned = text.replace('\\/', '/').replace('\\u003d', '=').replace('\\u0026', '&')
    for m in re.findall(r'https?://[^\s"\\<>)\]]+', cleaned):
        u = m.rstrip('.,;)\'"')
        if len(u) < 12 or '://' not in u:
            continue
        parts = u.split('/')
        if len(parts) < 3:
            continue
        host = parts[2].lower()
        if any(j in host for j in JUNK):
            continue
        found.add(u)
    return found


async def find_warmed_profile():
    try:
        from src.database import DatabaseManager
        db = DatabaseManager()
        await db.connect()
        row = await db.fetchrow(
            "SELECT storage_path, proxy_string FROM browser_profiles "
            "WHERE engine_type = 'gemini' AND status = 'AVAILABLE' LIMIT 1"
        )
        await db.close()
        if row:
            return row["storage_path"], row.get("proxy_string")
    except Exception as e:
        print(f"[TEST] DB query failed: {e}")
    return None, None


def parse_proxy(p):
    if not p:
        return None
    parts = p.split(":")
    return {"server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]} if len(parts) == 4 else None


async def main():
    profile_path, proxy_str = await find_warmed_profile()
    if not (profile_path and os.path.exists(profile_path)):
        profile_path = os.path.join(os.getcwd(), "profiles", "gemini", "_test_sources")
        os.makedirs(profile_path, exist_ok=True)
        proxy_str = None
        print("[TEST] No warmed profile — throwaway (may not ground).")
    else:
        print(f"[TEST] Reusing warmed profile: {os.path.basename(profile_path)}")

    async with AsyncCamoufox(
        headless=False, persistent_context=True, user_data_dir=profile_path,
        proxy=parse_proxy(proxy_str), geoip=True, locale="en-US",
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        page.on("pageerror", lambda exc: None)

        # ── LAYER 1: capture every response body from generation endpoints ──
        async def on_response(resp):
            try:
                url = resp.url
                # Gemini's generation / RPC endpoints carry the grounding payload
                if any(k in url for k in ('StreamGenerate', 'batchexecute', 'GenerateContent',
                                          'assistant', 'BardFrontend', 'lamda')):
                    body = await resp.text()
                    hits = harvest_from_text(body)
                    if hits:
                        endpoint = url.split('?')[0].split('/')[-1]
                        for h in hits:
                            if h not in network_urls:
                                network_urls.add(h)
                                network_hits.append((endpoint, h))
                        print(f"[NET] {endpoint} -> +{len(hits)} candidate urls")
            except Exception:
                pass

        page.on("response", on_response)

        print("[TEST] Navigating...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector(INPUT_SEL, timeout=30000)
        await asyncio.sleep(0.5)

        editor = page.locator(INPUT_SEL).first
        await editor.click()
        await page.keyboard.insert_text(
            "Search the web and list the best local SEO rank tracking tools in 2025. "
            "Give me the source article links."
        )
        await asyncio.sleep(1.0)
        send = page.locator('button[aria-label*="Send"]').first
        if await send.count() > 0:
            await send.click()
        else:
            await page.keyboard.press("Enter")

        print("[TEST] Waiting for response + network capture...")
        await asyncio.sleep(8)
        for _ in range(70):
            if await page.locator('button[aria-label*="Stop"]').count() == 0:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(5)  # let trailing network settle

        chips = await page.evaluate("""() => document.querySelectorAll('.source-inline-chip-container button, source-inline-chip button, button.multiple-button').length""")
        print(f"\n[TEST] source chips on page: {chips}")

        print(f"\n[LAYER 1 — NETWORK] captured {len(network_urls)} candidate article URLs:")
        for endpoint, u in network_hits:
            print(f"    [{endpoint}] {u}")

        # ── LAYER 2: slow, careful per-chip popover read ──
        print(f"\n[LAYER 2 — DOM POPOVER] clicking {min(chips,6)} chips slowly...")
        dom_urls = set()
        for i in range(min(chips, 6)):
            try:
                await page.evaluate("""(idx) => {
                    const b = document.querySelectorAll('.source-inline-chip-container button, source-inline-chip button, button.multiple-button')[idx];
                    if (!b) return;
                    b.scrollIntoView({block:'center', behavior:'instant'});
                    const r = b.getBoundingClientRect();
                    const o = {bubbles:true, cancelable:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2, view:window};
                    b.dispatchEvent(new PointerEvent('pointerdown', o));
                    b.dispatchEvent(new PointerEvent('pointerup', o));
                    b.dispatchEvent(new MouseEvent('click', o));
                }""", i)
                await asyncio.sleep(2.5)   # wait longer — real link loads after cookie chrome
                links = await page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('.cdk-overlay-container a[href^="http"], gem-popover a[href^="http"], [role="tooltip"] a[href^="http"], inline-source-card a[href^="http"]').forEach(a => out.push(a.href));
                    return [...new Set(out)];
                }""")
                real = [l for l in links if len(l.split('/')) > 2 and not any(j in l.split('/')[2].lower() for j in JUNK)]
                print(f"   chip[{i}]: {real if real else '(only google chrome links)'}")
                for l in real:
                    dom_urls.add(l)
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"   chip[{i}] err: {e}")

        print(f"\n[LAYER 2 — DOM] {len(dom_urls)} article URLs from popovers:")
        for u in sorted(dom_urls):
            print(f"    {u}")

        print("\n" + "=" * 60)
        print(f"VERDICT: network={len(network_urls)} urls | popover={len(dom_urls)} urls")
        print("=" * 60)
        print("\n[TEST] Browser open 90s. Inspect manually too. Ctrl+C to exit.\n")
        await asyncio.sleep(90)


if __name__ == "__main__":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n[TEST] Done.")
    finally:
        loop.close()
