"""
Headless=False debug tool for Gemini's sources/citations DOM.

Gemini (Angular) shows sources differently from Perplexity:
  - inline citation chips within the response
  - a "Sources" / "Show more" expander under the answer
  - sometimes a "Sources and related content" footer

This tool sends a prompt that should trigger web grounding, then dumps every
element that looks source-related so we can build a verified selector — same
methodology that solved Perplexity.

Reuses a warmed gemini profile from the DB if available.

Usage:
    python test_gemini_sources.py
"""
import asyncio
import os

os.environ["DEBUG_HEADLESS"] = "0"

from camoufox.async_api import AsyncCamoufox

URL = "https://gemini.google.com/app?hl=en"
INPUT_SEL = 'div.ql-editor[contenteditable="true"], div[contenteditable="true"], p[data-placeholder]'
RESPONSE_SEL = 'model-response'

KILL_OVERLAYS_JS = """() => {
    const style = document.createElement('style');
    style.innerHTML = `
        iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"],
        div[role="dialog"], .cdk-overlay-container, [class*="backdrop"], #credential_picker_container {
            display: none !important; opacity: 0 !important; pointer-events: none !important;
            z-index: -9999 !important; visibility: hidden !important;
        }
    `;
    document.head.appendChild(style);
}"""


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


