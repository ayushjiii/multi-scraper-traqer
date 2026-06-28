"""
Clear pending tasks from the Redis queues.

Usage:
    python clear_queues.py                # show queue depths, then clear ALL three
    python clear_queues.py perplexity     # clear only one engine's queue
    python clear_queues.py --status       # just show depths, clear nothing
"""
import asyncio
import sys
import redis.asyncio as redis
from src.config import Config

ENGINES = ["chatgpt", "perplexity", "gemini", "aio"]


async def main():
    r = redis.Redis(
        host=Config.REDIS_HOST,
        port=Config.REDIS_PORT,
        password=Config.REDIS_PASSWORD,
        decode_responses=True,
    )
    try:
        await r.ping()
    except Exception as e:
        print(f"[CLEAR] Redis connection failed: {e}")
        return

    # Show current depths
    print("[CLEAR] Current queue depths:")
    for e in ENGINES:
        n = await r.llen(f"task_queue:{e}")
        print(f"  task_queue:{e}: {n}")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if "--status" in sys.argv:
        await r.aclose()
        return

    targets = args if args else ENGINES
    print(f"\n[CLEAR] Clearing: {', '.join(targets)}")
    for e in targets:
        if e not in ENGINES:
            print(f"  unknown engine '{e}' — skipping")
            continue
        removed = await r.delete(f"task_queue:{e}")
        print(f"  task_queue:{e}: cleared")

    print("\n[CLEAR] Done. New depths:")
    for e in ENGINES:
        n = await r.llen(f"task_queue:{e}")
        print(f"  task_queue:{e}: {n}")

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
