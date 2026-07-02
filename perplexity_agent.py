import os
import uuid
import time
import json
import asyncio
import redis.asyncio as redis
from camoufox.async_api import AsyncCamoufox
from src.database import DatabaseManager
from src.config import Config
from src.utils import parse_proxy, safe_task_id

ENGINE = "perplexity"
DEBUG_HEADLESS = os.getenv("DEBUG_HEADLESS", "1") != "0"

# ─────────────────────────────────────────────────────────────────────────────
# 1. Perplexity Worker
# ─────────────────────────────────────────────────────────────────────────────
class PerplexityWorker:
    def __init__(self, profile_path: str, proxy_string: str = None):
        self.profile_path = profile_path
        self.proxy_string = proxy_string
        self.screenshot_dir = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)
        
        # Perplexity-specific selectors
        self.url = "https://www.perplexity.ai"
        self.input_selector = 'textarea, [contenteditable="true"], input[type="text"]'
        self.response_selector = '[data-renderer="lm"]'
        self.send_btn_selector = 'button[type="submit"], button:has(svg)'
        self.stop_btn_selector = 'button[aria-label*="Stop"]'

    def _parse_proxy(self, proxy_str: str) -> dict:
        return parse_proxy(proxy_str)

    async def execute_task(self, prompt: str, task_id: str):
        print(f"[WORKER:PERPLEXITY] Launching profile: {os.path.basename(self.profile_path)}")

        camoufox_proxy = self._parse_proxy(self.proxy_string)
        # Refuse to launch unproxied when a proxy was assigned — proxy=None leaks the real IP.
        if self.proxy_string and camoufox_proxy is None:
            raise Exception(f"Proxy string is malformed; refusing to launch unproxied: {self.proxy_string!r}")

        async with AsyncCamoufox(
            headless=DEBUG_HEADLESS,
            persistent_context=True,
            user_data_dir=self.profile_path,
            proxy=camoufox_proxy,
            geoip=True,
            locale="en-US",
        ) as browser:
            page = browser.pages[0] if browser.pages else await browser.new_page()

            # Suppress unhandled React/Cloudflare errors
            page.on("pageerror", lambda exc: None)

            async def safe_route_handler(route):
                try:
                    if route.request.resource_type == "media":
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    pass

            await page.route("**/*", safe_route_handler)

            print(f"[WORKER:PERPLEXITY] Navigating to {self.url} ...")
            await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)

            print(f"[WORKER:PERPLEXITY] Waiting for page to hydrate...")
            try:
                await page.wait_for_selector('textarea, [contenteditable="true"], input[type="text"]', timeout=30000)
            except Exception:
                raise Exception("Page blank after reload — proxy or network issue.")

            print(f"[WORKER:PERPLEXITY] Executing CSS Layer Hider...")
            await page.evaluate('''() => {
                const style = document.createElement('style');
                style.innerHTML = `
                    iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"],
                    div[role="dialog"], .cdk-overlay-container, [class*="backdrop"], #credential_picker_container,
                    #cookie-consent {
                        display: none !important; opacity: 0 !important; pointer-events: none !important;
                        z-index: -9999 !important; visibility: hidden !important;
                    }
                `;
                document.head.appendChild(style);
            }''')

            # Dismiss Perplexity signup/cookie modals before interacting
            try:
                for dismiss_text in ["Decline optional", "Got it", "No thanks"]:
                    btn = page.locator(f'button:has-text("{dismiss_text}")').first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(0.5)
                        break
            except Exception:
                pass

            print(f"[WORKER:PERPLEXITY] Injecting prompt ...")
            editor = page.locator(self.input_selector).first
            try:
                await editor.wait_for(state="attached", timeout=30000)
            except Exception:
                raise Exception("Editor not found — likely a hard login wall or proxy block.")

            await editor.focus()

            # Hardware typing handles complex Lexical/React states where native fill fails.
            await page.keyboard.insert_text(prompt)
            await asyncio.sleep(1.0)

            # Submit with ENTER — it reliably submits Perplexity's composer. We do
            # NOT click a generic 'button:has(svg)' (the homepage has many svg
            # buttons; .first clicks the wrong one and the query never sends). Only
            # fall back to the SPECIFIC aria-labelled submit button if Enter didn't
            # start generation.
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.5)
            started = await page.evaluate(f"""() => {{
                const els = document.querySelectorAll('{self.response_selector}');
                const stop = document.querySelectorAll('{self.stop_btn_selector}').length;
                return stop > 0 || (els.length && (els[els.length-1].innerText||'').length > 0);
            }}""")
            if not started:
                btn = page.locator('button[aria-label="Submit"], button[aria-label*="Submit" i], button[data-testid*="submit" i]').first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()

            print(f"[WORKER:PERPLEXITY] Prompt successfully dispatched. Monitoring runtime generation cycle...")

            # ── Dynamic Witness Loop ──
            await asyncio.sleep(3.0)

            # Wait for the answer element to EXIST and start filling — not for
            # Playwright's strict "visible" state, which is layout-dependent and was
            # timing out even when [data-renderer="lm"] was present and streaming.
            # Poll via JS (like the Gemini agent) so a slow first paint / covering
            # modal doesn't cause a false "failed to anchor".
            anchored = False
            for _ in range(35):
                await asyncio.sleep(1.0)
                try:
                    txt_len = await page.evaluate(f"""() => {{
                        const els = document.querySelectorAll('{self.response_selector}');
                        if (!els.length) return 0;
                        return (els[els.length - 1].innerText || '').length;
                    }}""")
                    if txt_len and txt_len > 0:
                        anchored = True
                        break
                except Exception:
                    pass
            if not anchored:
                raise Exception("Perplexity answer never rendered (login wall, block, or slow generation).")

            print(f"[WORKER:PERPLEXITY] Stream processing confirmed active. Tracking output buffer limits...")

            previous_length = 0
            stable_ticks = 0

            for _ in range(90):  # 90 seconds maximum execution ceiling
                await asyncio.sleep(1.0)
                try:
                    # Single JS read of both stop-button state and answer length —
                    # pure evaluate() never blocks on element-stability the way
                    # locator.inner_text() does during React re-renders.
                    state = await page.evaluate(f"""() => {{
                        const stop = document.querySelectorAll('{self.stop_btn_selector}').length;
                        const els = document.querySelectorAll('{self.response_selector}');
                        const txt = els.length ? (els[els.length - 1].innerText || '') : '';
                        return {{ generating: stop > 0, len: txt.length }};
                    }}""")
                    is_still_generating = state["generating"]
                    current_length = state["len"]

                    if not is_still_generating and current_length == previous_length and current_length > 0:
                        stable_ticks += 1
                        if stable_ticks >= 3:
                            print(f"[WORKER:PERPLEXITY] Target output buffer stabilized. Preparing snapshot render...")
                            break
                    else:
                        stable_ticks = 0
                        previous_length = current_length
                except Exception:
                    pass

            # Final read (timeout-guarded, then JS fallback like the Gemini agent).
            try:
                response_text = await page.locator(self.response_selector).last.inner_text(timeout=10000)
            except Exception:
                response_text = await page.evaluate(f"""() => {{
                    const els = document.querySelectorAll('{self.response_selector}');
                    return els.length ? els[els.length - 1].innerText : '';
                }}""")
            if not response_text:
                raise Exception("Could not extract response text.")

            if "sign up and repeat your request" in response_text.lower() or "please sign in" in response_text.lower():
                raise Exception("Verification wall hit: Perplexity requires login.")

            # Dismiss the Perplexity signup modal if it appeared — it blocks all clicks
            try:
                dismiss = page.locator('button:has-text("Decline optional"), button[aria-label*="close" i], button[aria-label*="dismiss" i]').first
                if await dismiss.count() > 0 and await dismiss.is_visible():
                    await dismiss.click()
                    await asyncio.sleep(0.8)
                    print(f"[WORKER:PERPLEXITY] Signup modal dismissed.")
            except Exception:
                pass

            # ── Source extraction is decoupled from the chip click ──
            # DATA path: read links straight from the DOM. The "Links" tab content and the
            # source cards are present in the DOM after generation whether or not the panel
            # is visually open, so this does not depend on a click landing. Reliable + fast.
            async def _extract_sources():
                return await page.evaluate("""() => {
                    const BLOCKED = ['perplexity.ai', 'perplexity.com'];
                    const seen = new Set();
                    const results = [];
                    // The open sources panel is a Radix container with id "...content-citations"
                    // (and a fixed right-side sidebar). Prefer links inside it; fall back to any.
                    const allLinks = Array.from(document.querySelectorAll(
                        '[id*="citations"] a[href^="http"], ' +
                        '[id*="sources"] a[href^="http"], ' +
                        '[class*="search-side-content"] a[href^="http"], ' +
                        'a[href^="http"][data-cite], ' +
                        '[class*="source"] a[href^="http"], ' +
                        '[class*="citation"] a[href^="http"], ' +
                        'a[href^="http"]'
                    ));
                    for (const a of allLinks) {
                        let href;
                        try { href = new URL(a.href).href; } catch { continue; }
                        const host = new URL(href).hostname;
                        if (BLOCKED.some(b => host === b || host.endsWith('.' + b))) continue;
                        if (seen.has(href)) continue;
                        seen.add(href);
                        results.push(href);
                    }
                    return results;
                }""")

            # Open the "X sources" panel — REQUIRED: Perplexity does not render source links
            # into the DOM until the panel is opened (confirmed: 0 links before, 10 after).
            # CRITICAL: the chip is a TOGGLE — a second click closes it again. Click EXACTLY
            # ONCE, then verify by checking the external-link count increased. Never re-click.
            async def _count_links():
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

            sources = []
            try:
                before = await _count_links()
                # Single JS click — proven reliable; bypasses pointer-event interception.
                clicked = await page.evaluate("""() => {
                    for (const b of document.querySelectorAll('button')) {
                        if ((b.innerText || '').toLowerCase().includes('source')) { b.click(); return true; }
                    }
                    return false;
                }""")
                if clicked:
                    await asyncio.sleep(2.5)
                    after = await _count_links()
                    print(f"[WORKER:PERPLEXITY] Sources panel: {before} -> {after} links.")
                    # If a second toggle accidentally closed it (count dropped back), click once more.
                    if after <= before:
                        await page.evaluate("""() => {
                            for (const b of document.querySelectorAll('button')) {
                                if ((b.innerText || '').toLowerCase().includes('source')) { b.click(); return; }
                            }
                        }""")
                        await asyncio.sleep(2.0)
                else:
                    print(f"[WORKER:PERPLEXITY] Sources chip not found.")
            except Exception:
                pass

            # Extract now that the panel is open — prefers the citations container's links.
            sources = await _extract_sources()
            print(f"[WORKER:PERPLEXITY] Sources extracted: {len(sources)}")

            unique_sources = list(dict.fromkeys(sources))

            # Standardized layout normalization for clean snaps (Light Mode Enforcement)
            await page.evaluate("""() => {
                document.documentElement.classList.remove('dark', 'theme-dark');
                document.documentElement.classList.add('light', 'theme-light');
                document.documentElement.setAttribute('data-theme', 'light');
                document.documentElement.style.colorScheme = 'light';
            }""")
            
            # CSS Expansion: Force the invisible layout scrollers to render exactly their full content
            await page.evaluate(f"""() => {{
                const messages = Array.from(document.querySelectorAll('{self.response_selector}'));
                if (messages.length === 0) return;
                const lastMessage = messages[messages.length - 1];
                let scroller = lastMessage.parentElement;
                while (scroller && scroller !== document.body && scroller !== document.documentElement) {{
                    if (scroller.scrollHeight > scroller.clientHeight && scroller.clientHeight > 300) {{ break; }}
                    scroller = scroller.parentElement;
                }}
                if (!scroller || scroller === document.documentElement) scroller = document.body;
                const exactHeight = scroller.scrollHeight + 150; 
                let parent = scroller;
                while (parent && parent !== document) {{
                    parent.style.setProperty('height', `${{exactHeight}}px`, 'important');
                    parent.style.setProperty('max-height', 'none', 'important');
                    parent.style.setProperty('overflow', 'visible', 'important');
                    parent = parent.parentElement;
                }}
            }}""")
            
            await asyncio.sleep(2.0)
            
            shot_path = os.path.join(self.screenshot_dir, f"{safe_task_id(task_id)}.jpg")
            await page.screenshot(path=shot_path, full_page=True, type="jpeg", quality=85)
            print(f"[WORKER:PERPLEXITY] Verification screenshot saved: {shot_path}")

            return {
                "ai_response": response_text,
                "sources": unique_sources[:15], # cap at 15 to avoid database bloat
                "screenshot_path": shot_path,
            }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Perplexity Factory
