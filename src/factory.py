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
                geoip=True, locale="en-US"
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
                
                # --- AUTOMATED TURNSTILE SOLVER ---
                print(f"[FACTORY:{self.engine.upper()}] Scanning for verification frames...")
                await asyncio.sleep(3)
                try:
                    for frame in page.frames:
                        if "challenge" in frame.url or "turnstile" in frame.url:
                            print(f"[FACTORY:{self.engine.upper()}] Turnstile detected! Simulating human clearance interaction...")
                            checkbox = frame.locator('input[type="checkbox"], .mark, #challenge-stage').first
                            if await checkbox.count() > 0 and await checkbox.is_visible():
                                box = await checkbox.bounding_box()
                                if box:
                                    target_x = box["x"] + (box["width"] / 2)
                                    target_y = box["y"] + (box["height"] / 2)
                                    await page.mouse.move(target_x, target_y, steps=15)
                                    await asyncio.sleep(0.5)
                                    await page.mouse.click(target_x, target_y)
                                    print(f"[FACTORY:{self.engine.upper()}] Verification coordinates clicked.")
                                    await asyncio.sleep(5)
                            break
                except Exception as e:
                    print(f"[FACTORY:{self.engine.upper()}] Anti-bot scan exception: {e}")

                # --- ACTIVE DOM NUKE ---
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
                await asyncio.sleep(2)

                input_element = page.locator(self.config['input_selector']).first
                await input_element.wait_for(state="attached", timeout=45000)
                await input_element.focus()
                await page.keyboard.insert_text(self.config['tiny_prompt'])
                await asyncio.sleep(1.0)
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)
                print(f"[FACTORY:{self.engine.upper()}] Tiny prompt accepted. Session trusted.")

        except Exception as e:
            error_msg = str(e)
            print(f"[FACTORY:{self.engine.upper()}] Error warming {self.engine}: {error_msg}")

            try: await self.db.execute("DELETE FROM browser_profiles WHERE id = $1", profile_id)
            except Exception: pass

            if any(sig in error_msg for sig in ["NS_ERROR_PROXY", "Timeout", "closed", "Connection refused"]):
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