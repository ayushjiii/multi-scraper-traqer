# src/worker.py
import os
import time
import asyncio
from camoufox.async_api import AsyncCamoufox
from src.engine_profiles import ENGINE_PROFILES

class ExtractionWorker:
    def __init__(self, profile_path: str, engine: str, proxy_string: str = None):
        self.profile_path = profile_path
        self.engine = engine.lower()
        self.proxy_string = proxy_string
        
        self.screenshot_dir = os.path.join(os.getcwd(), f"screenshots/{self.engine}")
        os.makedirs(self.screenshot_dir, exist_ok=True)
        self.config = ENGINE_PROFILES[self.engine]

    def _parse_proxy(self, proxy_str: str) -> dict | None:
        if not proxy_str:
            return None
        parts = proxy_str.split(":")
        if len(parts) != 4:
            return None
        return {
            "server":   f"http://{parts[0]}:{parts[1]}",
            "username": parts[2],
            "password": parts[3]
        }

    async def execute_task(self, prompt: str, task_id: str):
        print(f"[WORKER:{self.engine.upper()}] Launching Profile: {os.path.basename(self.profile_path)}")
        camoufox_proxy = self._parse_proxy(self.proxy_string)

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

            network_state = {
                "stream_started": False,
                "stream_finished": False,
                "last_packet_time": time.time(),
                "sources": []
            }

            # --- RECURSIVE SOURCE HUNTER ---
            def extract_urls(obj):
                found = []
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        if key.lower() in ["url", "link", "source_url", "domain"] and isinstance(value, str) and value.startswith("http"):
                            found.append(value)
                        else:
                            found.extend(extract_urls(value))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(extract_urls(item))
                return found

            # --- NATIVE PLAYWRIGHT INTERCEPTION ---
            async def handle_response(response):
                url = response.url.lower()
                # FIX: Ignore autocomplete network traps
                if "suggest" in url or "autocomplete" in url:
                    return

                if any(ind in url for ind in self.config["stream_indicators"]):
                    network_state["stream_started"] = True
                    network_state["last_packet_time"] = time.time()
                    if response.status == 200:
                        network_state["stream_finished"] = True

                try:
                    if any(x in url for x in ["graphql", "api", "rest"]):
                        data = await response.json()
                        extracted = extract_urls(data)
                        if extracted:
                            clean_urls = [u for u in extracted if "perplexity.ai" not in u and "sentry.io" not in u]
                            if clean_urls:
                                network_state["sources"].extend(clean_urls)
                                print(f"[WORKER:{self.engine.upper()}] Siphoned {len(clean_urls)} source URLs out of API payload.")
                except Exception:
                    pass

            def handle_websocket(ws):
                url = ws.url.lower()
                # FIX: Ignore autocomplete network traps
                if "suggest" in url or "autocomplete" in url:
                    return

                if any(ind in url for ind in self.config["stream_indicators"]):
                    network_state["stream_started"] = True
                    network_state["last_packet_time"] = time.time()
                    ws.on("framereceived", lambda frame: update_socket_time(frame))
                    ws.on("close", lambda: mark_socket_closed())

            def update_socket_time(frame):
                network_state["last_packet_time"] = time.time()
                network_state["stream_started"] = True
                if "done" in frame.text.lower() or "[done]" in frame.text:
                    network_state["stream_finished"] = True

            def mark_socket_closed():
                network_state["stream_finished"] = True

            page.on("response", lambda r: asyncio.ensure_future(handle_response(r)))
            page.on("websocket", handle_websocket)

            async def route_filter(route):
                try:
                    if route.request.resource_type == "media":
                        await route.abort()
                        return
                    if any(t in route.request.url.lower() for t in ['analytics', 'telemetry', 'sentry', 'mixpanel']):
                        await route.abort()
                        return
                    await route.continue_()
                except Exception: pass

            await page.route("**/*", route_filter)

            # --- NAVIGATE & ACTIVE OVERLAY SHIELD ---
            print(f"[WORKER:{self.engine.upper()}] Navigating to {self.config['url']}...")
            await page.goto(self.config['url'], wait_until="domcontentloaded", timeout=60000)
            
            await asyncio.sleep(2)
            page_text = await page.content()
            for indicator in self.config['login_wall_indicators']:
                if indicator in page_text:
                    raise Exception("Proxy IP burned. Cloudflare / Verification wall detected on load.")
            
            await page.evaluate('''() => {
                const style = document.createElement('style');
                style.innerHTML = `
                    iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"], 
                    div[role="dialog"], .cdk-overlay-container, [class*="backdrop"], 
                    #credential_picker_container, [class*="signup"], [class*="login"] {
                        display: none !important; opacity: 0 !important; pointer-events: none !important;
                        z-index: -9999 !important; visibility: hidden !important;
                    }
                `;
                document.head.appendChild(style);

                setInterval(() => {
                    document.querySelectorAll(`
                        iframe[src*="smartlock"], div[role="dialog"], 
                        .cdk-overlay-container, #credential_picker_container
                    `).forEach(el => el.remove());
                }, 500);
            }''')

            # --- INPUT INJECTION ---
            input_element = page.locator(self.config["input_selector"]).first
            await input_element.wait_for(state="attached", timeout=45000)
            await input_element.click()
            await asyncio.sleep(0.5)

            if self.config["injection_method"] == "fill":
                await input_element.fill(prompt)
            else:
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                # FIX: Slightly slower typing to ensure React captures it
                await page.keyboard.type(prompt, delay=25)
                
            # FIX: Give the UI a full second to process the text and enable the submit button
            await asyncio.sleep(1.0)
            
            # FIX: Press Escape to close any autocomplete dropdowns that might block the Enter key
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # FIX: Bruteforce JS Clicker. Finds the specific submit button inside the text area's parent container
            try:
                await page.evaluate('''() => {
                    const textareas = document.querySelectorAll('textarea, [contenteditable="true"]');
                    if (textareas.length > 0) {
                        // Find the button closest to the text box
                        const btn = textareas[0].closest('.group, div, form').querySelector('button[aria-label*="Submit"], button:has(svg)');
                        if (btn && !btn.disabled) {
                            btn.click();
                        }
                    }
                }''')
            except Exception: pass

            # Backup native Enter press with a hard delay
            await page.keyboard.press("Enter", delay=100)
            
            print(f"[WORKER:{self.engine.upper()}] Prompt dispatched. Awaiting Stream...")

            # --- STREAM TIMING LOOP ---
            stream_detected = False
            for _ in range(15):
                if network_state["stream_started"]:
                    stream_detected = True
                    break
                await asyncio.sleep(1)

            if stream_detected:
                print(f"[WORKER:{self.engine.upper()}] Stream verified. Tracking network data flow...")
                for tick in range(90):
                    await asyncio.sleep(1.0)
                    silence_duration = time.time() - network_state["last_packet_time"]
                    if network_state["stream_finished"] or silence_duration >= 4.0:
                        print(f"[WORKER:{self.engine.upper()}] Stream completed via network diagnostics.")
                        break
            else:
                print(f"[WORKER:{self.engine.upper()}] Stream fallback triggered. Monitoring UI mutation ticks...")
                prev_len = 0
                stable_ticks = 0
                for _ in range(60):
                    await asyncio.sleep(1.0)
                    text = await page.evaluate(f'''(sel) => {{
                        const els = document.querySelectorAll(sel);
                        return els.length > 0 ? els[els.length-1].innerText : "";
                    }}''', self.config["response_selector"])
                    curr_len = len(text)
                    if curr_len > 10 and curr_len == prev_len:
                        stable_ticks += 1
                        if stable_ticks >= 4: break
                    else:
                        stable_ticks = 0
                        prev_len = curr_len

            await asyncio.sleep(2.0)

            # --- EXTRACTION & SCREENSHOT ---
            final_response = await page.evaluate(f'''(sel) => {{
                const els = document.querySelectorAll(sel);
                return els.length > 0 ? els[els.length-1].innerText : "";
            }}''', self.config["response_selector"])

            if not final_response or len(final_response.strip()) < 5:
                # FIX: Force a debug screenshot so we can see what the UI actually looks like!
                debug_path = os.path.join(self.screenshot_dir, f"DEBUG_EMPTY_DOM_{task_id}.jpg")
                await page.screenshot(path=debug_path, full_page=True, type="jpeg")
                raise Exception(f"Data channel closed but text targets remained unpopulated. Saved DOM state to {debug_path}")

            # Brute Force Typography Override
            await page.evaluate("""() => {
                document.documentElement.classList.remove('dark');
                document.documentElement.classList.add('light');
                document.body.style.setProperty('background-color', '#ffffff', 'important');
                document.documentElement.style.setProperty('background-color', '#ffffff', 'important');
                document.documentElement.style.setProperty('height', 'auto', 'important');
                document.body.style.setProperty('height', 'auto', 'important');
                document.documentElement.style.setProperty('overflow', 'visible', 'important');
                document.body.style.setProperty('overflow', 'visible', 'important');
            }""")

            # Document Element Purging
            await page.evaluate("""() => {
                const selectorsToNuke = ['header', 'nav', 'footer', '[class*="sticky"]', '[class*="fixed"]', '[role="dialog"]'];
                selectorsToNuke.forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
                document.querySelectorAll('*').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'sticky') el.remove();
                });
            }""")

            await asyncio.sleep(1.5)

            shot_path = os.path.join(self.screenshot_dir, f"{task_id}.jpg")
            await page.screenshot(path=shot_path, full_page=True, type="jpeg", quality=90)
            print(f"[WORKER:{self.engine.upper()}] Execution lifecycle clean. Saved: {shot_path}")
            
            return {
                "ai_response":     final_response,
                "sources":         list(set(network_state["sources"])),
                "screenshot_path": shot_path
            }