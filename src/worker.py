import os
import asyncio
import time
from urllib.parse import quote_plus
from camoufox.async_api import AsyncCamoufox
from src.engine_profiles import ENGINE_PROFILES

# ─────────────────────────────────────────────────────────────────────────────
# Senior Scraper Notes:
#
# ARCHITECTURE: Two-phase, persistent-profile system.
#   Phase 1 (Factory): Warm a browser profile → Cloudflare issues cf_clearance
#                      → cookie stored on disk with the profile.
#   Phase 2 (Worker):  Load the SAME profile with the SAME proxy IP → cf_clearance
#                      is still valid → no bot challenge → clean page load.
#
# KEY INSIGHT: cf_clearance is IP-bound. The worker MUST use the same proxy as
#              the factory that warmed the profile. This is already enforced by
#              storing proxy_string in the DB alongside the profile.
#
# MODAL STRATEGY: Perplexity's login modal is cosmetic CSS (z-index overlay).
#   The AI generation happens underneath it. We do NOT need to dismiss it to
#   extract the answer — we just need the DOM to be populated, which it is.
#   We make ONE soft attempt to dismiss it, then proceed regardless.
#
# PROMPT INJECTION: We use the Lexical editor (data-lexical-editor="true").
#   We click it with force=True to bypass pointer-events issues caused by any
#   modal overlay, then use keyboard.insert_text (hardware typing).
#
# NETWORK TIMING: Smart Silence Timer — wait for 4s of network inactivity.
#   This is the definitive signal that the AI has finished streaming.
# ─────────────────────────────────────────────────────────────────────────────

# Set via env var: DEBUG_HEADLESS=1 python perplexity_agent.py
import os as _os
DEBUG_HEADLESS = _os.getenv("DEBUG_HEADLESS", "0") != "1"


