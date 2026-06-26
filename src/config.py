import os
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

class Config:
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME", "traqer_db")
    DB_USER = os.getenv("DB_USER", "postgres")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")

    # URL-encode user/password so special chars (@ : / ? #) don't corrupt the DSN.
    DB_DSN = (
        f"postgresql://{quote(DB_USER, safe='')}:{quote(DB_PASSWORD, safe='')}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )

    REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
    REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)

    MAX_CONCURRENT_WORKERS = int(os.getenv("MAX_CONCURRENT_WORKERS", 2))
    PROFILE_TTL_MINUTES = int(os.getenv("PROFILE_TTL_MINUTES", 45))