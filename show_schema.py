## run this file to get the database schema

import asyncio
from src.database import DatabaseManager

async def show():
    db = DatabaseManager()
    await db.connect()
    rows = await db.fetch("""
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """)
    for r in rows:
        print(f"{r['table_name']}.{r['column_name']} | {r['data_type']} | nullable={r['is_nullable']} | default={r['column_default']}")
    await db.close()

asyncio.run(show())
