"""
Headless=False debug tool for inspecting the Perplexity sources chip DOM.
Opens the browser visibly, sends a prompt, then pauses so you can inspect
the element. Also prints every candidate element it finds near the chip.

Usage:
    python test_perplexity_sources.py
"""
import asyncio
import os
import sys

os.environ["DEBUG_HEADLESS"] = "0"

from camoufox.async_api import AsyncCamoufox


async def main():
    profile_path = os.path.join(os.getcwd(), "profiles", "perplexity", "_test_sources")
    os.makedirs(profile_path, exist_ok=True)

    async with AsyncCamoufox(
        headless=False,
        persistent_context=True,
        user_data_dir=profile_path,
        geoip=True,
        locale="en-US",
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        page.on("pageerror", lambda exc: None)

        print("[TEST] Navigating to Perplexity...")
        await page.goto("https://www.perplexity.ai", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector('textarea, [contenteditable="true"]', timeout=30000)

        print("[TEST] Typing prompt...")
        editor = page.locator('textarea, [contenteditable="true"]').first
        await editor.click()
        await page.keyboard.type("What are the best local SEO rank tracking tools in 2025?", delay=40)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")

        print("[TEST] Waiting for response to stabilise...")
        await asyncio.sleep(5)

        # Wait for stop button to disappear (generation done)
        for _ in range(60):
            stop = await page.locator('button[aria-label*="Stop"]').count()
            if stop == 0:
                break
            await asyncio.sleep(1)

        await asyncio.sleep(2)
        print("[TEST] Response stable. Waiting extra 5s for DOM to fully settle...")
        await asyncio.sleep(5)

        # 1. Dump full page text so we can confirm the response is actually there
        body_text = await page.evaluate("() => document.body.innerText.slice(0, 500)")
        print(f"\n[TEST] Page body preview:\n{body_text}\n")

        # 2. Dump ALL visible elements — no filters, so we see everything
        print("[TEST] Dumping ALL visible interactive elements...\n")
        info = await page.evaluate("""() => {
            const results = [];
            const all = document.querySelectorAll('button, div[role="button"], a[role="button"], [onclick], [tabindex]');
            for (const el of all) {
                const text = (el.innerText || el.textContent || '').trim().slice(0, 50);
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    results.push({
                        tag: el.tagName,
                        text: text,
                        class: el.className.toString().slice(0, 120),
                        id: el.id || '',
                        role: el.getAttribute('role') || '',
                        dataTestId: el.getAttribute('data-testid') || '',
                        tabindex: el.getAttribute('tabindex') || '',
                        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) }
                    });
                }
            }
            return results;
        }""")
        print(f"Found {len(info)} interactive elements:\n")
        for el in info:
            print(f"  <{el['tag']}> | text='{el['text']}' | testid='{el['dataTestId']}' | class='{el['class']}' | pos=({el['rect']['x']},{el['rect']['y']}) size={el['rect']['w']}x{el['rect']['h']}")

        # 3. Check specifically for anything with 'source' anywhere in its attributes or text
        print("\n[TEST] Scanning ALL elements for 'source' in any attribute or text...\n")
        source_info = await page.evaluate("""() => {
            const results = [];
            for (const el of document.querySelectorAll('*')) {
                const attrs = Array.from(el.attributes || []).map(a => a.name + '=' + a.value).join(' ');
                const text = (el.innerText || el.textContent || '').trim().slice(0, 60);
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 &&
                    (attrs.toLowerCase().includes('source') || text.toLowerCase().includes('source'))) {
                    results.push({
                        tag: el.tagName,
                        text: text,
                        attrs: attrs.slice(0, 150),
                        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) }
                    });
                }
            }
            return results;
        }""")
        print(f"Found {len(source_info)} elements mentioning 'source':\n")
        for el in source_info:
            print(f"  <{el['tag']}> text='{el['text']}' | attrs='{el['attrs']}' | pos=({el['rect']['x']},{el['rect']['y']})")

        print("\n[TEST] Browser staying open — inspect the sources chip manually.")
        print("[TEST] Press Ctrl+C to exit.\n")
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
