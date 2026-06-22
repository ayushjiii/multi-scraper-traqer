import asyncio
import sys
import json
import redis.asyncio as redis
from src.config import Config
from src.database import DatabaseManager
from src.orchestrator import Orchestrator
from src.factory import ProfileFactory

ENGINE = "gemini"

async def main():
    print(f"\n[SYSTEM] Starting {ENGINE.upper()} Agent Microservice...")

    db_manager = DatabaseManager()
    await db_manager.connect()

    print(f"[{ENGINE.upper()}] Resetting stale profiles...")
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
        print(f"[{ENGINE.upper()}] Redis connection failed: {e}")
        return

    factory = ProfileFactory(db_manager, engine=ENGINE)
    orchestrator = Orchestrator(db_manager, engine=ENGINE)

    factory_task = asyncio.create_task(factory.run_daemon(target_pool_size=1))
    concurrency_limit = max(1, Config.MAX_CONCURRENT_WORKERS // 3)
    semaphore = asyncio.Semaphore(concurrency_limit)
    queue_name = f"task_queue:{ENGINE}"

    async def process_task(task_data):
        async with semaphore:
            try:
                success = await orchestrator.process_task(task_data)
                if not success:
                    print(f"[{ENGINE.upper()}] Task {task_data.get('task_id')} failed — requeuing.")
                    await r.lpush(queue_name, json.dumps(task_data))
            except Exception as e:
                print(f"[{ENGINE.upper()}] CRITICAL: {e}")
                await asyncio.sleep(5)
                await r.lpush(queue_name, json.dumps(task_data))

    print(f"\n[{ENGINE.upper()}] Dispatcher online. Listening on {queue_name}...")
    try:
        while True:
            dispatched = False
            if semaphore._value > 0 and await r.llen(queue_name) > 0:
                avail = await db_manager.fetchval(
                    "SELECT COUNT(*) FROM browser_profiles WHERE engine_type = $1 AND status = 'AVAILABLE'", ENGINE
                )
                if avail > 0:
                    raw_task = await r.rpop(queue_name)
                    if raw_task:
                        asyncio.create_task(process_task(json.loads(raw_task)))
                        dispatched = True
            if not dispatched:
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
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
            phrase in str(exc) for phrase in [
                "Event loop is closed", "I/O operation on closed pipe"
            ]
        ):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_silence_cleanup_noise)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print(f"\n[{ENGINE.upper()}] Terminated.")
    finally:
        loop.close()
