# src/worker.py
import os
import asyncio
import json
from camoufox.async_api import AsyncCamoufox
from src.engine_profiles import ENGINE_PROFILES

class ExtractionWorker:
    def __init__(self, profile_path: str, engine: str, proxy_string: str = None):
        self.profile_path = profile_path
        self.engine = engine.lower()
        self.proxy_string = proxy_string
        
        # Isolate screenshots by engine
        self.screenshot_dir = os.path.join(os.getcwd(), f"screenshots/{self.engine}")
        os.makedirs(self.screenshot_dir, exist_ok=True)
        self.config = ENGINE_PROFILES[self.engine]

    def _parse_proxy(self, proxy_str: str) -> dict | None:
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
        camoufox_proxy = self._parse_proxy(self.proxy_string) if self.proxy_string else None

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

            trap_state = {"sources": []}

            async def on_response(response):
                url = response.url.lower()
                try:
                    if "graphql" in url or "api" in url or "rest" in url:
                        data = await response.json()
                        if isinstance(data, dict):
                            for key in ("web_results", "sources", "citations", "results", "web_search_results"):
                                if key in data and isinstance(data[key], list):
                                    trap_state["sources"].extend(data[key])
                                    break
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

            async def safe_route_handler(route):
                try:
                    if route.request.resource_type == "media":
                        await route.abort()
                        return
                    if any(t in route.request.url.lower() for t in ['analytics', 'telemetry', 'sentry', 'datadog', 'mixpanel']):
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    pass

            await page.route("**/*", safe_route_handler)

            print(f"[WORKER:{self.engine.upper()}] Navigating to {self.config['url']}...")
            await page.goto(self.config['url'], wait_until="domcontentloaded", timeout=60000)
            
            await asyncio.sleep(2)
            page_text = await page.content()
            for indicator in self.config['login_wall_indicators']:
                if indicator in page_text:
                    raise Exception("Proxy IP burned. Hard Login Wall detected on load.")
                
            await page.evaluate('''() => {
                const style = document.createElement('style');
                style.innerHTML = `
                    iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"],
                    div[role="dialog"], .cdk-overlay-container, [class*="backdrop"],
                    #credential_picker_container { display: none !important; opacity: 0 !important; visibility: hidden !important; }
                `;
                document.head.appendChild(style);
            }''')

            input_element = page.locator(self.config["input_selector"]).first
            await input_element.wait_for(state="attached", timeout=45000)
            
            # Injection Router
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
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.0)
            
            try:
                await page.evaluate(f'''(btnSelector) => {{
                    const btn = document.querySelector(btnSelector);
                    if (btn && !btn.disabled) btn.click();
                }}''', self.config["send_button_selector"])
            except Exception:
                pass

            print(f"[WORKER:{self.engine.upper()}] Prompt dispatched. Scanning DOM...")

            started = False
            for _ in range(45): 
                text = await page.evaluate(f'''(sel) => {{
                    const els = document.querySelectorAll(sel);
                    return els.length > 0 ? els[els.length-1].innerText.trim() : "";
                }}''', self.config["response_selector"])
                
                if len(text) > 5:
                    started = True
                    break
                await asyncio.sleep(1)

            if not started:
                debug_path = os.path.join(self.screenshot_dir, f"DEBUG_FAIL_{task_id}.jpg")
                await page.screenshot(path=debug_path, full_page=True, type="jpeg")
                raise Exception("Response element never populated. Captured debug screenshot.")

            print(f"[WORKER:{self.engine.upper()}] Active streaming. Waiting for stabilization...")

            previous_length = 0
            stable_ticks = 0
            final_response = ""
            
            for tick in range(self.config['response_timeout_sec']):
                await asyncio.sleep(1.0)
                try:
                    current_text = await page.evaluate(f'''(sel) => {{
                        const els = document.querySelectorAll(sel);
                        return els.length > 0 ? els[els.length-1].innerText : "";
                    }}''', self.config["response_selector"])

                    current_length = len(current_text)

                    if current_length > 5:
                        if current_length == previous_length:
                            stable_ticks += 1
                            if stable_ticks >= self.config['stability_threshold']:
                                print(f"[WORKER:{self.engine.upper()}] Stabilized at {current_length} characters.")
                                final_response = current_text
                                break
                        else:
                            stable_ticks = 0
                            previous_length = current_length
                except Exception:
                    break

            if not final_response:
                raise Exception("Extraction loop completed but response buffer was empty.")

            await page.evaluate("""() => {
                document.documentElement.classList.remove('dark');
                document.documentElement.classList.add('light');
                document.documentElement.style.colorScheme = 'light';
            }""")

            await asyncio.sleep(1.5)

            shot_path = os.path.join(self.screenshot_dir, f"{task_id}.jpg")
            await page.screenshot(path=shot_path, full_page=True, type="jpeg", quality=90)
            
            return {
                "ai_response":     final_response,
                "sources":         trap_state["sources"],
                "screenshot_path": shot_path
            }