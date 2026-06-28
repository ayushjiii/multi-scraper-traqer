import os
import uuid
import time
import json
import re
import asyncio
from urllib.parse import quote_plus
import redis.asyncio as redis
from camoufox.async_api import AsyncCamoufox
from src.database import DatabaseManager
from src.config import Config
from src.utils import parse_proxy, safe_task_id

ENGINE = "aio"
DEBUG_HEADLESS = os.getenv("DEBUG_HEADLESS", "1") != "0"

# Google interface language + country. US/English is the flagship market with the
# broadest, most reliable AI Overview coverage.
GL = os.getenv("AIO_GL", "us")
HL = os.getenv("AIO_HL", "en")

# Domains that are Google chrome / infra / its own self-citations, never a real
# external cited source. AIO citation URLs are direct publisher deep-links (no
# /url?q= redirect wrapping), so we only need to drop Google's own properties.
_SOURCE_JUNK = (
    "google.com", "gstatic", "googleapis", "googleusercontent", "ggpht",
    "gvt1", "gvt2", "schema.org", "w3.org", "googletagmanager",
    "google-analytics", "doubleclick", "youtube.com", "youtu.be",
    "google.co", "googleadservices", "googlesyndication",
)


def _harvest_urls(text: str) -> set:
    """Extract real (non-Google) source article URLs from a network payload.

    Google's /async/ AI Overview hydration body is a chunked+Brotli HTML
    fragment (NOT JSON like Gemini's StreamGenerate). The cited URLs live inside
    it as anchor hrefs and inside HTML comments. We unescape, regex out every
    http(s) URL, strip Google '#:~:text=' highlight fragments, and drop
    infrastructure/Google domains. What remains are the genuine source articles.
    """
    found = set()
    cleaned = (text.replace("\\/", "/")
                   .replace("\\u003d", "=")
                   .replace("\\u0026", "&")
                   .replace("&amp;", "&"))
    for m in re.findall(r'https?://[^\s"\'\\<>)\]}]+', cleaned):
        u = m.rstrip(".,;)'\"\\")
        # Strip Google "scroll to text" highlight fragment so duplicates collapse
        u = u.split("#:~:text")[0]
        if len(u) < 12 or "://" not in u:
            continue
        parts = u.split("/")
        if len(parts) < 3:
            continue
        host = parts[2].lower()
        if any(j in host for j in _SOURCE_JUNK):
            continue
        found.add(u)
    return found


