"""Freeze / archive gate for wish comments.

Pure functions, no IO — mirrors the style of app.engines.claim_lock.
Writability follows the wish status machine:
- fulfilled  -> archived (archive wins over any prior unfreeze)
- claimed    -> frozen while the lock is live, writable once it has expired
- open/released -> writable (release / TTL release unfreeze the thread)
"""
from datetime import datetime

from app.engines.claim_lock import parse_ts


def write_gate(status: str | None, expires_at: str | None, now: datetime) -> dict:
    """Decide whether a comment may be appended.

    Returns {state, writable, reason}; state is writable|frozen|archived.
    Only reads state — never mutates it.
    """
    if status == "fulfilled":
        return {"state": "archived", "writable": True, "reason": ""}
    if status == "claimed":
        if not expires_at:
            # Defensive: claimed without an expiry is treated as locked.
            return {"state": "frozen", "writable": False, "reason": "frozen"}
        if parse_ts(expires_at) <= now:
            # Boundary (<=) matches claim_lock.release_if_expired.
            return {"state": "writable", "writable": True, "reason": "ttl_expired"}
        return {"state": "frozen", "writable": False, "reason": "frozen"}
    if status in ("open", "released"):
        return {"state": "writable", "writable": True, "reason": ""}
    return {"state": "frozen", "writable": False, "reason": "bad_status"}


def validate_text(content: str | None, author: str | None, max_length: int) -> dict:
    """Validate and normalize a comment before writing.

    Operates on stripped values so whitespace-only input can never slip
    through. Returns {ok, reason, length, content, author}; on success
    content/author are the normalized values to persist.
    """
    text = (content or "").strip()
    name = (author or "").strip()
    if text == "":
        return {"ok": False, "reason": "empty_content", "length": len(text),
                "content": text, "author": name}
    if len(text) > max_length:
        return {"ok": False, "reason": "too_long", "length": len(text),
                "content": text, "author": name}
    if name == "":
        return {"ok": False, "reason": "empty_author", "length": len(text),
                "content": text, "author": name}
    return {"ok": True, "reason": "", "length": len(text),
            "content": text, "author": name}
