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

# Set to True temporarily to watch the browser window and debug visually.
DEBUG_HEADLESS = False


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
            locale="en-US",
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

            # ── STEP 1: Navigate ──────────────────────────────────────────────
            # Use the homepage — the profile has cf_clearance cookies so
            # Cloudflare will pass us through cleanly.
            nav_url = self.config["url"]
            print(f"[WORKER:{self.engine.upper()}] Navigating to {nav_url} ...")
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for React SPA to hydrate (#root is empty on initial HTML)
            try:
                await page.wait_for_selector("#root > *", timeout=30000)
            except Exception:
                # If still blank, try one reload
                print(f"[WORKER:{self.engine.upper()}] Blank page — reloading ...")
                await page.reload(wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_selector("#root > *", timeout=20000)
                except Exception:
                    raise Exception("Page blank after reload — proxy or network issue.")

            # ── STEP 2: Soft modal dismissal (ONE attempt, non-blocking) ──────
            # The login modal is cosmetic. We try to dismiss it but we don't
            # wait long and we don't fail if it doesn't disappear.
            # Priority order: cookie consent first, then login modal X button.
            await self._soft_dismiss_modals(page)

            # ── STEP 3: Inject prompt via Lexical editor ──────────────────────
            # Use force=True on click so pointer-events overlays don't block us.
            print(f"[WORKER:{self.engine.upper()}] Injecting prompt ...")
            editor = page.locator(self.config["input_selector"]).first
            try:
                await editor.wait_for(state="attached", timeout=30000)
            except Exception:
                await self._save_debug_screenshot(page, task_id, "no_editor")
                raise Exception("Editor not found — likely a hard login wall or proxy block.")

            await editor.click(force=True)
            await asyncio.sleep(0.3)

            # After clicking the editor, modal may re-appear. Soft-dismiss again.
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
        One non-blocking attempt to dismiss cookie consent and login modals.
        Never raises — if modal can't be dismissed, we proceed anyway.
        The AI generates underneath the modal, so extraction still works.
        """
        try:
            # 1. Cookie consent (appears first, must be clicked before anything)
            consent_btn = page.locator(
                'button:has-text("Got it"), button:has-text("Allow all"), '
                'button:has-text("Only necessary")'
            ).first
            if await consent_btn.is_visible():
                await consent_btn.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass

        try:
            # 2. Login modal X close button
            #    The modal close button has no aria-label on Perplexity,
            #    so we target the last button inside the dialog role element.
            close = page.locator('div[role="dialog"] button').last
            if await close.is_visible():
                await close.click()
                await asyncio.sleep(0.3)
        except Exception:
            pass

        # 3. Remove Google One Tap / credential iframes from DOM
        try:
            await page.evaluate("""() => {
                document.querySelectorAll(
                    'iframe[src*="accounts.google"], iframe[src*="smartlock"], #credential_picker_container'
                ).forEach(el => el.remove());
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