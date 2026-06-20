import os
import asyncio
from camoufox.async_api import AsyncCamoufox

class ExtractionWorker:
    def __init__(self, profile_path: str, proxy_string: str = None):
        self.profile_path = profile_path
        self.proxy_string = proxy_string
        self.screenshot_dir = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)

        self.engine_configs = {
            "chatgpt": {
                "input": '#prompt-textarea, [contenteditable="true"]',
                "assistant": "[data-message-author-role='assistant'], article",
                "send_btn": 'button[data-testid="send-button"]',
                "stop_btn": 'button[data-testid="stop-button"], button[aria-label*="Stop"]'
            },
            "perplexity": {
                "input": 'textarea, [contenteditable="true"]',
                "assistant": "div.prose, div.default.font-sans, div.break-words",
                "send_btn": 'button[aria-label*="Submit"], button:has(svg)',
                "stop_btn": 'button[aria-label*="Stop"]'
            },
            "gemini": {
                "input": 'rich-textarea, div[contenteditable="true"], textarea:visible',
                "assistant": "message-content, .message-content, div.message-text",
                "send_btn": 'button[aria-label*="Send"]',
                "stop_btn": 'button[aria-label*="Stop"]'
            }
        }

    def _get_engine_type(self, url: str) -> str:
        if "chatgpt.com" in url: return "chatgpt"
        if "perplexity.ai" in url: return "perplexity"
        if "gemini.google.com" in url: return "gemini"
        return "chatgpt"

    async def execute_task(self, engine_url: str, prompt: str, task_id: str):
        """Execute a scraping task for the given engine URL.

        Implements the Hybrid Extraction Strategy for the Perplexity engine:
        * Network interception via response/websocket events.
        * Smart silence timer (4 s of inactivity) to detect generation end.
        * DOM extraction after network signals DONE.
        * Safe removal of Google/consent pop‑ups without breaking Tailwind UI.
        * Graceful soft‑ban detection.
        """
        import time
        engine_type = self._get_engine_type(engine_url)
        config = self.engine_configs[engine_type]

        print(f"[WORKER] Launching [{engine_type.upper()}] with profile: {os.path.basename(self.profile_path)}")

        # ---------------------------------------------------------------------
        # Browser launch
        # ---------------------------------------------------------------------
        camoufox_proxy = None
        if self.proxy_string:
            parts = self.proxy_string.split(":")
            if len(parts) == 4:
                camoufox_proxy = {
                    "server": f"http://{parts[0]}:{parts[1]}",
                    "username": parts[2],
                    "password": parts[3]
                }

        async with AsyncCamoufox(
            headless=True,
            persistent_context=True,
            user_data_dir=self.profile_path,
            proxy=camoufox_proxy,
            geoip=True,
            locale="en-US"
        ) as browser:
            page = browser.pages[0] if browser.pages else await browser.new_page()
            page.on("pageerror", lambda exc: None)

            # -----------------------------------------------------------------
            # Safe route handler – block trackers & media, but allow UI assets.
            # -----------------------------------------------------------------
            async def safe_route_handler(route):
                try:
                    if route.request.resource_type == "media":
                        await route.abort()
                        return
                    url = route.request.url.lower()
                    blocked_trackers = ["analytics", "telemetry", "sentry", "datadog", "mixpanel"]
                    if any(tracker in url for tracker in blocked_trackers):
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    pass

            await page.route("**/*", safe_route_handler)

            # -----------------------------------------------------------------
            # Navigation
            # -----------------------------------------------------------------
            print(f"[WORKER] Navigating to {engine_url}...")
            await page.goto(engine_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)

            # -----------------------------------------------------------------
            # Safe Popup Assassin – remove Google sign‑in iframes & click consent.
            # -----------------------------------------------------------------
            print("[WORKER] Installing safe popup assassin...")
            await page.evaluate('''() => {
                const removeBadIframes = () => {
                    document.querySelectorAll('iframe[src*="smartlock"], iframe[src*="accounts"], iframe[src*="google"], #credential_picker_container')
                        .forEach(el => el.remove());
                };
                const clickConsent = () => {
                    const buttons = Array.from(document.querySelectorAll('button, input[type="button"]'));
                    buttons.forEach(btn => {
                        const txt = (btn.textContent || "").trim().toLowerCase();
                        if (txt.includes('got it') || txt.includes('accept')) {
                            btn.click();
                        }
                    });
                };
                // Initial clean‑up
                removeBadIframes();
                clickConsent();
                // Keep cleaning every 500 ms
                setInterval(() => { removeBadIframes(); clickConsent(); }, 500);
            }''')

            # -----------------------------------------------------------------
            # Prompt injection – type into the input and press Enter.
            # -----------------------------------------------------------------
            print("[WORKER] Injecting prompt...")
            input_element = page.locator(config["input"]).first
            try:
                await input_element.wait_for(state="attached", timeout=45000)
            except asyncio.TimeoutError:
                raise Exception("Proxy IP burned. Verification wall or soft-ban detected.")
            await input_element.click()
            await asyncio.sleep(0.5)
            await input_element.fill(prompt)
            await asyncio.sleep(0.5)
            await input_element.press("Enter")
            print("[WORKER] Prompt dispatched – monitoring network activity.")

            # -----------------------------------------------------------------
            # Network listeners for the Hybrid Extraction Strategy
            # -----------------------------------------------------------------
            last_activity = time.time()
            silence_threshold = 4  # seconds of inactivity

            def mark_activity(event_url: str):
                # Ignore autocomplete/suggest traffic to avoid false positives
                lower = event_url.lower()
                if "suggest" in lower or "autocomplete" in lower:
                    return
                nonlocal last_activity
                last_activity = time.time()

            # Response events
            page.on("response", lambda response: mark_activity(response.url))
            # WebSocket events – Playwright provides "websocket" events on page
            async def ws_message(ws, message):
                mark_activity(ws.url)
            page.on("websocket", lambda ws: ws.on("framereceived", lambda frame: mark_activity(ws.url)))

            # -----------------------------------------------------------------
            # Wait for smart silence timer – 4 s of no relevant network activity.
            # -----------------------------------------------------------------
            while True:
                await asyncio.sleep(0.5)
                if time.time() - last_activity >= silence_threshold:
                    break

            print("[WORKER] Network silence detected – extracting final DOM output.")

            # -----------------------------------------------------------------
            # DOM extraction – ensure the assistant message is visible.
            # -----------------------------------------------------------------
            ai_message = page.locator(config["assistant"]).last
            try:
                await ai_message.wait_for(state="visible", timeout=25000)
            except Exception:
                debug_path = os.path.join(self.screenshot_dir, f"DEBUG_{task_id}.jpg")
                await page.screenshot(path=debug_path, full_page=True)
                raise Exception(f"Generation element failed to anchor. Saved debug image to {debug_path}")

            # -----------------------------------------------------------------
            # Smooth scroll to force lazy‑loaded citations.
            # -----------------------------------------------------------------
            print("[WORKER] Scrolling to bottom to force lazy‑load renders...")
            await page.evaluate('''async () => {
                await new Promise(resolve => {
                    let totalHeight = 0;
                    const distance = 150;
                    const timer = setInterval(() => {
                        const scrollHeight = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if (totalHeight >= scrollHeight - window.innerHeight) {
                            clearInterval(timer);
                            resolve();
                        }
                    }, 50);
                });
                window.scrollTo(0, 0);
            }''')
            await asyncio.sleep(1.5)

            # -----------------------------------------------------------------
            # Capture final response and screenshot.
            # -----------------------------------------------------------------
            final_response = await ai_message.inner_text()
            if "Sign up and repeat" in final_response or "unlock the full potential" in final_response:
                raise Exception("Hard Login Wall validation failed.")

            # Force Light Mode (optional aesthetic fix)
            await page.evaluate('''() => {
                document.documentElement.classList.remove('dark');
                document.documentElement.classList.add('light');
                document.body.style.setProperty('background-color', '#ffffff', 'important');
            }''')

            # Clean headers/footers – safe Tailwind‑aware selector list
            await page.evaluate('''() => {
                const selectors = ['header', 'nav', 'footer', '[class*="sticky"]', '[class*="fixed"]'];
                selectors.forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
            }''')

            shot_path = os.path.join(self.screenshot_dir, f"{task_id}.jpg")
            await page.screenshot(path=shot_path, full_page=True, type="jpeg", quality=90)
            print(f"[WORKER] Verification screenshot saved: {shot_path}")

            return {
                "ai_response": final_response,
                "sources": [],
                "screenshot_path": shot_path
            }