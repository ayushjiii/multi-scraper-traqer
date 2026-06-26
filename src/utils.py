"""Shared helpers used by all engine agents.

Centralising these means a fix lands in one place instead of being copy-pasted
(and drifting) across perplexity_agent.py / gemini_agent.py / chatgpt_agent.py.
"""
import re


def parse_proxy(proxy_str: str) -> dict | None:
    """Parse a 'host:port:user:pass' proxy string into a Camoufox proxy dict.

    Returns None for an empty/malformed string. Callers MUST treat a None result
    on a non-empty proxy_string as a hard error and refuse to launch unproxied —
    launching with proxy=None leaks the real IP.
    """
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) != 4:
        return None
    host, port, user, pwd = parts
    if not (host and port and user and pwd):
        return None
    return {"server": f"http://{host}:{port}", "username": user, "password": pwd}


def safe_task_id(task_id) -> str:
    """Sanitise a task_id for safe use in filesystem paths.

    The task_id arrives from the Redis payload (an untrusted boundary). Without
    sanitising, a value like '../../etc/x' would escape the screenshots dir when
    used in os.path.join. We allow only [A-Za-z0-9_-] and cap the length.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(task_id))
    return cleaned[:64] or "task"
