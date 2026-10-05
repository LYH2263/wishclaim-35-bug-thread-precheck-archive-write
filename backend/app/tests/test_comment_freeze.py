"""Freeze/archive alignment tests for the comment thread.

Locks in the decisions:
- open/released threads stay appendable (the alignment must not freeze them)
- a live claim freezes: preview fails, submit 409s, no row, no count bump
- fulfilled archives permanently: archive outranks every unfreeze path
- wall card / detail / my-claims counts always equal the real floor count
- a failed precheck or rejected submit leaves no empty row behind
- concurrent appends serialize: floors and counts never tear

Runs under pytest, or standalone: python3 app/tests/test_comment_freeze.py
"""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import seed
from app.db import connect
from app.engines.claim_lock import release_if_expired
from app.modules import wish_comment
from app.modules.wish_comment import projection
from app.modules.wish_comment.gate import write_gate

NOW = datetime.now(timezone.utc)
MAX_LEN = 500


def fresh_db(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    seed.init_db()


def make_wish(c, status="open", claimer=None, expires_at=None, title="w"):
    claimed_at = NOW.isoformat() if status == "claimed" else None
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
        " VALUES (?,?,?,?,?,?,?)",
        (title, "", status, claimer, claimed_at, expires_at, "clean"),
    )
    c.commit()
    return cur.lastrowid


def live_claim(c, claimer="ghost"):
    exp = (NOW + timedelta(hours=1)).isoformat()
    return make_wish(c, status="claimed", claimer=claimer, expires_at=exp)


def surface_counts(c, wid, claimer=None):
    """The three user-visible numbers for one wish."""
    wall = projection.counts(c).get(wid, 0)
    detail = wish_comment.get_thread(c, wid, NOW, MAX_LEN)["comment_count"]
    mine = projection.counts(c, claimer=claimer).get(wid, 0) if claimer else wall
    rows = c.execute("SELECT COUNT(*) n FROM comments WHERE wish_id=?", (wid,)).fetchone()["n"]
    return {"wall": wall, "detail": detail, "mine": mine, "actual": rows}


# ---------- gate units ----------

def test_gate_archive_is_permanent(tmp_path=None):
    # Archive never writable, whatever the lock fields say — it outranks
    # any unfreeze because the gate keys on status alone.
    for exp in (None, (NOW - timedelta(hours=1)).isoformat(),
                (NOW + timedelta(hours=1)).isoformat()):
        g = write_gate("fulfilled", exp, NOW)
        assert g == {"state": "archived", "writable": False, "reason": "archived"}
    # The TTL sweeper must never reopen an archived wish.
    assert release_if_expired("fulfilled", (NOW - timedelta(hours=1)).isoformat(), NOW) is None


def test_gate_states(tmp_path=None):
    future = (NOW + timedelta(hours=1)).isoformat()
    past = (NOW - timedelta(hours=1)).isoformat()
    assert write_gate("open", None, NOW)["writable"] is True
    assert write_gate("released", None, NOW)["writable"] is True
    assert write_gate("claimed", future, NOW)["writable"] is False
    assert write_gate("claimed", None, NOW)["writable"] is False
    assert write_gate("claimed", past, NOW) == {
        "state": "writable", "writable": True, "reason": "ttl_expired"}
    assert write_gate("bogus", None, NOW)["writable"] is False


# ---------- live claim freezes ----------

def test_claimed_precheck_fails_and_adds_no_row(tmp_path):
    fresh_db(tmp_path)
    c = connect()
    wid = live_claim(c)

    v = wish_comment.preview(c, wid, "你好", "访客", NOW, MAX_LEN)
    assert v["ok"] is False and v["reason"] == "frozen" and v["state"] == "frozen"

    try:
        wish_comment.add_comment(c, wid, "你好", "访客", NOW, MAX_LEN)
        raise AssertionError("append on a frozen thread must raise")
    except wish_comment.CommentError as e:
        assert e.reason == "frozen" and e.status_code == 409

    counts = surface_counts(c, wid, claimer="ghost")
    assert counts == {"wall": 0, "detail": 0, "mine": 0, "actual": 0}
    c.close()


