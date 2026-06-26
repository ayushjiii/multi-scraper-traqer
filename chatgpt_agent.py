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

ENGINE = "chatgpt"
DEBUG_HEADLESS = os.getenv("DEBUG_HEADLESS", "1") != "0"

# ─────────────────────────────────────────────────────────────────────────────
# 1. ChatGPT Worker
# ─────────────────────────────────────────────────────────────────────────────
class ChatGPTWorker:
    def __init__(self, profile_path: str, proxy_string: str = None):
        self.profile_path = profile_path
        self.proxy_string = proxy_string
        self.screenshot_dir = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)
        
        # ChatGPT-specific selectors (Next.js / ProseMirror, as of 2025)
        self.url = "https://chatgpt.com/"
        self.input_selector = 'div#prompt-textarea, div#prompt-textarea.ProseMirror'
        self.response_selector = '[data-message-author-role="assistant"]'
        self.send_btn_selector = 'button[data-testid="send-button"], button#composer-submit-button'
        self.stop_btn_selector = 'button[aria-label*="Stop" i], button[data-testid="stop-button"]'

    def _parse_proxy(self, proxy_str: str) -> dict:
        return parse_proxy(proxy_str)

    async def execute_task(self, prompt: str, task_id: str):
        print(f"[WORKER:CHATGPT] Launching profile: {os.path.basename(self.profile_path)}")

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

            # Fresh chat — avoids inheriting warm-up thread
            print(f"[WORKER:CHATGPT] Navigating to fresh chat ...")
            await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)

            print(f"[WORKER:CHATGPT] Waiting for Next.js to hydrate...")
            try:
                await page.wait_for_selector('div#prompt-textarea, button[data-testid="send-button"]', timeout=30000)
            except Exception:
                raise Exception("Page blank after reload — proxy or network issue.")

            print(f"[WORKER:CHATGPT] Executing CSS Layer Hider...")
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

            print(f"[WORKER:CHATGPT] Injecting prompt ...")
            editor = page.locator(self.input_selector).first
            try:
                await editor.wait_for(state="attached", timeout=30000)
            except Exception:
                raise Exception("Editor not found — likely a hard login wall or proxy block.")

            await editor.click()
            await asyncio.sleep(0.3)
            # Type character-by-character so React's synthetic event handler registers each keystroke
            await page.keyboard.type(prompt, delay=30)
            await asyncio.sleep(0.5)

            # After typing, wait for the send button to become enabled (React may take a moment)
            try:
                await page.locator('button[data-testid="send-button"]:not([disabled])').wait_for(state="visible", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.3)

            # Dismiss any open tooltip/popover that can intercept pointer events on the send button
            await page.evaluate("""() => {
                document.querySelectorAll('[popover]').forEach(el => {
                    try { el.hidePopover(); } catch {}
                });
            }""")
            await asyncio.sleep(0.1)

            # Fire the active submit button — force=True bypasses any remaining overlay intercept
            send_btn = page.locator(self.send_btn_selector).first
            if await send_btn.count() > 0 and await send_btn.is_visible():
                try:
                    await send_btn.click(force=True)
                except Exception:
                    await page.keyboard.press("Enter")
            else:
                await page.keyboard.press("Enter")
                
            print(f"[WORKER:CHATGPT] Prompt successfully dispatched. Monitoring runtime generation cycle...")

            # ── Dynamic Witness Loop ──
            await asyncio.sleep(3.0)
            
            try:
                await page.locator(self.response_selector).last.wait_for(state="visible", timeout=35000)
            except Exception:
                raise Exception("Generation element failed to anchor in window buffer. Threat signature suspected.")

            print(f"[WORKER:CHATGPT] Stream processing confirmed active. Tracking output buffer limits...")

            previous_length = 0
            stable_ticks = 0

            for _ in range(90):  # 90 seconds maximum execution ceiling
                await asyncio.sleep(1.0)
                try:
                    is_still_generating = await page.locator(self.stop_btn_selector).count() > 0
                    # Re-resolve .last each tick — prevents stale reference during streaming
                    current_text = await page.locator(self.response_selector).last.inner_text()
                    current_length = len(current_text)

                    if not is_still_generating and current_length == previous_length and current_length > 0:
                        stable_ticks += 1
                        if stable_ticks >= 3:
                            print(f"[WORKER:CHATGPT] Target output buffer stabilized. Preparing snapshot render...")
                            break
                    else:
                        stable_ticks = 0
                        previous_length = current_length
                except Exception:
                    pass

            # Re-resolve once more for the final read
            response_text = await page.locator(self.response_selector).last.inner_text()
            if not response_text:
                raise Exception("Could not extract response text.")

            if any(phrase in response_text.lower() for phrase in [
                "log in to continue", "sign in to continue", "please log in",
                "you need to log in", "create a free account", "sign up to continue"
            ]):
                raise Exception("Verification wall hit: ChatGPT requires login.")

            # Expand ChatGPT's search sources panel if present (only appears when web search is on)
            try:
                sources_btn = page.locator('button:has-text("sources"), button:has-text("Sources"), [data-testid="search-button"]').first
                if await sources_btn.count() > 0 and await sources_btn.is_visible():
                    await sources_btn.click()
                    await asyncio.sleep(2.0)
            except Exception:
                pass

            # Extract source URLs — only genuine external sources, strip all OpenAI/ChatGPT domains
            sources = await page.evaluate("""() => {
                const BLOCKED = [
                    'chatgpt.com', 'openai.com', 'auth.openai.com',
                    'help.openai.com', 'chat.com', 'oaistatic.com'
                ];
                return Array.from(document.querySelectorAll('a[href^="http"]'))
                    .map(a => {
                        try { return new URL(a.href).href; } catch { return null; }
                    })
                    .filter(href => {
                        if (!href) return false;
                        try {
                            const host = new URL(href).hostname;
                            return !BLOCKED.some(b => host === b || host.endsWith('.' + b));
                        } catch { return false; }
                    });
            }""")
            unique_sources = list(dict.fromkeys(sources))

            # Light mode enforcement — ChatGPT uses next-themes: class on <html> is "dark" or "light"
            await page.evaluate("""() => {
                document.documentElement.classList.remove('dark');
                document.documentElement.classList.add('light');
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
            print(f"[WORKER:CHATGPT] Verification screenshot saved: {shot_path}")

            return {
                "ai_response": response_text,
                "sources": unique_sources[:15], # cap at 15 to avoid database bloat
                "screenshot_path": shot_path,
            }


# ─────────────────────────────────────────────────────────────────────────────
# 2. ChatGPT Factory
# ─────────────────────────────────────────────────────────────────────────────
class ChatGPTFactory:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.engine = "chatgpt"
        self.profiles_dir = os.path.join(os.getcwd(), f"profiles/{self.engine}")
        os.makedirs(self.profiles_dir, exist_ok=True)
        self.url = "https://chatgpt.com"
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

        print(f"\n[FACTORY:CHATGPT] Warming up new profile: '{profile_name}' on proxy {proxy_str.split(':')[0]}")

        try:
            async with AsyncCamoufox(
                headless=DEBUG_HEADLESS,
                persistent_context=True,
                user_data_dir=profile_path,
                proxy=camoufox_proxy,
                geoip=True
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

                print(f"[FACTORY:CHATGPT] Navigating to {self.url}...")
                await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)

                # ── Cloudflare JS Challenge Handler ───────────────────────────
                page_title = await page.title()
                if any(ind in page_title.lower() for ind in ["just a moment", "cf-error", "attention required", "403"]):
                    print(f"[FACTORY:CHATGPT] Cloudflare challenge detected — waiting for auto-resolve...")
                    for _ in range(15):
                        await asyncio.sleep(1)
                        page_title = await page.title()
                        if not any(ind in page_title.lower() for ind in ["just a moment", "cf-error", "attention required", "403"]):
                            print(f"[FACTORY:CHATGPT] Cloudflare challenge resolved!")
                            break
                    else:
                        raise RuntimeError(f"Cloudflare block detected. Marking proxy as banned.")

                if "challenges.cloudflare.com" in page.url.lower():
                    raise RuntimeError(f"Cloudflare hard block detected. Marking proxy as banned.")

                print(f"[FACTORY:CHATGPT] Waiting for page hydration...")
                try:
                    await page.wait_for_selector('div#prompt-textarea, button[data-testid="send-button"]', timeout=30000)
                except Exception:
                    pass

                # ── Executing CSS Element Hider ──────────────────────────────
                print(f"[FACTORY:CHATGPT] Executing CSS element hider...")
                await page.evaluate('''() => {
                    const style = document.createElement('style');
                    style.innerHTML = `
                        iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"],
                        div[role="dialog"], [class*="backdrop"], #credential_picker_container,
                        #cookie-consent {
                            display: none !important; opacity: 0 !important; pointer-events: none !important;
                            z-index: -9999 !important; visibility: hidden !important;
                        }
                    `;
                    document.head.appendChild(style);
                }''')
                await asyncio.sleep(1)

                # ── Tiny Prompt Injection ──────────────────────────────
                print(f"[FACTORY:CHATGPT] Submitting tiny prompt to initialize UI and cache WebSockets...")
                input_selector = 'div#prompt-textarea, div#prompt-textarea.ProseMirror'
                input_element = page.locator(input_selector).first

                try:
                    await input_element.wait_for(state="attached", timeout=45000)
                    await input_element.click()
                    await asyncio.sleep(0.3)

                    await page.keyboard.type(self.tiny_prompt, delay=30)
                    await asyncio.sleep(0.5)

                    try:
                        await page.locator('button[data-testid="send-button"]:not([disabled])').wait_for(state="visible", timeout=5000)
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)

                    # Dismiss tooltip/popover overlays before clicking
                    await page.evaluate("""() => {
                        document.querySelectorAll('[popover]').forEach(el => {
                            try { el.hidePopover(); } catch {}
                        });
                    }""")
                    await asyncio.sleep(0.1)

                    send_btn = page.locator('button[data-testid="send-button"], button#composer-submit-button').first
                    if await send_btn.count() > 0 and await send_btn.is_visible():
                        try:
                            await send_btn.click(force=True)
                        except Exception:
                            await page.keyboard.press("Enter")
                    else:
                        await page.keyboard.press("Enter")
                        
                    await asyncio.sleep(3)
                    print(f"[FACTORY:CHATGPT] Tiny prompt accepted. Session is fully trusted.")
                except Exception as e:
                    print(f"[FACTORY:CHATGPT] Tiny prompt injection skipped: {e}")
                
                print(f"[FACTORY:CHATGPT] Profile successfully warmed.")

        except Exception as e:
            error_msg = str(e)
            print(f"[FACTORY:CHATGPT] Error warming chatgpt: {error_msg}")

            try: await self.db.execute("DELETE FROM browser_profiles WHERE id = $1", profile_id)
            except Exception: pass

            proxy_ban_signals = [
                "NS_ERROR_PROXY", "Connection refused", "Failed to connect",
                "Cloudflare block", "ERR_PROXY", "ECONNREFUSED"
            ]
            if any(sig in error_msg for sig in proxy_ban_signals):
                try:
                    await self.db.execute(f"UPDATE proxies SET {banned_col} = TRUE WHERE connection_string = $1", proxy_str)
                    print(f"[FACTORY:CHATGPT] Proxy flagged as banned for this engine.")
                except Exception: pass
            raise

        await self.db.execute("""
            UPDATE browser_profiles SET status = 'AVAILABLE', storage_path = $1 WHERE id = $2
        """, profile_path, profile_id)

        print(f"[FACTORY:CHATGPT] Profile '{profile_name}' is AVAILABLE.")
        return str(profile_id)

    async def run_daemon(self, target_pool_size: int = 1):
        print(f"\n[FACTORY DAEMON:CHATGPT] Started. Target pool: {target_pool_size}.")
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
                print(f"[FACTORY:CHATGPT] Pool low ({available}/{target_pool_size}). Warming...")
                try: await self.warm_new_profile()
                except Exception: pass

            await asyncio.sleep(10)


# ─────────────────────────────────────────────────────────────────────────────
# 3. ChatGPT Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
class ChatGPTOrchestrator:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.engine = "chatgpt"

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
            print(f"[ORCHESTRATOR:CHATGPT] Checked out profile: {profile['profile_name']}")
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
        print(f"[ORCHESTRATOR:CHATGPT] Released profile {profile_id} -> {new_status}")

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
            print(f"[ORCHESTRATOR:CHATGPT] Duplicate prompt skipped.")
            return True

        print(f"\n[ORCHESTRATOR:CHATGPT] Processing Task [{task_id}]")

        profile = await self.checkout_profile()
        if not profile:
            print(f"[ORCHESTRATOR:CHATGPT] No AVAILABLE profile. Requeued.")
            return False

        success = False
        try:
            worker  = ChatGPTWorker(
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

            print(f"[ORCHESTRATOR:CHATGPT] Task {task_id} saved successfully.")
            success = True

        except Exception as e:
            error_msg = str(e)
            print(f"[ORCHESTRATOR:CHATGPT] Task {task_id} failed: {error_msg}")
            
            if any(term in error_msg for term in ["Proxy IP burned", "Cloudflare", "Verification wall", "Timeout"]):
                if profile.get("proxy_string"):
                    try:
                        await self._ban_proxy(profile["proxy_string"])
                        print(f"[ORCHESTRATOR:CHATGPT] Proxy flagged as banned for this engine.")
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
    print(f"\n[SYSTEM] Starting CHATGPT Agent Microservice...")

    db_manager = DatabaseManager()
    await db_manager.connect()

    print(f"[CHATGPT] Resetting stale profiles...")
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
        print(f"[CHATGPT] Redis connection failed: {e}")
        return

    factory = ChatGPTFactory(db_manager)
    orchestrator = ChatGPTOrchestrator(db_manager)

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
            print(f"[CHATGPT] Task {task_data.get('task_id')} hit {MAX_ATTEMPTS} attempts — dead-lettering.")
            await r.lpush(dead_queue, json.dumps(task_data))
        else:
            await asyncio.sleep(min(2 * task_data["attempts"], 10))
            await r.lpush(queue_name, json.dumps(task_data))

    async def process_task(task_data):
        # Semaphore is already held by the dispatcher before this task was created.
        try:
            success = await orchestrator.process_task(task_data)
            if not success:
                print(f"[CHATGPT] Task {task_data.get('task_id')} failed — requeuing.")
                await requeue(task_data)
        except Exception as e:
            print(f"[CHATGPT] CRITICAL: {e}")
            await requeue(task_data)
        finally:
            semaphore.release()

    print(f"\n[CHATGPT] Dispatcher online. Listening on {queue_name}...")
    try:
        while True:
            await semaphore.acquire()
            raw_task = None
            try:
                avail = await db_manager.fetchval(
                    "SELECT COUNT(*) FROM browser_profiles WHERE engine_type = $1 AND status = 'AVAILABLE'", ENGINE
                )
                if avail > 0 and await r.llen(queue_name) > 0:
                    raw_task = await r.rpop(queue_name)
            except Exception as e:
                print(f"[CHATGPT] Dispatch poll error: {e}")

            if raw_task:
                task = asyncio.create_task(process_task(json.loads(raw_task)))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            else:
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
        print(f"\n[CHATGPT] Terminated.")
    finally:
        loop.close()