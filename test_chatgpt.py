"""
Quick smoke test for ChatGPTWorker — no DB, no Redis, no proxy required.
Runs the browser visibly so you can watch what happens.

Usage:
    .venv/Scripts/activate
    python test_chatgpt.py
"""
import asyncio
import os
import sys

# Show the browser window
os.environ["DEBUG_HEADLESS"] = "0"

from chatgpt_agent import ChatGPTWorker


async def main():
    # Use a throwaway local profile dir so nothing bleeds into production profiles
    profile_path = os.path.join(os.getcwd(), "profiles", "chatgpt", "_test_profile")
    os.makedirs(profile_path, exist_ok=True)

    worker = ChatGPTWorker(profile_path=profile_path, proxy_string=None)

    print("\n[TEST] Launching ChatGPTWorker with no proxy...")
    print(f"[TEST] URL         : {worker.url}")
    print(f"[TEST] Input sel   : {worker.input_selector}")
    print(f"[TEST] Response sel: {worker.response_selector}")
    print(f"[TEST] Send btn    : {worker.send_btn_selector}")
    print(f"[TEST] Stop btn    : {worker.stop_btn_selector}\n")

    try:
        result = await worker.execute_task(
            prompt="What is 2 + 2? Answer in one sentence.",
            task_id="test_001"
        )
        print("\n[TEST] SUCCESS")
        print(f"[TEST] Response  : {result['ai_response'][:300]}")
        print(f"[TEST] Sources   : {result['sources'][:3]}")
        print(f"[TEST] Screenshot: {result['screenshot_path']}")

    except Exception as e:
        print(f"\n[TEST] FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
