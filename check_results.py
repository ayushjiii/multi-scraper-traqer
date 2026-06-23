import asyncio
from src.database import DatabaseManager

async def run():
    db = DatabaseManager()
    await db.connect()
    rows = await db.fetch("""
        SELECT task_id, engine_name,
               LEFT(input_prompt, 80) as prompt,
               LEFT(ai_response, 80) as response,
               jsonb_array_length(sources) as source_count,
               sources,
               screenshot_path,
               executed_at
        FROM scrape_results
        ORDER BY executed_at DESC
        LIMIT 20
    """)
    for r in rows:
        print(f"[{r['engine_name']}] {r['task_id']}")
        print(f"  sources : {r['source_count']}")
        print(f"  sources : {r['sources']}")
        print(f"  prompt  : {r['prompt']}")
        print(f"  response: {r['response']}")
        print(f"  shot    : {r['screenshot_path']}")
        print()
    await db.close()

asyncio.run(run())
