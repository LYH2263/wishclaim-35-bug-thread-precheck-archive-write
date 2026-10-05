"""wish_comment facade: the only surface main.py imports.

Combines the gate (pure policy), storage (rows) and projection (counts).
Writes run inside BEGIN IMMEDIATE so that gate decision, TTL lock release
and floor assignment are one atomic step.
"""
from datetime import datetime

from app.engines.claim_lock import release_if_expired
from app.modules.wish_comment import projection, storage
from app.modules.wish_comment.gate import validate_text, write_gate

init_schema = storage.init_schema

# Reasons that carry an explicit HTTP status.
_STATUS = {
    "not_found": 404,
    "empty_content": 422,
    "too_long": 422,
    "empty_author": 422,
    "frozen": 409,
    "archived": 409,
    "bad_status": 400,
}


class CommentError(Exception):
    def __init__(self, reason: str, status_code: int | None = None):
        self.reason = reason
        self.status_code = status_code or _STATUS.get(reason, 400)
        super().__init__(reason)


def _wish(c, wid: int):
    return c.execute(
        "SELECT id, status, expires_at FROM wishes WHERE id=?", (wid,)
    ).fetchone()


def preview(c, wid: int, content, author, now: datetime, max_length: int) -> dict:
    """Pre-flight check only. Never writes. Verdict is always 200-shaped;
    a missing wish is the only error."""
    w = _wish(c, wid)
    if not w:
        raise CommentError("not_found")
    v = validate_text(content, author, max_length)
    g = write_gate(w["status"], w["expires_at"], now)
    # The verdict must match what add_comment would do: text first, then gate.
    # A frozen/archived thread fails the precheck instead of passing it and
    # dying at submit time.
    ok = v["ok"] and g["writable"]
    reason = v["reason"] if not v["ok"] else ("" if g["writable"] else g["reason"])
    return {"ok": ok, "reason": reason, "state": g["state"],
            "length": v["length"], "max_length": max_length}


def get_thread(c, wid: int, now: datetime, max_length: int) -> dict:
    """Full read payload for the detail page. History is always readable,
    even when frozen or archived."""
    w = _wish(c, wid)
    if not w:
        raise CommentError("not_found")
    g = write_gate(w["status"], w["expires_at"], now)
    comments = storage.list_comments(c, wid)
    return {"wish_id": wid, "state": g["state"],
            "comment_count": projection.counts(c, wish_ids=[wid]).get(wid, 0),
            "max_length": max_length, "comments": comments}


def add_comment(c, wid: int, content, author, now: datetime, max_length: int) -> dict:
    """Validate then append a floor under a write lock.

    An expired claim is released inside the same transaction before the
    insert, so a comment never lands on a row still stamped 'claimed'.
    """
    # Missing resource is a 404 even when the body is also invalid.
    if not _wish(c, wid):
        raise CommentError("not_found")
    # Fail fast on text before taking the write lock.
    v = validate_text(content, author, max_length)
    if not v["ok"]:
        raise CommentError(v["reason"])

    try:
        c.execute("BEGIN IMMEDIATE")
        w = _wish(c, wid)
        if not w:
            raise CommentError("not_found")
        g = write_gate(w["status"], w["expires_at"], now)
        # Enforced under the write lock, so a claim landing between preview
        # and submit turns this into a 409 instead of a phantom floor.
        if not g["writable"]:
            raise CommentError(g["reason"])
        if g["reason"] == "ttl_expired":
            rel = release_if_expired(w["status"], w["expires_at"], now)
            c.execute(
                "UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=?"
                " WHERE id=?",
                (rel["status"], rel["claimer"], rel["claimed_at"],
                 rel["expires_at"], wid),
            )
        row = storage.insert_comment(c, wid, v["author"], v["content"], now)
        c.commit()
        return row
    except Exception:
        c.rollback()
        raise