# ─────────────────────────────────────────────────────────────────────────────
class PerplexityFactory:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.engine = "perplexity"
        self.profiles_dir = os.path.join(os.getcwd(), f"profiles/{self.engine}")
        os.makedirs(self.profiles_dir, exist_ok=True)
        self.url = "https://www.perplexity.ai"
        self.tiny_prompt = "hi"

    def _parse_proxy(self, proxy_str: str) -> dict:
        return parse_proxy(proxy_str)

    async def warm_new_profile(self) -> str:
        profile_name = f"{self.engine}_{uuid.uuid4().hex[:8]}"
        profile_path = os.path.join(self.profiles_dir, profile_name)
        proxy_str    = None
        profile_id   = None
        banned_col   = f"{self.engine}_banned"

        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                proxy_row = await conn.fetchrow(f"""
                    SELECT connection_string FROM proxies
                    WHERE status = 'ACTIVE' AND {banned_col} = FALSE
                    AND connection_string NOT IN (
                        SELECT proxy_string FROM browser_profiles
                        WHERE status IN ('AVAILABLE', 'BUSY') AND proxy_string IS NOT NULL
                    )
                    ORDER BY RANDOM() LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """)

                if not proxy_row:
                    raise RuntimeError(f"No active, unassigned proxies available for {self.engine}.")

                proxy_str = proxy_row["connection_string"]
                camoufox_proxy = self._parse_proxy(proxy_str)
                if camoufox_proxy is None:
                    raise RuntimeError(f"Malformed proxy string from DB, cannot warm unproxied: {proxy_str!r}")

                row = await conn.fetchrow("""
                    INSERT INTO browser_profiles
                        (profile_name, engine_type, storage_path, proxy_string, status, trust_score, created_at)
                    VALUES ($1, $2, $3, $4, 'BUSY', 100, CURRENT_TIMESTAMP)
                    RETURNING id;
                """, profile_name, self.engine, profile_path, proxy_str)
                profile_id = row["id"]

        print(f"\n[FACTORY:PERPLEXITY] Warming up new profile: '{profile_name}' on proxy {proxy_str.split(':')[0]}")

        try:
            async with AsyncCamoufox(
                headless=DEBUG_HEADLESS,
                persistent_context=True,
                user_data_dir=profile_path,
                proxy=camoufox_proxy,
                geoip=True,
                locale="en-US",
            ) as browser:
                page = browser.pages[0] if browser.pages else await browser.new_page()

                # SUPPRESS DRIVER CRASHES: Swallow unhandled page errors
                page.on("pageerror", lambda exc: None)

                async def safe_route_handler(route):
                    try:
                        if route.request.resource_type == "media":
                            await route.abort()
                            return
                        await route.continue_()
                    except Exception:
                        pass

                await page.route("**/*", safe_route_handler)

                print(f"[FACTORY:PERPLEXITY] Navigating to {self.url}...")
                await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)

                # ── Cloudflare JS Challenge Handler ───────────────────────────
                page_title = await page.title()
                if any(ind in page_title.lower() for ind in ["just a moment", "cf-error", "attention required", "403"]):
                    print(f"[FACTORY:PERPLEXITY] Cloudflare challenge detected — waiting for auto-resolve...")
                    for _ in range(15):
                        await asyncio.sleep(1)
                        page_title = await page.title()
                        if not any(ind in page_title.lower() for ind in ["just a moment", "cf-error", "attention required", "403"]):
                            print(f"[FACTORY:PERPLEXITY] Cloudflare challenge resolved!")
                            break
                    else:
                        raise RuntimeError(f"Cloudflare block detected. Marking proxy as banned.")

                if "challenges.cloudflare.com" in page.url.lower():
                    raise RuntimeError(f"Cloudflare hard block detected. Marking proxy as banned.")

                print(f"[FACTORY:PERPLEXITY] Waiting for page hydration...")
                try:
                    await page.wait_for_selector('textarea, [contenteditable="true"], input[type="text"]', timeout=30000)
                except Exception:
                    pass

                # ── Executing CSS Element Hider ──────────────────────────────
                print(f"[FACTORY:PERPLEXITY] Executing CSS element hider...")
                await page.evaluate('''() => {
                    const style = document.createElement('style');
                    style.innerHTML = `
                        iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"], 
                        div[role="dialog"], .cdk-overlay-container, [class*="backdrop"], #credential_picker_container,
                        #cookie-consent {
                            display: none !important; opacity: 0 !important; pointer-events: none !important;
                            z-index: -9999 !important; visibility: hidden !important;
                        }
                    `;
                    document.head.appendChild(style);
                }''')
                await asyncio.sleep(1)

                # ── Tiny Prompt Injection ──────────────────────────────
                print(f"[FACTORY:PERPLEXITY] Submitting tiny prompt to initialize UI and cache WASM/WebSockets...")
                input_selector = 'textarea, [contenteditable="true"]'
                input_element = page.locator(input_selector).first
                
                try:
                    await input_element.wait_for(state="attached", timeout=45000)
                    await input_element.focus()
                    
                    await page.keyboard.insert_text(self.tiny_prompt)
                    await asyncio.sleep(1.0)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(1.0)
                    
                    send_btn = page.locator('button[type="submit"], button:has(svg)').first
                    if await send_btn.count() > 0 and await send_btn.is_visible():
                        await send_btn.click()
                        
                    await asyncio.sleep(3)
                    print(f"[FACTORY:PERPLEXITY] Tiny prompt accepted. Session is fully trusted.")
                except Exception as e:
                    print(f"[FACTORY:PERPLEXITY] Tiny prompt injection skipped: {e}")
                
                print(f"[FACTORY:PERPLEXITY] Profile successfully warmed.")

        except Exception as e:
            error_msg = str(e)
            print(f"[FACTORY:PERPLEXITY] Error warming perplexity: {error_msg}")

            try: await self.db.execute("DELETE FROM browser_profiles WHERE id = $1", profile_id)
            except Exception: pass

            proxy_ban_signals = [
                "NS_ERROR_PROXY", "Connection refused", "Failed to connect",
                "Cloudflare block", "ERR_PROXY", "ECONNREFUSED"
            ]
            if any(sig in error_msg for sig in proxy_ban_signals):
                try:
                    await self.db.execute(f"UPDATE proxies SET {banned_col} = TRUE WHERE connection_string = $1", proxy_str)
                    print(f"[FACTORY:PERPLEXITY] Proxy flagged as banned for this engine.")
                except Exception: pass
            raise

        await self.db.execute("""
            UPDATE browser_profiles SET status = 'AVAILABLE', storage_path = $1 WHERE id = $2
        """, profile_path, profile_id)

        print(f"[FACTORY:PERPLEXITY] Profile '{profile_name}' is AVAILABLE.")
        return str(profile_id)

    async def run_daemon(self, target_pool_size: int = 1):
        print(f"\n[FACTORY DAEMON:PERPLEXITY] Started. Target pool: {target_pool_size}.")
        while True:
            # TTL = idle time: expire profiles unused for PROFILE_TTL_MINUTES, not ones
            # that merely existed that long (last_used_at, not created_at).
            await self.db.execute(
                "UPDATE browser_profiles SET status = 'EXPIRED' "
                "WHERE status = 'AVAILABLE' AND engine_type = $1 "
                "AND last_used_at < NOW() - make_interval(mins => $2)",
                self.engine, Config.PROFILE_TTL_MINUTES,
            )

            available = await self.db.fetchval(
                "SELECT COUNT(*) FROM browser_profiles WHERE engine_type = $1 AND status = 'AVAILABLE'", self.engine
            )
            if available < target_pool_size:
                print(f"[FACTORY:PERPLEXITY] Pool low ({available}/{target_pool_size}). Warming...")
                try: await self.warm_new_profile()
                except Exception: pass

            await asyncio.sleep(10)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Perplexity Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
