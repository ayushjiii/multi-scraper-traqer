# src/worker.py
import os
import time
import asyncio
import json
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

            # State Tracker for Playwright Hooks
            network_state = {
                "stream_started": False,
                "stream_finished": False,
                "last_packet_time": time.time(),
                "sources": []
            }

            # --- THE RECURSIVE SOURCE HUNTER ---
            def extract_urls(obj):
                """Recursively hunts through massive JSON payloads for hidden citations."""
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
                
                # Check for End of Stream
                if any(ind in url for ind in self.config["stream_indicators"]):
                    network_state["stream_started"] = True
                    network_state["last_packet_time"] = time.time()
                    if response.status == 200:
                        network_state["stream_finished"] = True

                # Siphon source URLs out of API/GraphQL payloads
                try:
                    if any(x in url for x in ["graphql", "api", "rest"]):
                        data = await response.json()
                        extracted = extract_urls(data)
                        if extracted:
                            # Filter out internal tracking garbage
                            clean_urls = [u for u in extracted if "perplexity.ai" not in u and "sentry.io" not in u]
                            if clean_urls:
                                network_state["sources"].extend(clean_urls)
                                print(f"[WORKER:{self.engine.upper()}] Siphoned {len(clean_urls)} hidden URLs from network tree.")
                except Exception:
                    pass

            def handle_websocket(ws):
                url = ws.url.lower()
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

            # Register Hooks
            page.on("response", lambda r: asyncio.ensure_future(handle_response(r)))
            page.on("websocket", handle_websocket)

            # Block Trackers
            async def route_filter(route):
                try:
                    if route.request.resource_type == "media":
                        await route.abort()
                        return
                    if any(t in route.request.url.lower() for t in ['analytics', 'telemetry', 'sentry', 'mixpanel']):
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    pass

            await page.route("**/*", route_filter)

            # --- NAVIGATE & CHECK ---
            print(f"[WORKER:{self.engine.upper()}] Navigating to {self.config['url']}...")
            await page.goto(self.config['url'], wait_until="domcontentloaded", timeout=60000)
            
            await asyncio.sleep(2)
            page_text = await page.content()
            for indicator in self.config['login_wall_indicators']:
                if indicator in page_text:
                    raise Exception("Proxy IP burned. Cloudflare / Verification wall detected on load.")
            
            # ACTIVE DOM NUKE
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
                await page.keyboard.type(prompt, delay=15)
                
            await asyncio.sleep(1.0)
            
            # Dispatch
            try:
                send_btn = page.locator(self.config["send_button_selector"]).first
                if await send_btn.count() > 0 and await send_btn.is_visible():
                    await send_btn.click()
            except Exception:
                pass

            await page.keyboard.press("Enter")
            print(f"[WORKER:{self.engine.upper()}] Prompt dispatched. Awaiting Protocol Stream...")

            # --- SMART NETWORK TRACKING ---
            stream_detected = False
            for _ in range(30):
                if network_state["stream_started"]:
                    stream_detected = True
                    break
                await asyncio.sleep(1)

            if not stream_detected:
                debug_path = os.path.join(self.screenshot_dir, f"DEBUG_FAIL_{task_id}.jpg")
                await page.screenshot(path=debug_path, full_page=True, type="jpeg")
                raise Exception("Protocol Monitor failed to catch network stream allocation.")

            print(f"[WORKER:{self.engine.upper()}] Stream active. Tracking transaction flow...")

            for tick in range(90):
                await asyncio.sleep(1.0)
                silence_duration = time.time() - network_state["last_packet_time"]
                
                # If backend closed stream OR hasn't sent data in 4 seconds
                if network_state["stream_finished"] or silence_duration >= 4.0:
                    print(f"[WORKER:{self.engine.upper()}] Network pipeline normalized (Silence: {silence_duration:.1f}s).")
                    break

            await asyncio.sleep(2.0)

            # --- EXTRACTION & SCREENSHOT ---
            final_response = await page.evaluate(f'''(sel) => {{
                const els = document.querySelectorAll(sel);
                return els.length > 0 ? els[els.length-1].innerText : "";
            }}''', self.config["response_selector"])

            if not final_response or len(final_response.strip()) < 5:
                raise Exception("Network stream stabilized but text extraction targets remained unpopulated.")

            # Brute Force Formatting
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

            # Pre-Screenshot Purge
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
            print(f"[WORKER:{self.engine.upper()}] Extraction completed successfully: {shot_path}")
            
            # Filter unique URLs before saving
            unique_sources = list(set(network_state["sources"]))

            return {
                "ai_response":     final_response,
                "sources":         unique_sources,
                "screenshot_path": shot_path
            }