"""Acceptance tests for the comment freeze/archive alignment.

Decisions under test (拍板):
- open/released thread  -> writable
- claimed (lock live)   -> read-only; precheck fails, submit 409, no row
- fulfilled             -> archived, permanently read-only; archive beats
                           any unfreeze attempt
- wall card / detail thread / my-claims note always show the same count
- a failed precheck or failed submit never leaves a row or a phantom count
- concurrent appends serialize: whatever lands is visible identically
  everywhere; a frozen thread rejects every concurrent writer

Stdlib-only on purpose: runs under pytest (no fixtures needed) and also
standalone from the backend dir:

    python3 -m app.tests.test_comment_thread
"""
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

from app import seed
from app.db import connect
from app.modules import wish_comment
from app.modules.wish_comment import projection

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
MAX = 500
LIVE = (NOW + timedelta(hours=1)).isoformat()
EXPIRED = (NOW - timedelta(hours=1)).isoformat()


# ---------- helpers ----------

def fresh_db():
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="wishclaim-test-")
    seed.init_db()


def make_wish(status="open", claimer=None, claimed_at=None, expires_at=None):
    c = connect()
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,"
        "data_quality) VALUES (?,?,?,?,?,?,?)",
        ("t", "n", status, claimer, claimed_at, expires_at, "clean"),
    )
    c.commit()
    wid = cur.lastrowid
    c.close()
    return wid


def preview(wid, content="你好", author="小甲", now=NOW):
    c = connect()
    try:
        return wish_comment.preview(c, wid, content, author, now, MAX)
    finally:
        c.close()


def submit(wid, content="你好", author="小甲", now=NOW):
    c = connect()
    try:
        return wish_comment.add_comment(c, wid, content, author, now, MAX)
    finally:
        c.close()


def thread(wid, now=NOW):
    c = connect()
    try:
        return wish_comment.get_thread(c, wid, now, MAX)
    finally:
        c.close()


def row_count(wid):
    c = connect()
    n = c.execute("SELECT COUNT(*) n FROM comments WHERE wish_id=?", (wid,)).fetchone()["n"]
    c.close()
    return n


def wall_count(wid):
    """Mirrors /api/wishes: SELECT all + projection.merge(counts)."""
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes")]
    projection.merge(rows, projection.counts(c))
    c.close()
    return next(r["comment_count"] for r in rows if r["id"] == wid)


def mine_count(wid, claimer):
    """Mirrors /api/mine."""
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE claimer=?", (claimer,))]
    projection.merge(rows, projection.counts(c, claimer=claimer))
    c.close()
    return next(r["comment_count"] for r in rows if r["id"] == wid)


def done_count(wid):
    """Mirrors /api/done."""
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE status='fulfilled'")]
    projection.merge(rows, projection.counts(c))
    c.close()
    return next(r["comment_count"] for r in rows if r["id"] == wid)


def set_status(wid, status, claimer=None, expires_at=None):
    c = connect()
    c.execute("UPDATE wishes SET status=?, claimer=?, expires_at=? WHERE id=?",
              (status, claimer, expires_at, wid))
    c.commit()
    c.close()


def assert_comment_error(reason, fn):
    try:
        fn()
    except wish_comment.CommentError as e:
        assert e.reason == reason, f"expected reason {reason!r}, got {e.reason!r}"
        return e
    raise AssertionError(f"expected CommentError({reason!r}), call succeeded")


def assert_counts_agree(wid, expected, claimer=None, done=False):
    assert wall_count(wid) == expected, f"wall {wall_count(wid)} != {expected}"
    assert thread(wid)["comment_count"] == expected, "detail count mismatch"
    assert len(thread(wid)["comments"]) == expected, "detail rows mismatch"
    assert row_count(wid) == expected, "table rows mismatch"
    if claimer is not None:
        assert mine_count(wid, claimer) == expected, "mine count mismatch"
    if done:
        assert done_count(wid) == expected, "done count mismatch"


# ---------- tests ----------

def test_open_thread_stays_writable():
    """Unclaimed open threads must NOT be frozen by this alignment."""
    fresh_db()
    for status in ("open", "released"):
        wid = make_wish(status=status)
        v = preview(wid)
        assert v["ok"] and v["state"] == "writable", (status, v)
        submit(wid)
        assert_counts_agree(wid, 1)


