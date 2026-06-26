"""
Load / refresh the proxy pool in PostgreSQL.

Source priority:
    1. proxies.txt   (one "host:port:user:pass" per line — Webshare native format)
    2. PROXY_LIST    (comma-separated env var — legacy fallback)

Behaviour:
    - MERGES into the DB by default: new proxies are inserted, existing ones keep
      their status and per-engine ban flags (so a working proxy isn't reset).
    - Pass --replace to wipe the table first (TRUNCATE) and reload from scratch.

Usage:
    python load_proxies.py            # merge proxies.txt into the DB
    python load_proxies.py --replace  # wipe and reload
"""
import asyncio
import os
import sys
from src.database import DatabaseManager

PROXY_FILE = os.path.join(os.path.dirname(__file__), "proxies.txt")


def _valid(entry: str) -> bool:
    """A valid proxy is host:port:user:pass with a numeric port."""
    parts = entry.split(":")
    if len(parts) != 4:
        return False
    host, port, user, pwd = parts
    return bool(host) and port.isdigit() and bool(user) and bool(pwd)


def read_proxies() -> list[str]:
    """Read proxies from proxies.txt, falling back to the PROXY_LIST env var."""
    entries: list[str] = []

    if os.path.exists(PROXY_FILE):
        with open(PROXY_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                entries.append(line)
        print(f"[PROXIES] Read {len(entries)} lines from proxies.txt")
    else:
        raw = os.getenv("PROXY_LIST", "")
        if raw:
            entries = [p.strip() for p in raw.split(",") if p.strip()]
            print(f"[PROXIES] proxies.txt not found — read {len(entries)} from PROXY_LIST env")

    # Validate + dedupe (preserve order)
    seen = set()
    clean = []
    skipped = 0
    for e in entries:
        if not _valid(e):
            skipped += 1
            print(f"[PROXIES] Skipping malformed entry: {e!r}")
            continue
        if e in seen:
            continue
        seen.add(e)
        clean.append(e)

    print(f"[PROXIES] {len(clean)} valid unique proxies ({skipped} skipped)")
    return clean


async def load_proxies(replace: bool = False):
    db = DatabaseManager()
    await db.connect()

    proxies = read_proxies()
    if not proxies:
        print("[PROXIES] Nothing to load. Add entries to proxies.txt.")
        await db.close()
        return

    if replace:
        print("[PROXIES] --replace: purging existing proxy table...")
        await db.execute("TRUNCATE TABLE proxies CASCADE;")

    inserted = 0
    for p in proxies:
        # Insert new; on conflict (already known) leave its status/ban flags untouched.
        # asyncpg returns "INSERT 0 1" when a row was inserted, "INSERT 0 0" on conflict.
        result = await db.execute(
            "INSERT INTO proxies (connection_string) VALUES ($1) ON CONFLICT (connection_string) DO NOTHING",
            p,
        )
        if result == "INSERT 0 1":
            inserted += 1

    total = await db.fetchval("SELECT COUNT(*) FROM proxies")
    active = await db.fetchval("SELECT COUNT(*) FROM proxies WHERE status = 'ACTIVE'")
    print(f"[PROXIES] Inserted {inserted} new. Table now has {total} total ({active} ACTIVE).")
    await db.close()


if __name__ == "__main__":
    replace = "--replace" in sys.argv
    asyncio.run(load_proxies(replace=replace))