def test_preview_pass_then_claim_lands_submit_fails_clean(tmp_path):
    """Preview ok on an open thread, a claim lands, submit must fail and
    no surface may show +1."""
    fresh_db(tmp_path)
    c = connect()
    wid = make_wish(c)

    v = wish_comment.preview(c, wid, "你好", "访客", NOW, MAX_LEN)
    assert v["ok"] is True

    exp = (NOW + timedelta(hours=1)).isoformat()
    c.execute("UPDATE wishes SET status='claimed', claimer='ghost', expires_at=? WHERE id=?",
              (exp, wid))
    c.commit()

    try:
        wish_comment.add_comment(c, wid, "你好", "访客", NOW, MAX_LEN)
        raise AssertionError("submit after the freeze must raise")
    except wish_comment.CommentError as e:
        assert e.reason == "frozen"

    counts = surface_counts(c, wid, claimer="ghost")
    assert counts == {"wall": 0, "detail": 0, "mine": 0, "actual": 0}
    c.close()


# ---------- archive ----------

def test_archive_readonly_and_unfreeze_cannot_reopen(tmp_path):
    fresh_db(tmp_path)
    c = connect()
    wid = live_claim(c)
    # Append history before archiving is impossible (frozen), so seed one
    # floor while still open, then claim -> fulfill.
    wid2 = make_wish(c)
    wish_comment.add_comment(c, wid2, "一楼", "访客", NOW, MAX_LEN)
    exp = (NOW + timedelta(hours=1)).isoformat()
    c.execute("UPDATE wishes SET status='claimed', claimer='ghost', expires_at=? WHERE id=?",
              (exp, wid2))
    c.execute("UPDATE wishes SET status='fulfilled' WHERE id=?", (wid2,))
    c.commit()

    # Append API rejects with archived.
    try:
        wish_comment.add_comment(c, wid2, "二楼", "访客", NOW, MAX_LEN)
        raise AssertionError("append on an archived thread must raise")
    except wish_comment.CommentError as e:
        assert e.reason == "archived" and e.status_code == 409

    # "Unfreeze" attempts cannot reopen it: the release endpoint only accepts
    # status='claimed', and the TTL sweeper ignores fulfilled rows.
    assert release_if_expired("fulfilled", exp, NOW) is None
    g = write_gate("fulfilled", exp, NOW)
    assert g["writable"] is False

    # History stays readable, count intact on every surface.
    thread = wish_comment.get_thread(c, wid2, NOW, MAX_LEN)
    assert thread["state"] == "archived" and len(thread["comments"]) == 1
    counts = surface_counts(c, wid2, claimer="ghost")
    assert counts == {"wall": 1, "detail": 1, "mine": 1, "actual": 1}
    c.close()


# ---------- counts agree ----------

def test_counts_agree_across_surfaces_no_phantom(tmp_path):
    fresh_db(tmp_path)
    c = connect()
    wid_open = make_wish(c)
    wish_comment.add_comment(c, wid_open, "一", "a", NOW, MAX_LEN)
    wish_comment.add_comment(c, wid_open, "二", "b", NOW, MAX_LEN)

    wid_claimed = live_claim(c, claimer="ghost")
    # Two real floors written before the claim (bypass the gate on purpose,
    # straight through storage, to set up the claimed-with-history state).
    from app.modules.wish_comment import storage
    c.execute("BEGIN IMMEDIATE")
    storage.insert_comment(c, wid_claimed, "a", "x", NOW)
    storage.insert_comment(c, wid_claimed, "b", "y", NOW)
    c.commit()

    # merge() must report the real count for claimed wishes — no phantom +1.
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE id IN (?,?)",
                                       (wid_open, wid_claimed))]
    projection.merge(rows, projection.counts(c))
    by_id = {r["id"]: r["comment_count"] for r in rows}
    assert by_id == {wid_open: 2, wid_claimed: 2}

    assert surface_counts(c, wid_open) == {"wall": 2, "detail": 2, "mine": 2, "actual": 2}
    assert surface_counts(c, wid_claimed, claimer="ghost") == {
        "wall": 2, "detail": 2, "mine": 2, "actual": 2}
    c.close()