def test_claimed_precheck_fails_and_writes_nothing():
    fresh_db()
    wid = make_wish(status="claimed", claimer="小乙", claimed_at=NOW.isoformat(),
                    expires_at=LIVE)
    v = preview(wid)
    assert not v["ok"], v
    assert v["reason"] == "frozen" and v["state"] == "frozen", v
    e = assert_comment_error("frozen", lambda: submit(wid))
    assert e.status_code == 409
    assert_counts_agree(wid, 0, claimer="小乙")


def test_archived_is_permanent_and_beats_unfreeze():
    fresh_db()
    wid = make_wish(status="claimed", claimer="小乙", claimed_at=NOW.isoformat(),
                    expires_at=LIVE)
    set_status(wid, "fulfilled", claimer="小乙")
    v = preview(wid)
    assert not v["ok"] and v["reason"] == "archived" and v["state"] == "archived", v
    e = assert_comment_error("archived", lambda: submit(wid))
    assert e.status_code == 409
    # Archive wins over unfreeze: TTL release never touches a fulfilled row.
    from app.engines.claim_lock import release_if_expired
    assert release_if_expired("fulfilled", EXPIRED, NOW) is None
    # Even a row stamped fulfilled with an expired lock stays archived.
    set_status(wid, "fulfilled", claimer=None, expires_at=EXPIRED)
    assert not preview(wid)["ok"]
    assert_comment_error("archived", lambda: submit(wid))
    assert_counts_agree(wid, 0, done=True)


def test_counts_identical_on_all_surfaces():
    """Regression: claimed wishes used to show count+1 on wall/mine."""
    fresh_db()
    wid = make_wish()
    for _ in range(3):
        submit(wid)
    set_status(wid, "claimed", claimer="小乙", expires_at=LIVE)
    assert_counts_agree(wid, 3, claimer="小乙")
    set_status(wid, "fulfilled", claimer="小乙")
    assert_counts_agree(wid, 3, done=True)


def test_preview_pass_then_submit_fails_leaves_no_phantom():
    """TOCTOU: preview ok on an open thread, claim lands, submit must 409
    and no surface may show +1."""
    fresh_db()
    wid = make_wish()
    assert preview(wid)["ok"]
    set_status(wid, "claimed", claimer="小乙", expires_at=LIVE)
    assert_comment_error("frozen", lambda: submit(wid))
    assert_counts_agree(wid, 0, claimer="小乙")


def test_dirty_precheck_leaves_no_row():
    fresh_db()
    wid = make_wish()
    for content, author, reason in (
        ("   ", "小甲", "empty_content"),
        ("你好", "  ", "empty_author"),
        ("长" * (MAX + 1), "小甲", "too_long"),
    ):
        v = preview(wid, content=content, author=author)
        assert not v["ok"] and v["reason"] == reason, (reason, v)
        e = assert_comment_error(reason, lambda: submit(wid, content=content, author=author))
        assert e.status_code == 422
    assert_counts_agree(wid, 0)


def test_expired_claim_append_releases_lock():
    fresh_db()
    wid = make_wish(status="claimed", claimer="小乙", claimed_at=EXPIRED,
                    expires_at=EXPIRED)
    assert preview(wid)["ok"]
    submit(wid)
    c = connect()
    w = c.execute("SELECT status, claimer FROM wishes WHERE id=?", (wid,)).fetchone()
    c.close()
    assert w["status"] == "open" and w["claimer"] is None, dict(w)
    assert_counts_agree(wid, 1)


def test_concurrent_appends_on_open_thread_all_land_consistently():
    fresh_db()
    wid = make_wish()
    errors, done = [], []

    def worker(i):
        try:
            submit(wid, content=f"楼{i}", author=f"u{i}")
            done.append(i)
        except Exception as exc:  # noqa: BLE001 - record whatever happens
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(done) == 8
    c = connect()
    floors = [r["floor"] for r in c.execute(
        "SELECT floor FROM comments WHERE wish_id=? ORDER BY floor", (wid,))]
    c.close()
    assert floors == list(range(1, 9)), floors
    assert_counts_agree(wid, 8)


def test_concurrent_appends_on_frozen_thread_all_fail():
    fresh_db()
    wid = make_wish(status="claimed", claimer="小乙", claimed_at=NOW.isoformat(),
                    expires_at=LIVE)
    errors = []

    def worker(i):
        try:
            submit(wid, content=f"楼{i}", author=f"u{i}")
        except wish_comment.CommentError as e:
            errors.append(e.reason)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == ["frozen"] * 4, errors
    assert_counts_agree(wid, 0, claimer="小乙")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
