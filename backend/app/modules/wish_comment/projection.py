"""Projection: comment floor counts derived from the comments table.

Every surface (detail thread, wall card, my-claims note) MUST source its
number from counts() so the three views can never disagree.
"""


def counts(c, wish_ids: list[int] | None = None, claimer: str | None = None) -> dict[int, int]:
    """Map wish_id -> floor count. One GROUP BY query per call.

    - claimer given: wishes currently held by that claimer (JOIN wishes)
    - wish_ids given: restrict to those wishes
    - neither: all wishes
    """
    sql = "SELECT cm.wish_id AS wid, COUNT(*) AS n FROM comments cm"
    where, params = [], []
    if claimer is not None:
        sql += " JOIN wishes w ON w.id = cm.wish_id"
        where.append("w.claimer = ?")
        params.append(claimer)
    if wish_ids is not None:
        if not wish_ids:
            return {}
        placeholders = ",".join("?" for _ in wish_ids)
        where.append(f"cm.wish_id IN ({placeholders})")
        params.extend(wish_ids)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY cm.wish_id"
    return {r["wid"]: r["n"] for r in c.execute(sql, params)}


def merge(rows: list[dict], counts_map: dict[int, int]) -> list[dict]:
    """Pure backfill: annotate each wish row with comment_count (0 if absent).

    The number is ALWAYS the real floor count from counts() — wall card,
    detail header and my-claims note must never disagree, so no status-based
    adjustment may happen here.
    """
    for row in rows:
        row["comment_count"] = counts_map.get(row["id"], 0)
    return rows
