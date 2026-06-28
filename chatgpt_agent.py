import os
import uuid
import time
import json
import random
import asyncio
import redis.asyncio as redis
from playwright.async_api import async_playwright
from src.database import DatabaseManager
from src.config import Config
from src.utils import parse_proxy, safe_task_id

ENGINE = "chatgpt"
DEBUG_HEADLESS = os.getenv("DEBUG_HEADLESS", "1") != "0"

# ChatGPT presents an anonymous-use wall to Firefox (Camoufox) — it accepts the prompt
# then force-redirects to login. Chromium presenting as Chrome passes. So this engine
# alone runs on Playwright Chromium with a Chrome fingerprint, not Camoufox.
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
SEC_CH_UA = '"Google Chrome";v="149", "Chromium";v="149", "Not A;Brand";v="24"'
# Injected before page scripts: real Chrome has window.chrome.runtime — its absence is a
# classic headless-Chrome tell that ChatGPT uses to soft-block (withhold the token stream).
CHATGPT_INIT_JS = """
try {
  Object.defineProperty(navigator, 'webdriver', {get: () => false});
  Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
  Object.defineProperty(navigator, 'userAgent', {get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36'});
  window.chrome = window.chrome || { runtime: {} };
  (function(){
    const orig = HTMLCanvasElement.prototype.toDataURL;
    const seed = __SEED__;
    HTMLCanvasElement.prototype.toDataURL = function() {
      try { return orig.apply(this, arguments) + '?fp=' + seed; } catch(e) { return orig.apply(this, arguments); }
    };
  })();
} catch(e){}
"""

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
        print(f"[WORKER:CHATGPT] Launching Chromium for task {safe_task_id(task_id)}")

        proxy_cfg = self._parse_proxy(self.proxy_string)
        # Refuse to launch unproxied when a proxy was assigned — proxy=None leaks the real IP.
        if self.proxy_string and proxy_cfg is None:
            raise Exception(f"Proxy string is malformed; refusing to launch unproxied: {self.proxy_string!r}")

        # Per-profile canvas seed so the fingerprint is stable per profile but varies across them.
        seed = abs(hash(self.profile_path)) % (10 ** 8)
        init_js = CHATGPT_INIT_JS.replace("__SEED__", str(seed))

        async with async_playwright() as pw:
            launch_kwargs = {
                "headless": DEBUG_HEADLESS,
                "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                         "--disable-dev-shm-usage", "--hide-scrollbars"],
            }
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg

            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
                    user_agent=CHROME_UA,
                    viewport={"width": 1440, "height": 1080},
                    locale="en-US",
                )
                await context.add_init_script(init_js)
                await context.set_extra_http_headers({
                    "accept-language": "en-US,en;q=0.9",
                    "sec-ch-ua": SEC_CH_UA,
                    "sec-ch-ua-platform": '"Windows"',
                })
                page = await context.new_page()
                page.on("pageerror", lambda exc: None)

                # Old domain + networkidle: the recipe that passes ChatGPT's anonymous gate.
                print(f"[WORKER:CHATGPT] Navigating to chat (networkidle) ...")
                await page.goto("https://chat.openai.com/chat", wait_until="networkidle", timeout=60000)
                await asyncio.sleep(2)

                if "auth.openai.com" in page.url or "log-in" in page.url:
                    raise Exception("Verification wall hit: ChatGPT redirected to login on load.")

                print(f"[WORKER:CHATGPT] Waiting for chat input ...")
                input_box = page.locator(
                    'textarea:visible, [contenteditable="true"]:visible, #prompt-textarea:visible'
                ).first
                try:
                    await input_box.wait_for(state="visible", timeout=20000)
                except Exception:
                    raise Exception("Chat input not found — likely a login wall or proxy block.")

                print(f"[WORKER:CHATGPT] Typing prompt (human cadence) ...")
                await input_box.click(force=True)
                await asyncio.sleep(random.uniform(0.4, 0.9))
                # Type character-by-character with gaussian-distributed delays. ChatGPT's
                # behavioural detection flags instant whole-prompt fills (insert_text) and
                # kills the token stream mid-generation — human cadence avoids that.
                for ch in prompt:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(max(0.02, random.gauss(0.075, 0.03)))
                await asyncio.sleep(random.uniform(0.5, 1.2))
                await page.keyboard.press("Enter")
                print(f"[WORKER:CHATGPT] Prompt dispatched. Monitoring generation...")

                await asyncio.sleep(2)
                if "auth.openai.com" in page.url or "log-in" in page.url:
                    raise Exception("Verification wall hit: ChatGPT redirected to login on submit.")

                # ── Dynamic Witness Loop ──
                print(f"[WORKER:CHATGPT] Tracking output buffer...")
                previous_length = 0
                stable_ticks = 0
                for tick in range(180):  # 180s ceiling — ChatGPT web-search answers can be slow
                    await asyncio.sleep(1.0)
                    if "auth.openai.com" in page.url or "log-in" in page.url:
                        raise Exception("Verification wall hit: ChatGPT redirected to login mid-generation.")
                    try:
                        state = await page.evaluate(
                            "() => {"
                            " const stop = document.querySelector('button[data-testid=\"stop-button\"], button[aria-label*=\"Stop\" i], .result-streaming');"
                            " const e = document.querySelectorAll('[data-message-author-role=assistant]');"
                            " const txt = e.length ? (e[e.length-1].innerText || '') : '';"
                            " return { generating: !!stop, len: txt.length };"
                            " }"
                        )
                        is_still_generating = state["generating"]
                        current_length = state["len"]
                        if tick % 10 == 0:
                            print(f"[WORKER:CHATGPT]   [{tick}s] len={current_length} generating={is_still_generating}")
                        if not is_still_generating and current_length == previous_length and current_length > 0:
                            stable_ticks += 1
                            if stable_ticks >= 3:
                                print(f"[WORKER:CHATGPT] Output stabilized.")
                                break
                        else:
                            stable_ticks = 0
                            previous_length = current_length
                    except Exception:
                        pass

                # Final read (JS, timeout-safe)
                response_text = await page.evaluate(
                    "() => { const e=document.querySelectorAll('[data-message-author-role=assistant]');"
                    " return e.length ? e[e.length-1].innerText : ''; }"
                )
                if not response_text:
                    raise Exception("Could not extract response text.")

                if any(phrase in response_text.lower() for phrase in [
                    "log in to continue", "sign in to continue", "please log in",
                    "you need to log in", "create a free account", "sign up to continue"
                ]):
                    raise Exception("Verification wall hit: ChatGPT requires login.")

                # Open the search sources panel if present (only when web search fired)
                try:
                    sources_btn = page.locator('button:has-text("Sources"), button:has-text("sources")').first
                    if await sources_btn.count() > 0 and await sources_btn.is_visible():
                        await sources_btn.click()
                        await asyncio.sleep(2.0)
                except Exception:
                    pass

                # Extract real external source URLs (strip OpenAI/Google chrome + utm noise)
                sources = await page.evaluate("""() => {
                    const BLOCKED = ['chatgpt.com', 'openai.com', 'oaistatic.com',
                                     'oaiusercontent.com', 'chat.com'];
                    const seen = new Set(); const out = [];
                    document.querySelectorAll('a[href^="http"]').forEach(a => {
                        let href; try { href = new URL(a.href); } catch { return; }
                        const host = href.hostname.toLowerCase();
                        if (BLOCKED.some(b => host === b || host.endsWith('.' + b))) return;
                        href.searchParams.delete('utm_source');
                        const clean = href.href.replace(/[?&]utm_source=[^&]*/,'');
                        if (!seen.has(clean)) { seen.add(clean); out.push(clean); }
                    });
                    return out;
                }""")
                unique_sources = list(dict.fromkeys(sources))
                print(f"[WORKER:CHATGPT] Extracted {len(unique_sources)} source URLs.")

                # Light mode for clean screenshots (next-themes: class on <html>)
                await page.evaluate("""() => {
                    document.documentElement.classList.remove('dark');
                    document.documentElement.classList.add('light');
                    document.documentElement.setAttribute('data-theme', 'light');
                    document.documentElement.style.colorScheme = 'light';
                }""")
                await asyncio.sleep(1.0)

                # Stretch the VIEWPORT to the full chat height so the whole conversation
                # renders on-screen. ChatGPT's inner scroll container clips a normal
                # full_page shot; resizing the window forces it to lay everything out.
                try:
                    chat_height = await page.evaluate("""() => {
                        const main = document.querySelector('main');
                        return main ? main.scrollHeight : document.body.scrollHeight;
                    }""")
                    new_height = min(max(1080, int(chat_height) + 200), 20000)  # cap to avoid GPU limits
                    await page.set_viewport_size({"width": 1440, "height": new_height})
                    await asyncio.sleep(1.5)
                except Exception as e:
                    print(f"[WORKER:CHATGPT] viewport resize warning: {e}")

                shot_path = os.path.join(self.screenshot_dir, f"{safe_task_id(task_id)}.jpg")
                await page.screenshot(path=shot_path, full_page=True, type="jpeg", quality=85)
                print(f"[WORKER:CHATGPT] Verification screenshot saved: {shot_path}")

                return {
                    "ai_response": response_text,
                    "sources": unique_sources[:15],  # cap at 15 to avoid database bloat
                    "screenshot_path": shot_path,
                }
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass


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
                if self._parse_proxy(proxy_str) is None:
                    raise RuntimeError(f"Malformed proxy string from DB: {proxy_str!r}")

                row = await conn.fetchrow("""
                    INSERT INTO browser_profiles
                        (profile_name, engine_type, storage_path, proxy_string, status, trust_score, created_at)
                    VALUES ($1, $2, $3, $4, 'BUSY', 100, CURRENT_TIMESTAMP)
                    RETURNING id;
                """, profile_name, self.engine, profile_path, proxy_str)
                profile_id = row["id"]

        # ChatGPT runs on fresh, non-persistent Chromium contexts per task (the only way
        # past its anonymous-use gate). There's no Camoufox profile to warm — the worker
        # spins up a clean Chrome each run. The Factory's job here is just to register a
        # profile record bound to an unused proxy so the Orchestrator has one to check out.
        print(f"\n[FACTORY:CHATGPT] Registered profile '{profile_name}' on proxy {proxy_str.split(':')[0]} (Chromium, no warm-up needed).")

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