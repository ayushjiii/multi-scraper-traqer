import asyncpg
from src.config import Config

class DatabaseManager:
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Initialize the high-performance asynchronous connection pool."""
        print("Connecting to PostgreSQL...")
        self.pool = await asyncpg.create_pool(
            dsn=Config.DB_DSN,
            min_size=1,
            # We add a buffer of 2 to our max workers for background cron jobs
            max_size=Config.MAX_CONCURRENT_WORKERS + 2 
        )
        print("Database connection pool established.")

    async def close(self):
        """Gracefully close the pool when the system shuts down."""
        if self.pool:
            await self.pool.close()
            print("Database connection pool closed.")
    
    async def execute(self, query: str, *args):
        """Execute a write-only query (INSERT, UPDATE, DELETE)."""
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def fetch(self, query: str, *args):
        """Fetch multiple rows (e.g., getting a list of expired profiles)."""
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)
            
    async def fetchrow(self, query: str, *args):
        """Fetch a single row (e.g., locking a specific profile for a worker)."""
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        """Fetch a single value (e.g., counting rows)."""
        async with self.pool.acquire() as connection:
            return await connection.fetchval(query, *args)