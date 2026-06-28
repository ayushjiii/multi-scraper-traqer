## quick health check — shows proxy count and available profiles per engine

import asyncio
from src.database import DatabaseManager

async def check():
    db = DatabaseManager()
    await db.connect()

    proxies = await db.fetchval("SELECT COUNT(*) FROM proxies WHERE status = 'ACTIVE'")
    print(f"Active proxies : {proxies}")

    for engine in ("chatgpt", "perplexity", "gemini", "aio"):
        col = f"{engine}_banned"
        unbanned = await db.fetchval(f"SELECT COUNT(*) FROM proxies WHERE status = 'ACTIVE' AND {col} = FALSE")
        print(f"  {engine}_banned=FALSE : {unbanned}")

    print()
    for status in ("AVAILABLE", "BUSY", "EXPIRED"):
        for engine in ("chatgpt", "perplexity", "gemini", "aio"):
            count = await db.fetchval(
                "SELECT COUNT(*) FROM browser_profiles WHERE engine_type = $1 AND status = $2",
                engine, status
            )
            if count:
                print(f"Profile {engine} {status}: {count}")

    results = await db.fetchval("SELECT COUNT(*) FROM scrape_results")
    print(f"\nScrape results total: {results}")

    await db.close()

asyncio.run(check())