def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) != 4:
        return None
    return {"server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}


async def main():
    profile_path, proxy_str = await find_warmed_profile()
    if profile_path and os.path.exists(profile_path):
        print(f"[TEST] Reusing warmed profile: {os.path.basename(profile_path)} (proxy: {proxy_str.split(':')[0] if proxy_str else 'none'})")
    else:
        profile_path = os.path.join(os.getcwd(), "profiles", "gemini", "_test_sources")
        os.makedirs(profile_path, exist_ok=True)
        proxy_str = None
        print(f"[TEST] No warmed profile — throwaway: {profile_path}")

    async with AsyncCamoufox(
        headless=False,
        persistent_context=True,
        user_data_dir=profile_path,
        proxy=parse_proxy(proxy_str),
        geoip=True,
        locale="en-US",
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        page.on("pageerror", lambda exc: None)

        async def route_handler(route):
            try:
                if route.request.resource_type == "media":
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                pass
        await page.route("**/*", route_handler)

        print("[TEST] Navigating...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector(INPUT_SEL, timeout=30000)
        await page.evaluate(KILL_OVERLAYS_JS)
        await asyncio.sleep(0.5)

        print("[TEST] Injecting prompt (use a query that forces web grounding)...")
        editor = page.locator(INPUT_SEL).first
        await editor.click()
        # A current-events / "according to recent sources" style prompt triggers grounding
        await page.keyboard.insert_text(
            "What are the best local SEO rank tracking tools in 2025? Cite your sources."
        )
        await asyncio.sleep(1.0)
        send = page.locator('button[aria-label*="Send"]').first
        if await send.count() > 0 and await send.is_visible():
            await send.click()
        else:
            await page.keyboard.press("Enter")

        print("[TEST] Waiting for response...")
        await asyncio.sleep(6)
        for _ in range(60):
            if await page.locator('button[aria-label*="Stop"]').count() == 0:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(4)

        resp_len = await page.evaluate(f"""() => {{
            const el = document.querySelector('{RESPONSE_SEL}');
            return el ? el.innerText.length : -1;
        }}""")
        print(f"[TEST] Response length: {resp_len} chars\n")

        # 1. Dump source/citation-related elements
        print("[TEST] === Elements with 'source'/'citation' in text or attrs ===")
        dump = await page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('*')) {
                const txt = (el.innerText || '').trim();
                const attrs = Array.from(el.attributes || []).map(a => a.name + '=' + a.value).join(' ');
                const hay = (txt + ' ' + attrs).toLowerCase();
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if ((hay.includes('source') || hay.includes('citation') || hay.includes('related content'))
                    && txt.length < 50) {
                    out.push({
                        tag: el.tagName,
                        text: txt.slice(0, 45),
                        clickable: el.tagName === 'BUTTON' || el.getAttribute('role') === 'button',
                        attrs: attrs.slice(0, 120),
                        pos: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) }
                    });
                }
            }
            return out;
        }""")
        for d in dump:
            print(f"  <{d['tag']}> text='{d['text']}' clickable={d['clickable']}")
            print(f"      attrs={d['attrs']}")

        # 2. Dump all external links currently in the DOM (before any expander click)
        print("\n[TEST] === External links currently in DOM ===")
        links = await page.evaluate("""() => {
            const seen = new Set();
            for (const a of document.querySelectorAll('a[href^="http"]')) {
                try {
                    const h = new URL(a.href).hostname;
                    if (!h.includes('google') && !h.includes('gstatic')) seen.add(a.href);
                } catch {}
            }
            return Array.from(seen);
        }""")
        print(f"  {len(links)} external links:")
        for l in links[:20]:
            print(f"    {l}")

        # 3. Look for Angular custom elements that hold citations
        print("\n[TEST] === Custom elements (Angular) present ===")
        customs = await page.evaluate("""() => {
            const tags = new Set();
            for (const el of document.querySelectorAll('*')) {
                if (el.tagName.includes('-')) tags.add(el.tagName.toLowerCase());
            }
            return Array.from(tags);
        }""")
        for c in customs:
            print(f"    <{c}>")

        # ── Click the FIRST source chip and dump the popover that opens ──
        print("\n[TEST] === Clicking first source chip to reveal popover URL ===")
        try:
            chip_btn = page.locator('button.multiple-button, source-inline-chip button, [class*="source-inline-chip"] button').first
            if await chip_btn.count() > 0:
                await chip_btn.scroll_into_view_if_needed()
                await asyncio.sleep(0.3)
                await chip_btn.click()
                await asyncio.sleep(2.0)
                popover = await page.evaluate("""() => {
                    const out = [];
                    // gem-popover / cdk overlay holds the expanded source detail
                    const containers = document.querySelectorAll('gem-popover, .cdk-overlay-container, sources-list, [role="tooltip"]');
                    for (const c of containers) {
                        for (const a of c.querySelectorAll('a[href]')) {
                            out.push({ text: (a.innerText||'').trim().slice(0,40), href: a.href });
                        }
                    }
                    return out;
                }""")
                print(f"  Popover links found: {len(popover)}")
                for p in popover:
                    print(f"    text='{p['text']}' -> {p['href']}")
            else:
                print("  No clickable source chip found.")
        except Exception as e:
            print(f"  Chip click failed: {e}")

        # ── Also try opening the full <sources-list> panel ──
        print("\n[TEST] === Looking for a sources-list / 'Sources' expander ===")
        try:
            expander = page.locator('sources-list button, button[aria-label*="ources"], button:has-text("Sources")').first
            if await expander.count() > 0 and await expander.is_visible():
                await expander.click()
                await asyncio.sleep(2.0)
                panel_links = await page.evaluate("""() => {
                    const out = [];
                    for (const a of document.querySelectorAll('sources-list a[href], .cdk-overlay-container a[href]')) {
                        out.push({ text: (a.innerText||'').trim().slice(0,40), href: a.href });
                    }
                    return out;
                }""")
                print(f"  sources-list panel links: {len(panel_links)}")
                for p in panel_links[:20]:
                    print(f"    text='{p['text']}' -> {p['href']}")
            else:
                print("  No sources-list expander visible.")
        except Exception as e:
            print(f"  Expander failed: {e}")

        # ── Final: all chip publisher names (always reliable) ──
        print("\n[TEST] === All source publisher names (chips) ===")
        names = await page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('.source-title, source-inline-chip .source-title')) {
                const t = (el.innerText||'').trim();
                if (t) out.push(t);
            }
            return out;
        }""")
        print(f"  {len(names)} names: {names}")

        print("\n[TEST] Browser stays open 120s. Inspect the popover/panel manually too.")
        print("[TEST] Ctrl+C to exit.\n")
        await asyncio.sleep(120)


if __name__ == "__main__":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n[TEST] Done.")
    finally:
        loop.close()
