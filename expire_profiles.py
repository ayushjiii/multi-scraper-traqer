import asyncio
from src.database import DatabaseManager

async def run():
    db = DatabaseManager()
    await db.connect()
    await db.execute("UPDATE browser_profiles SET status = 'EXPIRED' WHERE status = 'AVAILABLE'")
    remaining = await db.fetchval("SELECT COUNT(*) FROM browser_profiles WHERE status = 'AVAILABLE'")
    print(f"Done. AVAILABLE profiles remaining: {remaining}")
    print("Factory will re-warm fresh profiles with en-US locale on next start.")
    await db.close()

asyncio.run(run())
