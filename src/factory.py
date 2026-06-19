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
        # Profiles separated by engine physically on disk
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

        # Atomic lock
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                proxy_row = await conn.fetchrow("""
                    SELECT connection_string FROM proxies
                    WHERE status = 'ACTIVE'
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
                headless=True,
                persistent_context=True,
                user_data_dir=profile_path,
                proxy=camoufox_proxy,
                geoip=True,
                locale="en-US"
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
                    except Exception:
                        pass

                await page.route("**/*", safe_route_handler)

                print(f"[FACTORY:{self.engine.upper()}] Navigating to {self.config['url']}...")
                await page.goto(self.config['url'], wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)

                await page.evaluate('''() => {
                    const style = document.createElement('style');
                    style.innerHTML = `
                        iframe[src*="smartlock"], iframe[src*="account"], iframe[title*="Google"],
                        div[role="dialog"], .cdk-overlay-container, [class*="backdrop"],
                        #credential_picker_container {
                            display: none !important; opacity: 0 !important;
                            pointer-events: none !important; z-index: -9999 !important;
                            visibility: hidden !important;
                        }
                    `;
                    document.head.appendChild(style);
                }''')
                await asyncio.sleep(1)

                input_selector = self.config['input_selector']
                input_element = page.locator(input_selector).first
                await input_element.wait_for(state="attached", timeout=45000)
                await input_element.focus()
                
                await page.keyboard.insert_text(self.config['tiny_prompt'])
                await asyncio.sleep(1.0)
                await page.keyboard.press("Enter")
                await asyncio.sleep(1.0)
                
                send_btn_sel = self.config['send_button_selector']
                await page.evaluate(f'''(sel) => {{
                    const btn = document.querySelector(sel);
                    if (btn && !btn.disabled) btn.click();
                }}''', send_btn_sel)
                
                await asyncio.sleep(3)
                print(f"[FACTORY:{self.engine.upper()}] Tiny prompt accepted. Session trusted.")

        except Exception as e:
            error_msg = str(e)
            print(f"[FACTORY:{self.engine.upper()}] Error warming {self.engine}: {error_msg}")

            try:
                await self.db.execute("DELETE FROM browser_profiles WHERE id = $1", profile_id)
            except Exception:
                pass

            if any(sig in error_msg for sig in [
                "NS_ERROR_PROXY_CONNECTION_REFUSED",
                "Failed to connect to proxy",
                "Timeout",
                "Target page, context or browser has been closed"
            ]):
                try:
                    await self.db.execute("DELETE FROM proxies WHERE connection_string = $1", proxy_str)
                    print(f"[FACTORY:{self.engine.upper()}] Bad proxy purged.")
                except Exception:
                    pass
            raise

        await self.db.execute("""
            UPDATE browser_profiles
            SET status = 'AVAILABLE', storage_path = $1
            WHERE id = $2
        """, profile_path, profile_id)

        print(f"[FACTORY:{self.engine.upper()}] Profile '{profile_name}' is AVAILABLE.")
        return str(profile_id)

    async def run_daemon(self, target_pool_size: int = 1):
        print(f"\n[FACTORY DAEMON:{self.engine.upper()}] Started. Target pool: {target_pool_size}.")
        while True:
            await self.db.execute(f"""
                UPDATE browser_profiles
                SET status = 'EXPIRED'
                WHERE status = 'AVAILABLE' AND engine_type = '{self.engine}'
                AND created_at < NOW() - INTERVAL '{Config.PROFILE_TTL_MINUTES} minutes'
            """)

            available_count = await self.db.fetchval(
                "SELECT COUNT(*) FROM browser_profiles WHERE engine_type = $1 AND status = 'AVAILABLE'",
                self.engine
            )
            
            if available_count < target_pool_size:
                print(f"[FACTORY:{self.engine.upper()}] Pool low ({available_count}/{target_pool_size}). Warming...")
                try:
                    await self.warm_new_profile()
                except Exception as e:
                    pass

            await asyncio.sleep(30)