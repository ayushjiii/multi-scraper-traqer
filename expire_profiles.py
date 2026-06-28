"""
Manual reset tool: mark AVAILABLE profiles EXPIRED so the factory re-warms them.

This ignores the TTL — it expires immediately. By default it resets ALL engines;
pass an engine name to scope it.

Usage:
    python expire_profiles.py            # expire AVAILABLE profiles for every engine
    python expire_profiles.py gemini     # expire only gemini's AVAILABLE profiles
"""
import asyncio
import sys
from src.database import DatabaseManager

ENGINES = ("chatgpt", "perplexity", "gemini", "aio")


async def run(engine: str | None):
    db = DatabaseManager()
    await db.connect()

    if engine:
        await db.execute(
            "UPDATE browser_profiles SET status = 'EXPIRED' "
            "WHERE status = 'AVAILABLE' AND engine_type = $1",
            engine,
        )
        scope = engine
    else:
        await db.execute("UPDATE browser_profiles SET status = 'EXPIRED' WHERE status = 'AVAILABLE'")
        scope = "ALL engines"

    remaining = await db.fetchval("SELECT COUNT(*) FROM browser_profiles WHERE status = 'AVAILABLE'")
    print(f"Expired AVAILABLE profiles for {scope}. AVAILABLE remaining: {remaining}")
    print("Factory will re-warm fresh profiles on next start.")
    await db.close()


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if arg and arg not in ENGINES:
        print(f"Unknown engine '{arg}'. Choose from: {', '.join(ENGINES)} (or omit for all).")
        sys.exit(1)
    asyncio.run(run(arg))
