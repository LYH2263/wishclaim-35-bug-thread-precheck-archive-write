"""Endpoint-level acceptance tests: the three count surfaces and the
freeze/archive gate wired through app.main.

Skipped when fastapi is not installed (e.g. a bare stdlib sandbox); the
module-level suite in test_comment_thread.py covers the same decisions
without third-party deps.
"""
import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from app import main, seed


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return main


def _count(rows, wid):
    return next(r["comment_count"] for r in rows if r["id"] == wid)


def test_three_surfaces_show_same_count(db):
    wid = db.create_wish(db.WishIn(title="x"))["id"]
    for i in range(2):
        db.add_comment(wid, db.CommentIn(content=f"楼{i}", author="小甲"))
    db.claim(wid, db.ClaimIn(claimer="小乙"))
    assert _count(db.list_wishes(), wid) == 2
    assert db.get_comments(wid)["comment_count"] == 2
    assert _count(db.mine(claimer="小乙"), wid) == 2
    db.fulfill(wid)
    assert _count(db.done(), wid) == 2
    assert db.get_comments(wid)["comment_count"] == 2


def test_claimed_precheck_fails_and_submit_409(db):
    wid = db.create_wish(db.WishIn(title="x"))["id"]
    db.claim(wid, db.ClaimIn(claimer="小乙"))
    v = db.preview_comment(wid, db.CommentIn(content="你好", author="小甲"))
    assert v["ok"] is False and v["reason"] == "frozen"
    with pytest.raises(HTTPException) as ei:
        db.add_comment(wid, db.CommentIn(content="你好", author="小甲"))
    assert ei.value.status_code == 409 and ei.value.detail == "frozen"
    assert _count(db.list_wishes(), wid) == 0
    assert db.get_comments(wid)["comment_count"] == 0
    assert _count(db.mine(claimer="小乙"), wid) == 0


def test_archived_append_409_and_unfreeze_cannot_reopen(db):
    wid = db.create_wish(db.WishIn(title="x"))["id"]
    db.add_comment(wid, db.CommentIn(content="旧楼", author="小甲"))
    db.claim(wid, db.ClaimIn(claimer="小乙"))
    db.fulfill(wid)
    # Unfreeze has no grip on an archived wish.
    with pytest.raises(HTTPException) as ei:
        db.release(wid)
    assert ei.value.status_code == 400
    v = db.preview_comment(wid, db.CommentIn(content="新楼", author="小丙"))
    assert v["ok"] is False and v["reason"] == "archived"
    with pytest.raises(HTTPException) as ei:
        db.add_comment(wid, db.CommentIn(content="新楼", author="小丙"))
    assert ei.value.status_code == 409 and ei.value.detail == "archived"
    assert _count(db.done(), wid) == 1
    assert db.get_comments(wid)["comment_count"] == 1


def test_preview_pass_then_claim_then_submit_leaves_counts_untouched(db):
    wid = db.create_wish(db.WishIn(title="x"))["id"]
    assert db.preview_comment(wid, db.CommentIn(content="你好", author="小甲"))["ok"]
    db.claim(wid, db.ClaimIn(claimer="小乙"))
    with pytest.raises(HTTPException):
        db.add_comment(wid, db.CommentIn(content="你好", author="小甲"))
    assert _count(db.list_wishes(), wid) == 0
    assert db.get_comments(wid)["comment_count"] == 0
    assert _count(db.mine(claimer="小乙"), wid) == 0


def test_open_thread_not_frozen(db):
    wid = db.create_wish(db.WishIn(title="x"))["id"]
    assert db.preview_comment(wid, db.CommentIn(content="你好", author="小甲"))["ok"]
    db.add_comment(wid, db.CommentIn(content="你好", author="小甲"))
    assert _count(db.list_wishes(), wid) == 1