class PerplexityOrchestrator:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.engine = "perplexity"

    async def check_duplicate(self, input_prompt: str) -> bool:
        count = await self.db.fetchval(
            "SELECT COUNT(*) FROM scrape_results WHERE engine_name = $1 AND input_prompt = $2",
            self.engine, input_prompt
        )
        return count > 0

    async def checkout_profile(self):
        query = """
        UPDATE browser_profiles
        SET status = 'BUSY', last_used_at = CURRENT_TIMESTAMP
        WHERE id = (
            SELECT id FROM browser_profiles
            WHERE status = 'AVAILABLE' AND engine_type = $1
            ORDER BY last_used_at ASC NULLS FIRST
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, profile_name, storage_path, proxy_string;
        """
        profile = await self.db.fetchrow(query, self.engine)
        if profile:
            print(f"[ORCHESTRATOR:PERPLEXITY] Checked out profile: {profile['profile_name']}")
            return dict(profile)
        return None

    async def release_profile(self, profile_id: str, success: bool):
        new_status = 'AVAILABLE' if success else 'EXPIRED'
        delta = 1 if success else -10
        await self.db.execute(
            "UPDATE browser_profiles "
            "SET status = $1, trust_score = GREATEST(0, LEAST(100, trust_score + $2)) "
            "WHERE id = $3",
            new_status, delta, profile_id,
        )
        print(f"[ORCHESTRATOR:PERPLEXITY] Released profile {profile_id} -> {new_status}")

    async def _ban_proxy(self, connection_string: str):
        # Column name is chosen from a fixed whitelist (never interpolated from input).
        col = {
            "chatgpt": "chatgpt_banned",
            "perplexity": "perplexity_banned",
            "gemini": "gemini_banned",
        }[self.engine]
        await self.db.execute(
            f"UPDATE proxies SET {col} = TRUE WHERE connection_string = $1",
            connection_string,
        )

    async def process_task(self, task_payload: dict):
        task_id = task_payload.get("task_id", "unknown")
        prompt  = task_payload.get("prompt", "")

        if await self.check_duplicate(prompt):
            print(f"[ORCHESTRATOR:PERPLEXITY] Duplicate prompt skipped.")
            return True

        print(f"\n[ORCHESTRATOR:PERPLEXITY] Processing Task [{task_id}]")

        profile = await self.checkout_profile()
        if not profile:
            print(f"[ORCHESTRATOR:PERPLEXITY] No AVAILABLE profile. Requeued.")
            return False

        success = False
        try:
            worker  = PerplexityWorker(
                profile_path=profile["storage_path"],
                proxy_string=profile.get("proxy_string")
            )
            results = await worker.execute_task(
                prompt=prompt,
                task_id=task_id
            )

            await self.db.execute("""
                INSERT INTO scrape_results
                    (profile_id, task_id, engine_name, input_prompt, ai_response, sources, screenshot_path)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
                profile["id"],
                task_id,
                self.engine,
                prompt,
                results["ai_response"],
                json.dumps(results.get("sources", [])),
                results["screenshot_path"]
            )

            print(f"[ORCHESTRATOR:PERPLEXITY] Task {task_id} saved successfully.")
            success = True

        except Exception as e:
            error_msg = str(e)
            print(f"[ORCHESTRATOR:PERPLEXITY] Task {task_id} failed: {error_msg}")
            
            if any(term in error_msg for term in ["Proxy IP burned", "Cloudflare", "Verification wall", "Timeout"]):
                if profile.get("proxy_string"):
                    try:
                        await self._ban_proxy(profile["proxy_string"])
                        print(f"[ORCHESTRATOR:PERPLEXITY] Proxy flagged as banned for this engine.")
                    except Exception:
                        pass

        finally:
            if profile:
                await self.release_profile(profile["id"], success)

        return success

# ─────────────────────────────────────────────────────────────────────────────
# 4. Main Event Loop
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n[SYSTEM] Starting PERPLEXITY Agent Microservice...")

    db_manager = DatabaseManager()
    await db_manager.connect()

    print(f"[PERPLEXITY] Resetting stale profiles...")
    await db_manager.execute(
        "UPDATE browser_profiles SET status = 'EXPIRED' WHERE status = 'BUSY' AND engine_type = $1",
        ENGINE
    )

    try:
        r = redis.Redis(
            host=Config.REDIS_HOST, port=Config.REDIS_PORT,
            password=Config.REDIS_PASSWORD, decode_responses=True
        )
        await r.ping()
    except Exception as e:
        print(f"[PERPLEXITY] Redis connection failed: {e}")
        return

    factory = PerplexityFactory(db_manager)
    orchestrator = PerplexityOrchestrator(db_manager)

    factory_task = asyncio.create_task(factory.run_daemon(target_pool_size=1))
    concurrency_limit = max(1, Config.MAX_CONCURRENT_WORKERS // 3)
    semaphore = asyncio.Semaphore(concurrency_limit)
    queue_name = f"task_queue:{ENGINE}"
    dead_queue = f"{queue_name}:dead"
    MAX_ATTEMPTS = 5

    inflight = set()   # retain task refs so fire-and-forget tasks aren't GC'd mid-run

    async def requeue(task_data):
        """Requeue with an attempt counter; route to the dead-letter list past the cap."""
        task_data["attempts"] = int(task_data.get("attempts", 0)) + 1
        if task_data["attempts"] >= MAX_ATTEMPTS:
            print(f"[PERPLEXITY] Task {task_data.get('task_id')} hit {MAX_ATTEMPTS} attempts — dead-lettering.")
            await r.lpush(dead_queue, json.dumps(task_data))
        else:
            # backoff scales with attempts so a hard-failing task can't hot-loop
            await asyncio.sleep(min(2 * task_data["attempts"], 10))
            await r.lpush(queue_name, json.dumps(task_data))

    async def process_task(task_data):
        # Semaphore is already held by the dispatcher before this task was created.
        try:
            success = await orchestrator.process_task(task_data)
            if not success:
                print(f"[PERPLEXITY] Task {task_data.get('task_id')} failed — requeuing.")
                await requeue(task_data)
        except Exception as e:
            print(f"[PERPLEXITY] CRITICAL: {e}")
            await requeue(task_data)
        finally:
            semaphore.release()

    print(f"\n[PERPLEXITY] Dispatcher online. Listening on {queue_name}...")
    try:
        while True:
            # Acquire a slot FIRST, then pop — so we never RPOP a job we can't run.
            await semaphore.acquire()
            raw_task = None
            try:
                avail = await db_manager.fetchval(
                    "SELECT COUNT(*) FROM browser_profiles WHERE engine_type = $1 AND status = 'AVAILABLE'", ENGINE
                )
                if avail > 0 and await r.llen(queue_name) > 0:
                    raw_task = await r.rpop(queue_name)
            except Exception as e:
                print(f"[PERPLEXITY] Dispatch poll error: {e}")

            if raw_task:
                task = asyncio.create_task(process_task(json.loads(raw_task)))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            else:
                # nothing to dispatch — release the slot and idle
                semaphore.release()
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        if not factory_task.done():
            factory_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(factory_task), timeout=3)
            except Exception:
                pass
        try:
            await db_manager.close()
        except Exception:
            pass
        try:
            await r.aclose()
        except Exception:
            pass

if __name__ == "__main__":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)

    def _silence_cleanup_noise(loop, context):
        exc = context.get("exception")
        if isinstance(exc, (RuntimeError, ValueError)) and any(
            phrase in str(exc) for phrase in ["Event loop is closed", "I/O operation on closed pipe"]
        ):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_silence_cleanup_noise)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print(f"\n[PERPLEXITY] Terminated.")
    finally:
        loop.close()