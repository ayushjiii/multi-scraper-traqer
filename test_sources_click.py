"""
Headless=False debug tool that tries multiple strategies to OPEN the Perplexity
sources panel and verifies success via the Radix `data-state="open"` attribute.

Mirrors the real worker's environment:
  - atomic insert_text (Google One-Tap popup cannot interrupt it)
  - full CSS layer hider + aggressive Google iframe killer BEFORE typing
  - reuses a real warmed profile from the DB if one is AVAILABLE (more trust,
    popup already handled) — falls back to a throwaway profile otherwise.

Usage:
    python test_sources_click.py
"""
import asyncio
import os

os.environ["DEBUG_HEADLESS"] = "0"

from camoufox.async_api import AsyncCamoufox

URL = "https://www.perplexity.ai"
INPUT_SEL = 'textarea, [contenteditable="true"], input[type="text"]'

KILL_OVERLAYS_JS = """() => {
    const style = document.createElement('style');
    style.innerHTML = `
        iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"],
        iframe[src*="accounts.google"], iframe[src*="gsi"], #credential_picker_container,
        div[role="dialog"], .cdk-overlay-container, [class*="backdrop"], #cookie-consent,
        [id*="credential_picker"], [class*="credential"] {
            display: none !important; opacity: 0 !important; pointer-events: none !important;
            z-index: -9999 !important; visibility: hidden !important;
        }
    `;
    document.head.appendChild(style);
    // Also physically remove any google iframes already injected
    document.querySelectorAll('iframe').forEach(f => {
        const src = f.src || '';
        if (src.includes('google') || src.includes('gsi') || src.includes('smartlock')) f.remove();
    });
}"""


async def get_state(page):
    return await page.evaluate("""() => {
        for (const b of document.querySelectorAll('button')) {
            if ((b.innerText || '').toLowerCase().includes('source')) {
                return b.getAttribute('data-state') || 'no-state-attr';
            }
        }
        return null;
    }""")


