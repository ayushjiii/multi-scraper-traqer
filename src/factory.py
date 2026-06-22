# src/factory.py
import os
import uuid
import asyncio
from camoufox.async_api import AsyncCamoufox
from src.database import DatabaseManager
from src.config import Config
from src.engine_profiles import ENGINE_PROFILES

class ProfileFactory:
    def __init__(self, db_manager: DatabaseManager, engine: str):
        self.db = db_manager
        self.engine = engine.lower()
        if self.engine not in ENGINE_PROFILES:
            raise ValueError(f"Unsupported engine: {self.engine}")
            
        self.config = ENGINE_PROFILES[self.engine]
        self.profiles_dir = os.path.join(os.getcwd(), f"profiles/{self.engine}")
        os.makedirs(self.profiles_dir, exist_ok=True)

    def _parse_proxy(self, proxy_str: str) -> dict | None:
        parts = proxy_str.split(":")
        if len(parts) != 4:
            return None
        return {
            "server":   f"http://{parts[0]}:{parts[1]}",
            "username": parts[2],
            "password": parts[3]
        }

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

                row = await conn.fetchrow("""
                    INSERT INTO browser_profiles
                        (profile_name, engine_type, storage_path, proxy_string, status, trust_score, created_at)
                    VALUES ($1, $2, $3, $4, 'BUSY', 100, CURRENT_TIMESTAMP)
                    RETURNING id
                """, profile_name, self.engine, "pending", proxy_str)

                profile_id = row["id"]

        try:
            async with AsyncCamoufox(
                headless=True, persistent_context=True,
                user_data_dir=profile_path, proxy=camoufox_proxy,
                geoip=True
                # NOTE: do NOT set locale= when geoip=True — geoip already
                # sets the locale to match the proxy country. Explicit locale
                # would conflict and cause inconsistent fingerprints.
            ) as browser:
                page = browser.pages[0] if browser.pages else await browser.new_page()
                page.on("pageerror", lambda exc: None)

                async def safe_route_handler(route):
                    try:
                        if route.request.resource_type == "media":
                            await route.abort()
                            return
                        url = route.request.url.lower()
                        if any(t in url for t in ['analytics', 'telemetry', 'sentry', 'datadog', 'mixpanel']):
                            await route.abort()
                            return
                        await route.continue_()
                    except Exception: pass

                await page.route("**/*", safe_route_handler)

                print(f"[FACTORY:{self.engine.upper()}] Navigating to {self.config['url']}...")
                await page.goto(self.config['url'], wait_until="domcontentloaded", timeout=60000)

                # ── Wait for React to hydrate ─────────────────────────────────
                print(f"[FACTORY:{self.engine.upper()}] Waiting for React hydration...")
                try:
                    await page.wait_for_selector('#root > *', timeout=30000)
                except Exception:
                    pass

                # ── Cloudflare JS Challenge Handler ───────────────────────────
                # "Just a moment..." = Cloudflare running a JS integrity check.
                # Camoufox (stealth browser) passes this ~80% of the time automatically.
                # We wait up to 15s for the challenge to resolve before giving up.
                # Only ban the proxy if the challenge page PERSISTS after waiting.
                page_title = await page.title()
                if any(ind in page_title.lower() for ind in ["just a moment", "cf-error",
                                                              "attention required", "403"]):
                    print(f"[FACTORY:{self.engine.upper()}] Cloudflare challenge detected — waiting for auto-resolve...")
                    for _ in range(15):  # wait up to 15 seconds
                        await asyncio.sleep(1)
                        page_title = await page.title()
                        page_url   = page.url
                        if not any(ind in page_title.lower() for ind in ["just a moment", "cf-error",
                                                                          "attention required", "403"]):
                            print(f"[FACTORY:{self.engine.upper()}] Cloudflare challenge resolved!")
                            break
                    else:
                        # Still blocked after 15s — this proxy is genuinely banned
                        raise RuntimeError(
                            f"Cloudflare block detected (title='{page_title}', url='{page.url}'). "
                            "Marking proxy as banned."
                        )

                # Hard block — challenge URL itself (redirect to Cloudflare)
                if "challenges.cloudflare.com" in page.url.lower():
                    raise RuntimeError(
                        f"Cloudflare block detected (url='{page.url}'). Marking proxy as banned."
                    )

                # ── Turnstile solver (runs only if a challenge frame appears) ─
                try:
                    frame = await page.wait_for_event("frameattached", timeout=5000)
                    if "challenge" in frame.url or "turnstile" in frame.url:
                        print(f"[FACTORY:{self.engine.upper()}] Turnstile detected — solving...")
                        checkbox = frame.locator('input[type="checkbox"], .mark, #challenge-stage').first
                        if await checkbox.count() > 0 and await checkbox.is_visible():
                            box = await checkbox.bounding_box()
                            if box:
                                cx = box["x"] + box["width"] / 2
                                cy = box["y"] + box["height"] / 2
                                await page.mouse.move(cx, cy, steps=15)
                                await asyncio.sleep(0.2)
                                await page.mouse.click(cx, cy)
                                await asyncio.sleep(2)
                except Exception:
                    pass  # No challenge frame — good, continue normally

                # ── Cookie consent (locale-independent) ──────────────────────
                # Use #cookie-consent ID + last button — works in any language.
                try:
                    consent = page.locator('#cookie-consent button').last
                    if await consent.is_visible():
                        await consent.click()
                        print(f"[FACTORY:{self.engine.upper()}] Cookie consent dismissed.")
                        await asyncio.sleep(0.8)
                except Exception:
                    pass

                # ── Login modal (soft dismiss) ─────────────────────────────────
                try:
                    close_btn = page.locator('div[role="dialog"] button').last
                    if await close_btn.is_visible():
                        await close_btn.click()
                        await asyncio.sleep(0.3)
                except Exception:
                    pass

                # Remove Google One Tap iframes
                await page.evaluate("""() => {
                    document.querySelectorAll(
                        'iframe[src*="accounts.google"], iframe[src*="smartlock"], #credential_picker_container'
                    ).forEach(el => el.remove());
                }""")

                # ── Find and use the Lexical editor ──────────────────────────
                # Use state="attached" (in DOM) not "visible" — the editor IS in
                # the DOM even when the login modal sits on top of it.
                # Then use click(force=True) to bypass pointer-events from the overlay.
                print(f"[FACTORY:{self.engine.upper()}] Waiting for search editor...")
                editor = page.locator('[data-lexical-editor="true"]').first
                try:
                    await editor.wait_for(state="attached", timeout=15000)
                except Exception:
                    raise RuntimeError(
                        "Editor not found after 15s — page may be a Cloudflare challenge "
                        "or Perplexity has changed its DOM structure."
                    )

                await editor.click(force=True)
                await asyncio.sleep(0.3)
                await page.keyboard.insert_text(self.config['tiny_prompt'])
                await asyncio.sleep(0.5)
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)
                print(f"[FACTORY:{self.engine.upper()}] Tiny prompt accepted. Session trusted.")


        except Exception as e:
            error_msg = str(e)
            print(f"[FACTORY:{self.engine.upper()}] Error warming {self.engine}: {error_msg}")

            try: await self.db.execute("DELETE FROM browser_profiles WHERE id = $1", profile_id)
            except Exception: pass

            proxy_ban_signals = [
                "NS_ERROR_PROXY", "Connection refused", "Failed to connect",
                "Cloudflare block", "ERR_PROXY", "ECONNREFUSED"
            ]
            if any(sig in error_msg for sig in proxy_ban_signals):
                try:
                    await self.db.execute(f"UPDATE proxies SET {banned_col} = TRUE WHERE connection_string = $1", proxy_str)
                    print(f"[FACTORY:{self.engine.upper()}] Proxy flagged as banned for this engine.")
                except Exception: pass
            raise

        await self.db.execute("""
            UPDATE browser_profiles SET status = 'AVAILABLE', storage_path = $1 WHERE id = $2
        """, profile_path, profile_id)

        print(f"[FACTORY:{self.engine.upper()}] Profile '{profile_name}' is AVAILABLE.")
        return str(profile_id)

    async def run_daemon(self, target_pool_size: int = 1):
        print(f"\n[FACTORY DAEMON:{self.engine.upper()}] Started. Target pool: {target_pool_size}.")
        while True:
            await self.db.execute(f"""
                UPDATE browser_profiles SET status = 'EXPIRED'
                WHERE status = 'AVAILABLE' AND engine_type = '{self.engine}'
                AND created_at < NOW() - INTERVAL '{Config.PROFILE_TTL_MINUTES} minutes'
            """)

            available = await self.db.fetchval(
                "SELECT COUNT(*) FROM browser_profiles WHERE engine_type = $1 AND status = 'AVAILABLE'", self.engine
            )
            if available < target_pool_size:
                print(f"[FACTORY:{self.engine.upper()}] Pool low ({available}/{target_pool_size}). Warming...")
                try: await self.warm_new_profile()
                except Exception: pass

            await asyncio.sleep(30)