class ExtractionWorker:
    def __init__(self, profile_path: str, engine: str, proxy_string: str = None):
        self.profile_path = profile_path
        self.engine       = engine.lower()
        self.proxy_string = proxy_string
        self.screenshot_dir = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)

        if self.engine not in ENGINE_PROFILES:
            raise ValueError(f"Unsupported engine: {self.engine}")
        self.config = ENGINE_PROFILES[self.engine]

    def _parse_proxy(self) -> dict | None:
        if not self.proxy_string:
            return None
        parts = self.proxy_string.split(":")
        if len(parts) != 4:
            return None
        return {
            "server":   f"http://{parts[0]}:{parts[1]}",
            "username": parts[2],
            "password": parts[3],
        }

    async def execute_task(self, prompt: str, task_id: str):
        print(f"[WORKER:{self.engine.upper()}] Task {task_id} — profile: {os.path.basename(self.profile_path)}")

        async with AsyncCamoufox(
            headless=DEBUG_HEADLESS,
            persistent_context=True,
            user_data_dir=self.profile_path,
            proxy=self._parse_proxy(),
            geoip=True,
            # NOTE: do NOT set locale= when geoip=True — geoip sets locale
            # automatically from the proxy IP. Explicit locale conflicts.
        ) as browser:
            page = browser.pages[0] if browser.pages else await browser.new_page()
            page.on("pageerror", lambda _: None)

            # ── Route handler: block media + trackers ─────────────────────────
            async def safe_route(route):
                try:
                    if route.request.resource_type == "media":
                        await route.abort(); return
                    if any(t in route.request.url.lower()
                           for t in ["analytics", "telemetry", "sentry", "datadog", "mixpanel"]):
                        await route.abort(); return
                    await route.continue_()
                except Exception:
                    pass

            await page.route("**/*", safe_route)

            # ── Navigation & Prompt Injection ─────────────────────────────────
            # STRATEGY A: Direct URL search if engine supports it
            # STRATEGY B: Homepage navigation + typing
            search_template = self.config.get("search_url_template")

            async def _wait_for_cloudflare_and_react():
                # 1. Cloudflare JS Challenge Handler
                page_title = await page.title()
                if any(ind in page_title.lower() for ind in ["just a moment", "cf-error", "attention required", "403"]):
                    print(f"[WORKER:{self.engine.upper()}] Cloudflare challenge detected — waiting for auto-resolve...")
                    for _ in range(15):
                        await asyncio.sleep(1)
                        page_title = await page.title()
                        if not any(ind in page_title.lower() for ind in ["just a moment", "cf-error", "attention required", "403"]):
                            print(f"[WORKER:{self.engine.upper()}] Cloudflare challenge resolved!")
                            break
                    else:
                        raise Exception("Cloudflare block persistent after 15s — proxy burned.")
                
                # 2. React Hydration Wait
                try:
                    await page.wait_for_selector("#root > *", timeout=15000)
                except Exception:
                    print(f"[WORKER:{self.engine.upper()}] Blank page — reloading ...")
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    try:
                        await page.wait_for_selector("#root > *", timeout=15000)
                    except Exception:
                        raise Exception("Page blank after reload — proxy or network issue.")

            if search_template:
                # ── Strategy A: Direct URL Search ──────────────────────────────
                nav_url = search_template.format(query=quote_plus(prompt))
                print(f"[WORKER:{self.engine.upper()}] Direct URL search: {nav_url[:80]}...")
                await page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)

                await _wait_for_cloudflare_and_react()

                # Soft modal dismissal (mostly for cookie banners, login won't block generation)
                await self._soft_dismiss_modals(page)
                print(f"[WORKER:{self.engine.upper()}] Prompt dispatched via URL — watching network ...")

            else:
                # ── Strategy B: Homepage Navigation & Typing ───────────────────
                nav_url = self.config["url"]
                print(f"[WORKER:{self.engine.upper()}] Navigating to {nav_url} ...")
                await page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)

                await _wait_for_cloudflare_and_react()

                await self._soft_dismiss_modals(page)

                print(f"[WORKER:{self.engine.upper()}] Injecting prompt ...")
                editor = page.locator(self.config["input_selector"]).first
                try:
                    await editor.wait_for(state="attached", timeout=30000)
                except Exception:
                    await self._save_debug_screenshot(page, task_id, "no_editor")
                    raise Exception("Editor not found — likely a hard login wall or proxy block.")

                await editor.click(force=True)
                await asyncio.sleep(0.3)
                await self._soft_dismiss_modals(page)

                await page.keyboard.insert_text(prompt)
                await asyncio.sleep(0.3)
                await page.keyboard.press("Enter")
                print(f"[WORKER:{self.engine.upper()}] Prompt sent — watching network ...")


            # ── STEP 4: Smart Silence Timer ───────────────────────────────────
            # The ONLY reliable signal that the AI is done streaming is
            # 4 consecutive seconds of no network activity on relevant endpoints.
            last_activity = time.time()
            SILENCE_SEC   = 4
            MAX_WAIT_SEC  = 120  # hard cap

            def mark_active(url: str):
                lower = url.lower()
                # Filter out autocomplete, analytics, and static assets
                if any(x in lower for x in ["suggest", "autocomplete", ".css", ".js", ".png", ".woff"]):
                    return
                nonlocal last_activity
                last_activity = time.time()

            page.on("response",  lambda r: mark_active(r.url))
            page.on("websocket", lambda ws: ws.on(
                "framereceived", lambda _: mark_active(ws.url)
            ))

            start = time.time()
            while True:
                await asyncio.sleep(0.5)
                elapsed = time.time() - start
                silent  = time.time() - last_activity
                if silent >= SILENCE_SEC:
                    print(f"[WORKER:{self.engine.upper()}] {SILENCE_SEC}s silence — generation complete ({elapsed:.1f}s total).")
                    break
                if elapsed > MAX_WAIT_SEC:
                    print(f"[WORKER:{self.engine.upper()}] MAX_WAIT reached — extracting anyway.")
                    break

            # ── STEP 5: DOM Extraction ────────────────────────────────────────
            # Extract from the response_selector. The modal (if still visible)
            # does NOT prevent inner_text() from reading underlying DOM nodes.
            response_text = await self._extract_response(page, task_id)
            sources       = await self._extract_sources(page)

            # ── STEP 6: Screenshot ────────────────────────────────────────────
            shot_path = os.path.join(self.screenshot_dir, f"{task_id}.jpg")
            await page.screenshot(path=shot_path, full_page=True, type="jpeg", quality=85)
            print(f"[WORKER:{self.engine.upper()}] Screenshot saved → {shot_path}")

            return {
                "ai_response":      response_text,
                "sources":          sources,
                "screenshot_path":  shot_path,
            }

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _soft_dismiss_modals(self, page):
        """
        Locale-independent modal dismissal.
        Uses structural selectors (IDs, roles) — NOT button text — because
        geoip=True causes the browser to render in the proxy's local language.
        Never raises — the AI generates underneath modals so extraction works either way.
        """
        try:
            # 1. Cookie consent — Perplexity uses id="cookie-consent"
            #    The LAST button is always the "accept" one (Got it / 同意する / etc.)
            #    regardless of language.
            consent = page.locator('#cookie-consent button').last
            if await consent.is_visible():
                await consent.click()
                await asyncio.sleep(0.5)
            else:
                # Fallback: any bottom-right cookie banner accept button
                consent2 = page.locator(
                    '[class*="cookie"] button:last-child, '
                    '[id*="cookie"] button:last-child, '
                    '[class*="consent"] button:last-child'
                ).last
                if await consent2.is_visible():
                    await consent2.click()
                    await asyncio.sleep(0.5)
        except Exception:
            pass

        try:
            # 2. Login modal — target the close (×) button which is always the
            #    FIRST button inside the dialog (top-right corner X).
            close = page.locator('div[role="dialog"] button').first
            if await close.is_visible():
                await close.click()
                await asyncio.sleep(0.3)
        except Exception:
            pass

        # 3. Kill Google One Tap in ALL its rendering forms:
        #    - iframe (old): src=accounts.google.com
        #    - div (new GSI): id="credential_picker_container", class="gsi*", etc.
        #    - The floating card seen in the Arabic screenshot
        try:
            await page.evaluate("""() => {
                const selectors = [
                    'iframe[src*="accounts.google"]',
                    'iframe[src*="smartlock"]',
                    '#credential_picker_container',
                    '#google-one-tap-container',
                    'div[id*="gsi"]',
                    'div[class*="gsi"]',
                    'div[id*="one-tap"]',
                    'div[id*="onetap"]',
                    '[data-google-oauth-client]',
                    '[aria-label*="Google"]',  // catches the Arabic Google One Tap card
                ];
                selectors.forEach(sel => {
                    try { document.querySelectorAll(sel).forEach(el => el.remove()); }
                    catch(e) {}
                });
            }""")
        except Exception:
            pass


    async def _extract_response(self, page, task_id: str) -> str:
        """
        Extract the final AI response text from the DOM.
        Tries multiple selectors in priority order.
        """
        selectors = self.config.get("response_selector", "div.prose").split(", ")
        for sel in selectors:
            try:
                loc = page.locator(sel.strip()).last
                if await loc.is_visible():
                    text = (await loc.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue

        # Nothing found — save a debug screenshot and raise
        await self._save_debug_screenshot(page, task_id, "no_response")
        raise Exception(f"Could not extract response. Debug screenshot saved.")

    async def _extract_sources(self, page) -> list:
        """
        Extract cited source URLs from the page.
        Filters out internal Perplexity links.
        """
        sources = []
        try:
            links = await page.locator(
                'a[href^="http"]:not([href*="perplexity.ai"])'
            ).all()
            seen = set()
            for link in links:
                href = await link.get_attribute("href")
                if href and href not in seen:
                    seen.add(href)
                    sources.append(href)
        except Exception:
            pass
        return sources

    async def _save_debug_screenshot(self, page, task_id: str, label: str):
        try:
            path = os.path.join(self.screenshot_dir, f"DEBUG_{label}_{task_id}.jpg")
            await page.screenshot(path=path, full_page=True, type="jpeg", quality=85)
            print(f"[WORKER] Debug screenshot → {path}")
        except Exception:
            pass