async def find_warmed_profile():
    """Return the storage_path of an AVAILABLE perplexity profile, or None."""
    try:
        from src.database import DatabaseManager
        db = DatabaseManager()
        await db.connect()
        row = await db.fetchrow(
            "SELECT storage_path, proxy_string FROM browser_profiles "
            "WHERE engine_type = 'perplexity' AND status = 'AVAILABLE' LIMIT 1"
        )
        await db.close()
        if row:
            return row["storage_path"], row.get("proxy_string")
    except Exception as e:
        print(f"[TEST] Could not query DB for warmed profile: {e}")
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
        profile_path = os.path.join(os.getcwd(), "profiles", "perplexity", "_test_sources")
        os.makedirs(profile_path, exist_ok=True)
        proxy_str = None
        print(f"[TEST] No warmed profile available — using throwaway: {profile_path}")

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

        # Block media to speed up
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

        # Kill Google/overlay BEFORE doing anything else
        await page.evaluate(KILL_OVERLAYS_JS)
        await asyncio.sleep(0.5)

        # Dismiss Perplexity modals
        for t in ["Decline optional", "Got it", "No thanks"]:
            try:
                b = page.locator(f'button:has-text("{t}")').first
                if await b.count() > 0 and await b.is_visible():
                    await b.click()
                    await asyncio.sleep(0.4)
            except Exception:
                pass

        print("[TEST] Injecting prompt atomically (insert_text)...")
        editor = page.locator(INPUT_SEL).first
        await editor.focus()
        # Atomic — Google popup cannot interrupt this the way char-by-char typing can
        await page.keyboard.insert_text("best local SEO rank tracking tools 2025")
        await asyncio.sleep(1.0)
        await page.keyboard.press("Enter")

        print("[TEST] Waiting for generation to finish...")
        await asyncio.sleep(6)
        for _ in range(60):
            if await page.locator('button[aria-label*="Stop"]').count() == 0:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)

        # Confirm a response actually rendered
        resp_len = await page.evaluate("""() => {
            const el = document.querySelector('[data-renderer="lm"]');
            return el ? el.innerText.length : -1;
        }""")
        print(f"[TEST] Response body length: {resp_len} chars (-1 = response element not found)")
        if resp_len < 50:
            print("[TEST] WARNING: response is empty/tiny — proxy may be blocked or page not loaded.")

        # Re-kill overlays (they reappear after generation)
        await page.evaluate(KILL_OVERLAYS_JS)
        for t in ["Decline optional", "Got it", "No thanks"]:
            try:
                b = page.locator(f'button:has-text("{t}")').first
                if await b.count() > 0 and await b.is_visible():
                    await b.click()
                    await asyncio.sleep(0.4)
            except Exception:
                pass

        state = await get_state(page)
        print(f"\n[TEST] Initial sources button state: {state}")

        # ── DIAGNOSTIC: dump EVERY element whose text mentions 'source' ──
        print("\n[TEST] === ALL elements mentioning 'source' ===")
        dump = await page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('*')) {
                const txt = (el.innerText || '').trim();
                if (txt.toLowerCase().includes('source') && txt.length < 40) {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const attrs = {};
                    for (const a of el.attributes) attrs[a.name] = a.value.slice(0, 60);
                    out.push({
                        tag: el.tagName,
                        text: txt,
                        clickable: (el.tagName === 'BUTTON' || el.onclick !== null || el.getAttribute('role') === 'button'),
                        dataState: el.getAttribute('data-state'),
                        ariaExpanded: el.getAttribute('aria-expanded'),
                        ariaControls: el.getAttribute('aria-controls'),
                        attrs: attrs,
                        pos: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }
                    });
                }
            }
            return out;
        }""")
        for d in dump:
            print(f"  <{d['tag']}> text='{d['text']}' | clickable={d['clickable']} | data-state={d['dataState']} | aria-expanded={d['ariaExpanded']} | aria-controls={d['ariaControls']}")
            print(f"      attrs={d['attrs']}")
            print(f"      pos={d['pos']}")
        print("[TEST] === end dump ===\n")

        # Count links BEFORE clicking — when the panel opens, new <a> source links appear
        async def count_external_links():
            return await page.evaluate("""() => {
                const seen = new Set();
                for (const a of document.querySelectorAll('a[href^="http"]')) {
                    try {
                        const h = new URL(a.href).hostname;
                        if (!h.includes('perplexity')) seen.add(a.href);
                    } catch {}
                }
                return seen.size;
            }""")

        before = await count_external_links()
        print(f"\n[TEST] External links before click: {before}")

        # Click EXACTLY ONCE (it's a toggle — a second click closes it again)
        print("[TEST] Clicking the chip ONCE via JS...")
        await page.evaluate("""() => {
            for (const b of document.querySelectorAll('button')) {
                if ((b.innerText || '').toLowerCase().includes('source')) { b.click(); return true; }
            }
            return false;
        }""")
        await asyncio.sleep(2.5)

        after = await count_external_links()
        print(f"[TEST] External links after click: {after}")

        # Dump what the open panel looks like so we can detect it reliably
        panel_info = await page.evaluate("""() => {
            // Look for a container that now holds many source links
            const containers = [];
            for (const el of document.querySelectorAll('div, aside, section')) {
                const links = el.querySelectorAll('a[href^="http"]');
                if (links.length >= 3) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 100) {
                        containers.push({
                            tag: el.tagName,
                            class: el.className.toString().slice(0, 80),
                            id: el.id,
                            linkCount: links.length,
                            pos: { x: Math.round(r.x), w: Math.round(r.width) }
                        });
                    }
                }
            }
            return containers.slice(0, 5);
        }""")
        print(f"\n[TEST] Containers with >=3 links (the open panel should be here):")
        for c in panel_info:
            print(f"   <{c['tag']}> id='{c['id']}' class='{c['class']}' links={c['linkCount']} pos={c['pos']}")

        success = "JS .click() (single)" if after > before else "NONE"
        print(f"\n[TEST] RESULT: {success}  ({before} -> {after} links)")
        print("[TEST] Look at the browser — is the sources panel open on the right?")
        print("[TEST] Browser stays open 90s. Ctrl+C to exit.\n")
        await asyncio.sleep(90)


async def _scroll_click(chip):
    await chip.scroll_into_view_if_needed()
    await asyncio.sleep(0.3)
    await chip.click(force=True)


if __name__ == "__main__":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n[TEST] Done.")
    finally:
        loop.close()