# Consent cookies pre-set on .google.com so the EU "Before you continue"
# interstitial never blocks the results page (US IPs usually skip it anyway).
def _consent_cookies():
    return [
        {"name": "CONSENT", "value": "YES+", "domain": ".google.com", "path": "/"},
        {"name": "SOCS", "value": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg",
         "domain": ".google.com", "path": "/"},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 1. AIO Worker
# ─────────────────────────────────────────────────────────────────────────────
class AioWorker:
    def __init__(self, profile_path: str, proxy_string: str = None):
        self.profile_path = profile_path
        self.proxy_string = proxy_string
        self.screenshot_dir = os.path.join(os.getcwd(), "screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)

        # AI Overviews live on a normal Google Search results page (no udm param).
        # Google decides per-query whether to render one.
        self.search_base = "https://www.google.com/search"

        # Block detection — anchor on STABLE attributes + the literal "AI Overview"
        # heading text. Google rotates obfuscated CSS class names every 4-8 weeks,
        # so class names are only ever a last-resort fallback.
        self.block_selectors = [
            'div[data-attrid="AIOverview"]',
            '[data-attrid*="overview" i]',
            '[aria-label*="AI Overview" i]',
            'div[jsname="N760b"]',
            '#m-x-content',
        ]

    def _parse_proxy(self, proxy_str: str) -> dict:
        return parse_proxy(proxy_str)

    def _build_url(self, prompt: str) -> str:
        return f"{self.search_base}?q={quote_plus(prompt)}&hl={HL}&gl={GL}"

    async def execute_task(self, prompt: str, task_id: str):
        print(f"[WORKER:AIO] Launching profile: {os.path.basename(self.profile_path)}")

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
            locale=f"{HL}-{GL.upper()}",
        ) as browser:
            page = browser.pages[0] if browser.pages else await browser.new_page()

            # NOTE: we deliberately do NOT subscribe to page.on("pageerror").
            # Google Search emits uncaught JS errors whose `location` is undefined,
            # and the Playwright-Firefox driver crashes dereferencing
            # pageError.location.url whenever a pageerror listener is attached —
            # which kills the whole context ("Connection closed while reading from
            # the driver"). Not subscribing avoids arming that buggy code path.

            # Pre-set consent so the EU interstitial never blocks results.
            try:
                await browser.add_cookies(_consent_cookies())
            except Exception:
                pass

            # ── Network source capture ──
            # AI Overview content + its citations hydrate via a separate async
            # request to a /async/ endpoint AFTER the initial search HTML. The
            # exact slug (/async/folsrch etc.) rotates, so we match the path
            # PREFIX. The body is a streamed HTML fragment — we harvest hrefs from
            # it. This survives DOM obfuscation and the citation-pill click dance.
            captured_source_urls = set()
            captured_async = []   # for first-run debugging: which /async/ slugs fired

            async def _capture_response(resp):
                try:
                    url = resp.url
                    if "/async/" in url and "google.com" in url:
                        captured_async.append(url.split("?")[0])
                        body = await resp.text()
                        for u in _harvest_urls(body):
                            captured_source_urls.add(u)
                except Exception:
                    pass

            page.on("response", _capture_response)

            async def safe_route_handler(route):
                try:
                    if route.request.resource_type == "media":
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    pass

            await page.route("**/*", safe_route_handler)

            search_url = self._build_url(prompt)
            print(f"[WORKER:AIO] Searching: {prompt[:80]}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

            # ── Block obvious failure states early ──
            # Google serves the "unusual traffic" reCAPTCHA two ways: a redirect to
            # /sorry/, OR inline on the /search URL (URL keeps q= but gains &sei= and
            # the body is just the CAPTCHA). So we check URL *and* page content — a
            # URL-only check misses the inline form. consent.google.com = EU wall.
            if "/sorry/" in page.url or "consent.google.com" in page.url:
                raise Exception("Google blocked this IP (CAPTCHA/consent wall).")
            try:
                is_captcha = await page.evaluate("""() => {
                    const t = (document.body && document.body.innerText || '');
                    return /unusual traffic|not a robot|systems have detected/i.test(t)
                           && t.length < 1500;  // a real SERP is far longer
                }""")
            except Exception:
                is_captcha = False
            if is_captcha:
                raise Exception("Google blocked this IP (CAPTCHA — unusual traffic).")

            # Belt-and-braces consent dismissal if the cookie didn't take.
            try:
                accept = page.locator("#L2AGLb, button:has-text('Accept all'), button:has-text('I agree')").first
                if await accept.count() > 0 and await accept.is_visible():
                    await accept.click()
                    await asyncio.sleep(1.0)
            except Exception:
                pass

            # ── Wait for an AI Overview to (maybe) appear ──
            # AIO is deferred/lazy-loaded AND non-deterministic: only ~30-48% of
            # queries get one. We poll for the block with a bounded budget and
            # treat "no AIO" as a LEGITIMATE empty result, never as an error.
            print(f"[WORKER:AIO] Waiting for AI Overview to hydrate (if present)...")
            block_present = False
            for _ in range(20):  # ~20s budget for the async hydration
                await asyncio.sleep(1.0)
                # Wrap in try/except: Google re-renders the SERP as the AIO hydrates,
                # which can transiently detach nodes and make a single evaluate() throw.
                # A hiccup on one tick must not abort the whole task — just retry next tick.
                try:
                    block_present = await page.evaluate(f"""() => {{
                        // 1. Attribute/jsname anchors
                        const sels = {json.dumps(self.block_selectors)};
                        for (const s of sels) {{
                            const el = document.querySelector(s);
                            if (el && (el.innerText || '').trim().length > 80) return true;
                        }}
                        // 2. Heading-text anchor: any heading reading "AI Overview"
                        const heads = document.querySelectorAll('h1,h2,h3,[role="heading"],strong');
                        for (const h of heads) {{
                            if ((h.innerText || '').trim().toLowerCase() === 'ai overview') return true;
                        }}
                        return false;
                    }}""")
                except Exception:
                    block_present = False
                if block_present:
                    break

            if captured_async:
                print(f"[WORKER:AIO] /async/ endpoints seen: {sorted(set(captured_async))}")

            # ── No AI Overview for this query — legitimate empty result ──
            if not block_present:
                print(f"[WORKER:AIO] No AI Overview rendered for this query (expected for ~50-70% of queries).")
                shot_path = await self._screenshot(page, task_id)
                return {
                    "ai_response": "",
                    "sources": [],
                    "screenshot_path": shot_path,
                    "aio_present": False,
                }

            print(f"[WORKER:AIO] AI Overview detected. Extracting answer text...")

            # Let the async stream finish filling the block, then expand if collapsed.
            await asyncio.sleep(2.0)
            try:
                show_more = page.locator(
                    'div[aria-label*="Show more" i], button:has-text("Show more")'
                ).first
                if await show_more.count() > 0 and await show_more.is_visible():
                    await show_more.click()
                    await asyncio.sleep(1.5)
            except Exception:
                pass

            # ── Extract answer text from the cloned, cleaned block ──
            # Clone the container, strip <style>/<script>/citation-badge noise, then
            # read innerText. Raw innerText leaks CSS fragments and badge labels.
            response_text = await page.evaluate(f"""() => {{
                const sels = {json.dumps(self.block_selectors)};
                let block = null;
                for (const s of sels) {{
                    const el = document.querySelector(s);
                    if (el && (el.innerText || '').trim().length > 80) {{ block = el; break; }}
                }}
                if (!block) {{
                    // Fall back to the nearest container of an "AI Overview" heading
                    const heads = document.querySelectorAll('h1,h2,h3,[role="heading"],strong');
                    for (const h of heads) {{
                        if ((h.innerText || '').trim().toLowerCase() === 'ai overview') {{
                            block = h.closest('div[jsname], section, [data-attrid], [role="region"]') || h.parentElement;
                            break;
                        }}
                    }}
                }}
                if (!block) return '';
                const clone = block.cloneNode(true);
                clone.querySelectorAll('style,script,template,noscript').forEach(n => n.remove());
                let txt = (clone.innerText || '').trim();
                // Drop the leading "AI Overview" heading line itself
                txt = txt.replace(/^\\s*AI Overview\\s*\\n+/i, '').trim();
                return txt;
            }}""")

            if not response_text or len(response_text) < 40:
                # Block detected but text never filled — treat as a soft failure so
                # it retries on a fresh proxy rather than saving a useless stub.
                raise Exception("AI Overview block detected but text never populated.")

            print(f"[WORKER:AIO] Response captured ({len(response_text)} chars). Resolving sources...")

            # ── Source extraction ──
            # 1) Network capture is primary (already collected from /async/ body).
            # 2) DOM fallback: click citation pills to reveal the source list, then
            #    read external anchor hrefs from the block.
            await asyncio.sleep(1.0)
            sources = sorted(captured_source_urls)

            if not sources:
                print(f"[WORKER:AIO] Network empty — falling back to DOM citation extraction.")
                # Reveal citations: click any citation pills inside the block.
                try:
                    pills = page.locator('[jsname="HtgYJd"], div[aria-label="View related links"], span[role="button"][aria-expanded]')
                    n = min(await pills.count(), 6)
                    for i in range(n):
                        try:
                            p = pills.nth(i)
                            if await p.is_visible():
                                await p.click()
                                await asyncio.sleep(0.4)
                        except Exception:
                            pass
                except Exception:
                    pass

                dom_links = await page.evaluate(f"""() => {{
                    const sels = {json.dumps(self.block_selectors)};
                    let block = null;
                    for (const s of sels) {{
                        const el = document.querySelector(s);
                        if (el && (el.innerText || '').trim().length > 80) {{ block = el; break; }}
                    }}
                    const scope = block || document.querySelector('#m-x-content') || document;
                    const out = [];
                    for (const a of scope.querySelectorAll('a[href^="http"]')) {{
                        const href = a.href || '';
                        if (href) out.push(href);
                    }}
                    return out;
                }}""")
                # Reuse the same junk filter / fragment stripping as the network path.
                clean = set()
                for href in dom_links:
                    clean |= _harvest_urls(href)
                sources = sorted(clean)
                if sources:
                    print(f"[WORKER:AIO] DOM fallback found {len(sources)} citation links.")
                else:
                    print(f"[WORKER:AIO] No external citations found (AIO can cite zero external sources).")
            else:
                print(f"[WORKER:AIO] Captured {len(sources)} source URLs from network.")

            unique_sources = list(dict.fromkeys(sources))

            shot_path = await self._screenshot(page, task_id)

            return {
                "ai_response": response_text,
                "sources": unique_sources[:15],   # cap to avoid DB bloat
                "screenshot_path": shot_path,
                "aio_present": True,
            }

    async def _screenshot(self, page, task_id: str) -> str:
        """Full-page JPEG of the SERP (with the AI Overview if present)."""
        try:
            # Light-mode + expand any clipped scrollers so the full page renders.
            await page.evaluate("""() => {
                document.documentElement.style.colorScheme = 'light';
            }""")
            await asyncio.sleep(0.5)
            shot_path = os.path.join(self.screenshot_dir, f"{safe_task_id(task_id)}.jpg")
            await page.screenshot(path=shot_path, full_page=True, type="jpeg", quality=85)
            print(f"[WORKER:AIO] Verification screenshot saved: {shot_path}")
            return shot_path
        except Exception as e:
            print(f"[WORKER:AIO] Screenshot failed: {e}")
            return ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. AIO Factory
# ─────────────────────────────────────────────────────────────────────────────
class AioFactory:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.engine = "aio"
        self.profiles_dir = os.path.join(os.getcwd(), f"profiles/{self.engine}")
        os.makedirs(self.profiles_dir, exist_ok=True)
        self.url = "https://www.google.com/"

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

        print(f"\n[FACTORY:AIO] Warming up new profile: '{profile_name}' on proxy {proxy_str.split(':')[0]}")

        try:
            async with AsyncCamoufox(
                headless=DEBUG_HEADLESS,
                persistent_context=True,
                user_data_dir=profile_path,
                proxy=camoufox_proxy,
                geoip=True,
                locale=f"{HL}-{GL.upper()}",
            ) as browser:
                page = browser.pages[0] if browser.pages else await browser.new_page()
                # See worker note: no page.on("pageerror") — it crashes the
                # Playwright-Firefox driver on Google's malformed pageerror events.

                try:
                    await browser.add_cookies(_consent_cookies())
                except Exception:
                    pass

                async def safe_route_handler(route):
                    try:
                        if route.request.resource_type == "media":
                            await route.abort()
                            return
                        await route.continue_()
                    except Exception:
                        pass

                await page.route("**/*", safe_route_handler)

                print(f"[FACTORY:AIO] Validating proxy can reach Google ({self.url})...")
                await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)

                # The whole point of warm-up here: prove this IP isn't already
                # CAPTCHA-walled. Datacenter IPs fail HERE, before being marked
                # AVAILABLE, so a bad proxy never gets a real task.
                if "/sorry/" in page.url:
                    raise RuntimeError("Google CAPTCHA wall on warm-up — proxy burned for Google.")

                # Dismiss consent if it still showed (EU IP, cookie didn't take).
                if "consent.google.com" in page.url:
                    try:
                        accept = page.locator("#L2AGLb, button:has-text('Accept all')").first
                        if await accept.count() > 0:
                            await accept.click()
                            await asyncio.sleep(1.0)
                    except Exception:
                        pass
                    if "consent.google.com" in page.url:
                        raise RuntimeError("Stuck on Google consent wall — proxy unusable.")

                # Confirm the search box rendered (i.e. a real Google homepage).
                try:
                    await page.wait_for_selector('textarea[name="q"], input[name="q"]', timeout=15000)
                    print(f"[FACTORY:AIO] Google reachable, search box present. Proxy is good.")
                except Exception:
                    raise RuntimeError("Google homepage didn't render a search box — proxy/network issue.")

        except Exception as e:
            error_msg = str(e)
            print(f"[FACTORY:AIO] Error warming aio: {error_msg}")

            try: await self.db.execute("DELETE FROM browser_profiles WHERE id = $1", profile_id)
            except Exception: pass

            # Ban ONLY truly dead proxies (can't connect at all). A Google CAPTCHA /
            # consent wall is largely IP-reputation/rate-based and can lift later, so
            # we do NOT permaban on it — otherwise one seed run burns the whole free
            # pool and leaves nothing to retry with. (Same lesson as the ChatGPT agent:
            # walls are probabilistic per-attempt; only dead proxies are permanent.)
            dead_proxy_signals = [
                "NS_ERROR_PROXY", "Connection refused", "Failed to connect",
                "ERR_PROXY", "ECONNREFUSED", "Proxy string is malformed",
            ]
            if any(sig in error_msg for sig in dead_proxy_signals):
                try:
                    await self.db.execute(f"UPDATE proxies SET {banned_col} = TRUE WHERE connection_string = $1", proxy_str)
                    print(f"[FACTORY:AIO] Dead proxy flagged as banned for this engine.")
                except Exception: pass
            elif any(sig in error_msg for sig in ("CAPTCHA", "consent wall")):
                print(f"[FACTORY:AIO] Proxy hit a Google wall — NOT banning (can recover); will try another proxy.")
            raise

        await self.db.execute("""
            UPDATE browser_profiles SET status = 'AVAILABLE', storage_path = $1 WHERE id = $2
        """, profile_path, profile_id)

        print(f"[FACTORY:AIO] Profile '{profile_name}' is AVAILABLE.")
        return str(profile_id)

    async def run_daemon(self, target_pool_size: int = 1):
        print(f"\n[FACTORY DAEMON:AIO] Started. Target pool: {target_pool_size}.")
        while True:
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
                print(f"[FACTORY:AIO] Pool low ({available}/{target_pool_size}). Warming...")
                try: await self.warm_new_profile()
                except Exception: pass

            await asyncio.sleep(10)


# ─────────────────────────────────────────────────────────────────────────────
# 3. AIO Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
class AioOrchestrator:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.engine = "aio"

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
            print(f"[ORCHESTRATOR:AIO] Checked out profile: {profile['profile_name']}")
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
        print(f"[ORCHESTRATOR:AIO] Released profile {profile_id} -> {new_status}")

    async def _ban_proxy(self, connection_string: str):
        # Column name is chosen from a fixed whitelist (never interpolated from input).
        col = {
            "chatgpt": "chatgpt_banned",
            "perplexity": "perplexity_banned",
            "gemini": "gemini_banned",
            "aio": "aio_banned",
        }[self.engine]
        await self.db.execute(
            f"UPDATE proxies SET {col} = TRUE WHERE connection_string = $1",
            connection_string,
        )

    async def process_task(self, task_payload: dict):
        task_id = task_payload.get("task_id", "unknown")
        prompt  = task_payload.get("prompt", "")

        if await self.check_duplicate(prompt):
            print(f"[ORCHESTRATOR:AIO] Duplicate prompt skipped.")
            return True

        print(f"\n[ORCHESTRATOR:AIO] Processing Task [{task_id}]")

        profile = await self.checkout_profile()
        if not profile:
            print(f"[ORCHESTRATOR:AIO] No AVAILABLE profile. Requeued.")
            return False

        success = False
        try:
            worker  = AioWorker(
                profile_path=profile["storage_path"],
                proxy_string=profile.get("proxy_string")
            )
            results = await worker.execute_task(
                prompt=prompt,
                task_id=task_id
            )

            # NOTE: an ungrounded "no AI Overview" result (aio_present=False) is a
            # VALID, completed scrape — we record it so we don't re-dispatch it,
            # and so GEO clients can see which queries simply have no AI Overview.
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

            state = "AI Overview" if results.get("aio_present") else "no AI Overview (empty)"
            print(f"[ORCHESTRATOR:AIO] Task {task_id} saved successfully — {state}.")
            success = True

        except Exception as e:
            error_msg = str(e)
            print(f"[ORCHESTRATOR:AIO] Task {task_id} failed: {error_msg}")

            # Permaban ONLY truly dead proxies. A Google CAPTCHA/consent wall is
            # rate/reputation-based and can lift, so we don't permaban on it — the
            # profile is just expired (released as EXPIRED) and the task requeues
            # onto a different proxy. Permabanning every walled IP would burn the
            # whole free pool in one run. (Same lesson as the ChatGPT agent.)
            if any(term in error_msg for term in ["NS_ERROR_PROXY", "Failed to connect",
                                                  "Connection refused", "ERR_PROXY", "ECONNREFUSED"]):
                if profile.get("proxy_string"):
                    try:
                        await self._ban_proxy(profile["proxy_string"])
                        print(f"[ORCHESTRATOR:AIO] Dead proxy flagged as banned for this engine.")
                    except Exception:
                        pass
            elif any(term in error_msg for term in ["CAPTCHA", "consent wall", "blocked this IP"]):
                print(f"[ORCHESTRATOR:AIO] Google wall hit — NOT banning proxy (can recover); task will retry elsewhere.")

        finally:
            if profile:
                await self.release_profile(profile["id"], success)

        return success


# ─────────────────────────────────────────────────────────────────────────────
# 4. Main Event Loop
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n[SYSTEM] Starting AIO (Google AI Overviews) Agent Microservice...")

    db_manager = DatabaseManager()
    await db_manager.connect()

    print(f"[AIO] Resetting stale profiles...")
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
        print(f"[AIO] Redis connection failed: {e}")
        return

    factory = AioFactory(db_manager)
    orchestrator = AioOrchestrator(db_manager)

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
            print(f"[AIO] Task {task_data.get('task_id')} hit {MAX_ATTEMPTS} attempts — dead-lettering.")
            await r.lpush(dead_queue, json.dumps(task_data))
        else:
            await asyncio.sleep(min(2 * task_data["attempts"], 10))
            await r.lpush(queue_name, json.dumps(task_data))

    async def process_task(task_data):
        # Semaphore is already held by the dispatcher before this task was created.
        try:
            success = await orchestrator.process_task(task_data)
            if not success:
                print(f"[AIO] Task {task_data.get('task_id')} failed — requeuing.")
                await requeue(task_data)
        except Exception as e:
            print(f"[AIO] CRITICAL: {e}")
            await requeue(task_data)
        finally:
            semaphore.release()

    print(f"\n[AIO] Dispatcher online. Listening on {queue_name}...")
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
                print(f"[AIO] Dispatch poll error: {e}")

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
        print(f"\n[AIO] Terminated.")
    finally:
        loop.close()
