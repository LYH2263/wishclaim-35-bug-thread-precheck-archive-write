from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.claim_lock import claim_allowed, lock_payload, release_if_expired
from app.modules import wish_comment
from app.modules.wish_comment import projection

app = FastAPI(title="Wishclaim", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

def now(): return datetime.now(timezone.utc)

def _setting(key: str, default: str) -> str:
    c = connect(); row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone(); c.close()
    return row["value"] if row else default

def ttl():
    return int(_setting("ttl_seconds", "86400"))

def comment_max_length():
    return int(_setting("comment_max_length", "500"))

def sweep(c):
    for r in c.execute("SELECT * FROM wishes WHERE status='claimed'"):
        rel = release_if_expired(r["status"], r["expires_at"], now())
        if rel:
            c.execute("UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
                      (rel["status"], None, None, None, r["id"]))

@app.get("/api/health")
def health(): return {"ok": True, "project": "wishclaim"}

@app.get("/api/wishes")
def list_wishes():
    c = connect(); sweep(c); c.commit()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes ORDER BY id DESC")]
    projection.merge(rows, projection.counts(c))
    c.close(); return rows

@app.get("/api/wishes/{wid}")
def get_wish(wid: int):
    c = connect(); sweep(c); c.commit()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    row = dict(r)
    projection.merge([row], projection.counts(c, wish_ids=[wid]))
    c.close(); return row

class WishIn(BaseModel):
    title: str
    note: str = ""

@app.post("/api/wishes")
def create_wish(body: WishIn):
    c = connect()
    cur = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    (body.title, body.note, "open", "clean"))
    c.commit(); wid = cur.lastrowid; c.close(); return {"id": wid}

class ClaimIn(BaseModel):
    claimer: str

@app.post("/api/wishes/{wid}/claim")
def claim(wid: int, body: ClaimIn):
    c = connect(); sweep(c); c.commit()
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        # Re-check under the write lock so two concurrent claims serialize.
        allowed = claim_allowed(r["status"], r["claimer"], now(), r["expires_at"])
        if not allowed["ok"]:
            raise HTTPException(409, allowed["reason"])
        p = lock_payload(body.claimer, now(), ttl())
        c.execute("UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
                  (p["status"], p["claimer"], p["claimed_at"], p["expires_at"], wid))
        c.commit()
    except Exception:
        c.rollback(); raise
    c.close(); return p

@app.post("/api/wishes/{wid}/release")
def release(wid: int):
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if r["status"] != "claimed":
            raise HTTPException(400, "not_claimed")
        c.execute("UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL, expires_at=NULL WHERE id=?", (wid,))
        c.commit()
    except Exception:
        c.rollback(); raise
    c.close(); return {"ok": True, "status": "released"}

@app.post("/api/wishes/{wid}/fulfill")
def fulfill(wid: int):
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if r["status"] != "claimed":
            raise HTTPException(400, "need_claim")
        c.execute("UPDATE wishes SET status='fulfilled' WHERE id=?", (wid,))
        c.commit()
    except Exception:
        c.rollback(); raise
    c.close(); return {"ok": True, "status": "fulfilled"}

# ---------- comment thread ----------

class CommentIn(BaseModel):
    content: str = ""
    author: str = ""

@app.get("/api/wishes/{wid}/comments")
def get_comments(wid: int):
    c = connect(); sweep(c); c.commit()
    try:
        payload = wish_comment.get_thread(c, wid, now(), comment_max_length())
    except wish_comment.CommentError as e:
        c.close(); raise HTTPException(e.status_code, e.reason)
    c.close(); return payload

@app.post("/api/wishes/{wid}/comments/preview")
def preview_comment(wid: int, body: CommentIn):
    """Pre-flight verdict for length/author/gate. Never writes — always 200
    unless the wish itself is missing."""
    c = connect(); sweep(c); c.commit()
    try:
        verdict = wish_comment.preview(c, wid, body.content, body.author,
                                       now(), comment_max_length())
    except wish_comment.CommentError as e:
        c.close(); raise HTTPException(e.status_code, e.reason)
    c.close(); return verdict

@app.post("/api/wishes/{wid}/comments")
def add_comment(wid: int, body: CommentIn):
    c = connect()
    try:
        row = wish_comment.add_comment(c, wid, body.content, body.author,
                                       now(), comment_max_length())
    except wish_comment.CommentError as e:
        c.close(); raise HTTPException(e.status_code, e.reason)
    c.close(); return row

@app.get("/api/mine")
def mine(claimer: str):
    c = connect(); sweep(c); c.commit()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE claimer=?", (claimer,))]
    projection.merge(rows, projection.counts(c, claimer=claimer))
    c.close(); return rows

@app.get("/api/done")
def done():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE status='fulfilled'")]
    projection.merge(rows, projection.counts(c))
    c.close(); return rows

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

@app.get("/api/rules")
def rules():
    return {
        "mutex": "同一愿望同时只能被一人认领",
        "ttl": "认领超时未核销则自动释放",
        "fulfill": "核销后状态变为 fulfilled",
        "comment_freeze": "认领期间留言串冻结，手动释放或超时后恢复可写",
        "comment_archive": "核销后留言串永久只读，不可删除、不可追加",
    }
