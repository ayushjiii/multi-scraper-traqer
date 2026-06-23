"""
Traqer Dispatcher — drop a prompt, fan it out to all three engines.

Usage:
    # Interactive (prompts you to type):
    python dispatch.py

    # One-liner:
    python dispatch.py "What are the best programming languages in 2025?"

    # Multiple prompts from a file (one per line):
    python dispatch.py --file prompts.txt

Each prompt is pushed to task_queue:chatgpt, task_queue:perplexity, and
task_queue:gemini with a shared task_id root so results are easy to correlate.
Duplicate detection is handled inside each agent (checked against scrape_results
before execution), so re-dispatching an already-scraped prompt is safe.
"""
import asyncio
import json
import sys
import uuid
import argparse
import redis.asyncio as redis
from src.config import Config

ENGINES = ["chatgpt", "perplexity", "gemini"]


async def dispatch(prompts: list[str]):
    r = redis.Redis(
        host=Config.REDIS_HOST,
        port=Config.REDIS_PORT,
        password=Config.REDIS_PASSWORD,
        decode_responses=True,
    )

    try:
        await r.ping()
    except Exception as e:
        print(f"[DISPATCH] Redis connection failed: {e}")
        return

    for prompt in prompts:
        prompt = prompt.strip()
        if not prompt:
            continue

        # Shared root so you can correlate results across engines
        task_root = uuid.uuid4().hex[:12]

        pushed_to = []
        for engine in ENGINES:
            task_id = f"{task_root}_{engine}"
            payload = json.dumps({"task_id": task_id, "prompt": prompt})
            await r.lpush(f"task_queue:{engine}", payload)
            pushed_to.append(engine)

        print(f"[DISPATCH] [{task_root}] Pushed to: {', '.join(pushed_to)}")
        print(f"           Prompt: {prompt[:120]}{'...' if len(prompt) > 120 else ''}")

    await r.aclose()


async def queue_status(r):
    lines = []
    for engine in ENGINES:
        length = await r.llen(f"task_queue:{engine}")
        lines.append(f"{engine}: {length}")
    return "  |  ".join(lines)


async def interactive():
    r = redis.Redis(
        host=Config.REDIS_HOST,
        port=Config.REDIS_PORT,
        password=Config.REDIS_PASSWORD,
        decode_responses=True,
    )

    try:
        await r.ping()
    except Exception as e:
        print(f"[DISPATCH] Redis connection failed: {e}")
        return

    print("\n Traqer Dispatcher")
    print("=" * 50)
    print("Type a prompt and press Enter to dispatch to all engines.")
    print("Type 'status' to see queue lengths.")
    print("Type 'quit' or press Ctrl-C to exit.\n")

    try:
        while True:
            status = await queue_status(r)
            prompt = input(f"[{status}] > ").strip()

            if not prompt:
                continue
            if prompt.lower() in ("quit", "exit", "q"):
                break
            if prompt.lower() == "status":
                print(f"  Queue depths: {status}\n")
                continue

            task_root = uuid.uuid4().hex[:12]
            for engine in ENGINES:
                task_id = f"{task_root}_{engine}"
                payload = json.dumps({"task_id": task_id, "prompt": prompt})
                await r.lpush(f"task_queue:{engine}", payload)

            print(f"  Dispatched [{task_root}] to {', '.join(ENGINES)}\n")

    except (KeyboardInterrupt, EOFError):
        print("\n[DISPATCH] Exiting.")
    finally:
        await r.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# B2B SaaS Geo-Optimization Prompt Pack
# These mirror real queries buyers use when evaluating tools like Traqer.
# Run with: python dispatch.py --seed
# ─────────────────────────────────────────────────────────────────────────────
GEO_PROMPTS = [
    # Category: Tool discovery (high-intent buyer queries)
    "What is the best rank tracking tool for local SEO agencies managing multiple client locations?",
    "Which B2B SaaS tools help track keyword rankings by city and zip code?",
    "Best rank tracker for multi-location businesses — what do SEO professionals recommend?",
    "What software do enterprise SEO teams use to monitor local search visibility across regions?",
    "Top geo-targeted rank tracking platforms compared: features, pricing, and accuracy",

    # Category: Use-case fit (Traqer's core value prop)
    "How do I track Google rankings for different cities without using multiple accounts?",
    "Can I monitor local pack rankings and organic rankings together in one SEO tool?",
    "What is the most accurate tool for tracking Google Maps rankings for B2B companies?",
    "How do SaaS companies monitor their visibility in local search results across different markets?",
    "Best way to track keyword rankings by country, state, and city simultaneously",

    # Category: Competitive intelligence (how buyers compare options)
    "BrightLocal vs Whitespark vs SEMrush local rank tracking — which is most accurate in 2025?",
    "Does Ahrefs support hyper-local rank tracking by zip code or neighborhood?",
    "What are the limitations of Google Search Console for local SEO rank tracking?",
    "Is there a rank tracker that shows local pack position AND organic position for the same keyword?",
    "What rank tracking tools support Google Business Profile performance monitoring?",

    # Category: Agency and reseller use cases
    "Best white-label rank tracking software for digital marketing agencies in 2025",
    "Which local SEO rank trackers offer client reporting dashboards and white-label exports?",
    "How do SEO agencies track hundreds of local keyword rankings across dozens of client locations?",
    "What is the most scalable rank tracking solution for agencies with 50+ local clients?",
    "Rank tracking tools with API access for custom reporting — what do large SEO agencies use?",

    # Category: AI-driven geo optimization (emerging queries)
    "How is AI changing local SEO rank tracking and geo-targeted visibility monitoring?",
    "What tools use AI to predict local search ranking changes for B2B SaaS companies?",
    "Which SEO platforms provide AI-generated insights for improving local search rankings?",
    "How do B2B SaaS companies optimize for AI search results and answer engines like Perplexity?",
    "What is GEO optimization and how does it differ from traditional local SEO for SaaS companies?",
]


def main():
    parser = argparse.ArgumentParser(description="Dispatch prompts to all Traqer agents.")
    parser.add_argument("prompt", nargs="?", help="Prompt string to dispatch")
    parser.add_argument("--file", "-f", help="File with one prompt per line")
    parser.add_argument("--seed", action="store_true", help="Dispatch the built-in B2B geo-optimization prompt pack")
    args = parser.parse_args()

    if args.seed:
        print(f"[DISPATCH] Seeding {len(GEO_PROMPTS)} B2B geo-optimization prompts...")
        asyncio.run(dispatch(GEO_PROMPTS))

    elif args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            prompts = [line.strip() for line in fh if line.strip()]
        print(f"[DISPATCH] Loaded {len(prompts)} prompt(s) from {args.file}")
        asyncio.run(dispatch(prompts))

    elif args.prompt:
        asyncio.run(dispatch([args.prompt]))

    else:
        # No args — drop into interactive REPL
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(interactive())
        loop.close()


if __name__ == "__main__":
    main()
