"""
Deep investigation: find the REAL article URLs behind Gemini's source chips.

The chip text shows only a domain/name, but the actual article link exists
somewhere in the DOM. This tool probes three theories:
  A. Clicking a chip opens a popover containing the real <a href> article link
  B. The <sources-list> / sources-carousel footer holds full URLs
  C. A "Sources and related content" expander reveals them

Reuses a warmed gemini profile.

Usage:
    python test_gemini_urls.py
"""
import asyncio
import os

os.environ["DEBUG_HEADLESS"] = "0"

from camoufox.async_api import AsyncCamoufox

URL = "https://gemini.google.com/app?hl=en"
INPUT_SEL = 'div.ql-editor[contenteditable="true"], div[contenteditable="true"], p[data-placeholder]'

KILL_OVERLAYS_JS = """() => {
    const style = document.createElement('style');
    style.innerHTML = `
        iframe[src*="smartlock"], iframe[src*="account"], cookie-banner, mat-dialog-container,
        #credential_picker_container {
            display: none !important; opacity: 0 !important; pointer-events: none !important;
            visibility: hidden !important;
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


async def dump_all_real_links(page, label):
    """Dump every http link that is NOT a google-chrome/policy link."""
    links = await page.evaluate("""() => {
        const JUNK = ['policies.google', 'accounts.google', 'support.google',
                      'gemini.google', 'business.gemini', 'gstatic', 'googleapis',
                      'google.com/intl', 'google.com/preferences'];
        const out = [];
        for (const a of document.querySelectorAll('a[href^="http"]')) {
            const h = a.href;
            if (JUNK.some(j => h.includes(j))) continue;
            out.push({ text: (a.innerText||'').trim().slice(0,30), href: h });
        }
        return out;
    }""")
    print(f"\n[{label}] real (non-google) links: {len(links)}")
    for l in links[:25]:
        print(f"    '{l['text']}' -> {l['href']}")
    return links


async def main():
    profile_path, proxy_str = await find_warmed_profile()
    if profile_path and os.path.exists(profile_path):
        print(f"[TEST] Reusing warmed profile: {os.path.basename(profile_path)}")
    else:
        profile_path = os.path.join(os.getcwd(), "profiles", "gemini", "_test_sources")
        os.makedirs(profile_path, exist_ok=True)
        proxy_str = None
        print("[TEST] No warmed profile — throwaway.")

    async with AsyncCamoufox(
        headless=False, persistent_context=True, user_data_dir=profile_path,
        proxy=parse_proxy(proxy_str), geoip=True, locale="en-US",
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        page.on("pageerror", lambda exc: None)

        print("[TEST] Navigating...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector(INPUT_SEL, timeout=30000)
        await page.evaluate(KILL_OVERLAYS_JS)
        await asyncio.sleep(0.5)

        editor = page.locator(INPUT_SEL).first
        await editor.click()
        # This phrasing reliably forces Gemini web grounding (produces source chips)
        await page.keyboard.insert_text(
            "Search the web and list the best local SEO rank tracking tools in 2025. "
            "Cite your sources with links for each tool."
        )
        await asyncio.sleep(1.0)
        send = page.locator('button[aria-label*="Send"]').first
        if await send.count() > 0:
            await send.click()
        else:
            await page.keyboard.press("Enter")

        print("[TEST] Waiting for response...")
        await asyncio.sleep(6)
        for _ in range(70):
            if await page.locator('button[aria-label*="Stop"]').count() == 0:
                break
            await asyncio.sleep(1)
        await asyncio.sleep(4)
        await page.evaluate(KILL_OVERLAYS_JS)

        # Confirm the response actually rendered AND has chips before testing click theories
        diag = await page.evaluate("""() => {
            const r = document.querySelector('model-response');
            return {
                respLen: r ? r.innerText.length : -1,
                chips: document.querySelectorAll('.source-inline-chip-container button, source-inline-chip button, button.multiple-button').length,
                sups: document.querySelectorAll('sup[data-turn-source-index]').length
            };
        }""")
        print(f"[TEST] Response length: {diag['respLen']} | source chips: {diag['chips']} | citation sups: {diag['sups']}")
        if diag['chips'] == 0:
            print("[TEST] WARNING: this answer has NO source chips (Gemini didn't ground it).")
            print("[TEST] Nothing to test — re-run, or try a more current-events prompt.")

        await dump_all_real_links(page, "BEFORE any click")

        # ── THEORY A: PointerEvent click (from the Zyte snippet) + dual selector check ──
        # Dispatches pointerdown/pointerup/click at the chip's real coords — CDK overlays
        # listen for pointerdown, which a plain .click() does NOT fire.
        print("\n[TEST] === THEORY A: PointerEvent click + check BOTH selectors ===")
        n = await page.evaluate("""() => document.querySelectorAll('.source-inline-chip-container button, source-inline-chip button, button.multiple-button').length""")
        print(f"[TEST] Found {n} source chips")
        for i in range(min(n, 4)):
            try:
                await page.evaluate(KILL_OVERLAYS_JS)
                # Fire the full pointer sequence at chip[i]
                fired = await page.evaluate("""(idx) => {
                    const chips = document.querySelectorAll('.source-inline-chip-container button, source-inline-chip button, button.multiple-button');
                    const chip = chips[idx];
                    if (!chip) return 'no-chip';
                    chip.scrollIntoView({block:'center', behavior:'instant'});
                    const r = chip.getBoundingClientRect();
                    const cx = r.left + r.width/2, cy = r.top + r.height/2;
                    const opt = {bubbles:true, cancelable:true, clientX:cx, clientY:cy, view:window};
                    chip.dispatchEvent(new PointerEvent('pointerdown', opt));
                    chip.dispatchEvent(new PointerEvent('pointerup', opt));
                    chip.dispatchEvent(new MouseEvent('click', opt));
                    return 'fired';
                }""", i)
                await asyncio.sleep(2.0)
                # Check BOTH the snippet's selector AND ours
                result = await page.evaluate("""() => {
                    const JUNK = ['policies.google','accounts.google','support.google','gemini.google','business.gemini','gstatic','googleapis'];
                    const clean = (arr) => [...new Set(arr)].filter(h => !JUNK.some(j => h.includes(j)));
                    const cardLinks = [];
                    document.querySelectorAll('inline-source-card a[href], source-card a[href]').forEach(a => cardLinks.push(a.href));
                    const overlayLinks = [];
                    document.querySelectorAll('.cdk-overlay-container a[href], gem-popover a[href], [role="tooltip"] a[href]').forEach(a => overlayLinks.push(a.href));
                    return {
                        inlineSourceCard: clean(cardLinks),
                        overlayPopover: clean(overlayLinks)
                    };
                }""")
                print(f"   chip[{i}] ({fired}):")
                print(f"      inline-source-card -> {result['inlineSourceCard']}")
                print(f"      overlay/popover    -> {result['overlayPopover']}")
                await page.evaluate("() => document.body.click()")
                await asyncio.sleep(0.4)
            except Exception as e:
                print(f"   chip[{i}] failed: {e}")

        # ── THEORY B: open the sources-list / carousel footer ──
        print("\n[TEST] === THEORY B: sources-list / carousel footer ===")
        try:
            # The footer often has a "Sources" button or the carousel itself
            footer_btn = page.locator(
                'sources-carousel-inline button, sources-list button, '
                'button[aria-label*="ources"], source-footnote button'
            ).first
            if await footer_btn.count() > 0:
                await footer_btn.click(force=True)
                await asyncio.sleep(2.0)
                await dump_all_real_links(page, "AFTER sources-list click")
            else:
                print("   no sources-list/carousel button found")
        except Exception as e:
            print(f"   failed: {e}")

        # ── THEORY C: inspect the raw component data for hrefs ──
        print("\n[TEST] === THEORY C: scan ALL elements for any http attribute ===")
        attr_links = await page.evaluate("""() => {
            const JUNK = ['policies.google','accounts.google','support.google','gemini.google','business.gemini','gstatic','googleapis'];
            const out = new Set();
            for (const el of document.querySelectorAll('*')) {
                for (const a of el.attributes) {
                    if (a.value && a.value.startsWith('http') && !JUNK.some(j => a.value.includes(j))) {
                        out.add(el.tagName + '[' + a.name + ']=' + a.value.slice(0,90));
                    }
                }
            }
            return [...out];
        }""")
        print(f"   {len(attr_links)} elements with http attributes:")
        for a in attr_links[:30]:
            print(f"     {a}")

        print("\n[TEST] Browser open 150s — manually click a chip and watch the popover.")
        print("[TEST] Ctrl+C to exit.\n")
        await asyncio.sleep(150)


if __name__ == "__main__":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n[TEST] Done.")
    finally:
        loop.close()