# ---------- dirty input ----------

def test_dirty_precheck_leaves_no_empty_row(tmp_path):
    fresh_db(tmp_path)
    c = connect()
    wid = make_wish(c)
    for content, author, reason in [
        ("", "访客", "empty_content"),
        ("   \n\t ", "访客", "empty_content"),
        ("x" * (MAX_LEN + 1), "访客", "too_long"),
        ("你好", "", "empty_author"),
        ("你好", "   ", "empty_author"),
    ]:
        v = wish_comment.preview(c, wid, content, author, NOW, MAX_LEN)
        assert v["ok"] is False and v["reason"] == reason
        try:
            wish_comment.add_comment(c, wid, content, author, NOW, MAX_LEN)
            raise AssertionError(f"dirty input {reason} must be rejected")
        except wish_comment.CommentError as e:
            assert e.reason == reason
    rows = c.execute("SELECT * FROM comments WHERE wish_id=?", (wid,)).fetchall()
    assert rows == [], "rejected prechecks must not leave empty rows"
    assert surface_counts(c, wid)["actual"] == 0
    c.close()


# ---------- open thread stays open ----------

def test_open_thread_stays_appendable(tmp_path):
    fresh_db(tmp_path)
    c = connect()
    wid = make_wish(c)
    v = wish_comment.preview(c, wid, "你好", "访客", NOW, MAX_LEN)
    assert v["ok"] is True and v["state"] == "writable"
    row = wish_comment.add_comment(c, wid, "你好", "访客", NOW, MAX_LEN)
    assert row["floor"] == 1
    assert surface_counts(c, wid) == {"wall": 1, "detail": 1, "mine": 1, "actual": 1}
    c.close()


def test_expired_lock_releases_and_appends_atomically(tmp_path):
    fresh_db(tmp_path)
    c = connect()
    past = (NOW - timedelta(hours=1)).isoformat()
    wid = make_wish(c, status="claimed", claimer="ghost", expires_at=past)
    row = wish_comment.add_comment(c, wid, "超时后可追加", "访客", NOW, MAX_LEN)
    assert row["floor"] == 1
    w = c.execute("SELECT status, claimer FROM wishes WHERE id=?", (wid,)).fetchone()
    assert w["status"] == "open" and w["claimer"] is None
    c.close()


# ---------- concurrency ----------

def test_concurrent_appends_never_tear_counts(tmp_path):
    fresh_db(tmp_path)
    wid = make_wish(connect())
    barrier = threading.Barrier(2)
    results, errors = [], []

    def append(tag):
        try:
            barrier.wait(timeout=10)
            cc = connect()
            results.append(wish_comment.add_comment(cc, wid, "楼" + tag, "访客" + tag, NOW, MAX_LEN))
            cc.close()
        except Exception as e:  # a clean rejection is acceptable; a tear is not
            errors.append(e)

    threads = [threading.Thread(target=append, args=(str(i),)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    c = connect()
    rows = c.execute("SELECT floor FROM comments WHERE wish_id=? ORDER BY floor", (wid,)).fetchall()
    floors = [r["floor"] for r in rows]
    # Whatever the interleaving: committed floors are unique and gapless...
    assert floors == list(range(1, len(floors) + 1))
    # ...and every surface reports exactly the real number of rows.
    counts = surface_counts(c, wid)
    assert counts["wall"] == counts["detail"] == counts["mine"] == counts["actual"]
    # Both appends were valid for an open thread, so both land.
    assert len(results) == 2 and not errors and len(floors) == 2
    c.close()


if __name__ == "__main__":
    import tempfile
    import traceback

    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        tmp = tempfile.mkdtemp()
        try:
            if fn.__code__.co_argcount == 0:
                fn()
            else:
                import pathlib
                fn(pathlib.Path(tmp))
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
