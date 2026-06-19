import asyncio
import os
from src.database import DatabaseManager


async def load_new_proxies():
    db = DatabaseManager()
    await db.connect()

    print("Purging old proxies...")
    await db.execute("TRUNCATE TABLE proxies CASCADE;")

    raw = os.getenv("PROXY_LIST", "")
    if not raw:
        print("ERROR: PROXY_LIST env var is empty. Set it in your .env file.")
        await db.close()
        return

    new_proxies = [p.strip() for p in raw.split(",") if p.strip()]

    for p in new_proxies:
        await db.execute(
            "INSERT INTO proxies (connection_string) VALUES ($1) ON CONFLICT DO NOTHING", p
        )

    print(f"Loaded {len(new_proxies)} proxies.")
    await db.close()


if __name__ == "__main__":
    asyncio.run(load_new_proxies())