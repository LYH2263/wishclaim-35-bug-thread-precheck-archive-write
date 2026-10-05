"""Comment row storage: schema + floor-sequenced inserts/reads."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wish_id INTEGER NOT NULL REFERENCES wishes(id),
  floor INTEGER NOT NULL,
  author TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(wish_id, floor)
);
CREATE INDEX IF NOT EXISTS idx_comments_wish ON comments(wish_id);
"""


def init_schema(c) -> None:
    c.executescript(_SCHEMA)


def insert_comment(c, wish_id: int, author: str, content: str, now) -> dict:
    """Append a floor. Caller MUST already hold a BEGIN IMMEDIATE transaction
    so MAX(floor) reads serialize and UNIQUE(wish_id, floor) cannot race."""
    floor = c.execute(
        "SELECT COALESCE(MAX(floor), 0) + 1 FROM comments WHERE wish_id=?",
        (wish_id,),
    ).fetchone()[0]
    created = now.isoformat() if hasattr(now, "isoformat") else str(now)
    cur = c.execute(
        "INSERT INTO comments(wish_id, floor, author, content, created_at)"
        " VALUES (?,?,?,?,?)",
        (wish_id, floor, author, content, created),
    )
    return {"id": cur.lastrowid, "wish_id": wish_id, "floor": floor,
            "author": author, "content": content, "created_at": created}


def list_comments(c, wish_id: int) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT id, wish_id, floor, author, content, created_at"
        " FROM comments WHERE wish_id=? ORDER BY floor ASC",
        (wish_id,),
    )]
