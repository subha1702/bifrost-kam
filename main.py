"""
Railway backend for Myntra Seller KAM Advisor.
No Bifrost connection — serves data pushed from the laptop via /api/push.
"""
from __future__ import annotations

import os
import json
import re
import calendar
from datetime import datetime, date
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL", "")
PUSH_TOKEN   = os.environ.get("PUSH_TOKEN", "change-me-in-railway-env")

app = FastAPI(title="Seller KAM Advisor — Railway", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_conn():
    if not DATABASE_URL:
        raise HTTPException(503, "DATABASE_URL not configured — set it in Railway Variables")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    cache_key   TEXT PRIMARY KEY,
                    seller_ids  TEXT[],
                    id_type     TEXT,
                    range_str   TEXT,
                    fetched_at  TEXT,
                    response    JSONB
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS target_cache (
                    cache_key   TEXT PRIMARY KEY,
                    fetched_at  TEXT,
                    data        JSONB
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portfolios (
                    id          TEXT PRIMARY KEY,
                    data        JSONB
                )
            """)
        conn.commit()


@app.on_event("startup")
def startup():
    if DATABASE_URL:
        init_db()


# ─── Models ───────────────────────────────────────────────────────────────────

class AnalysisRequest(BaseModel):
    ids:       list[str]
    idType:    str = "seller"
    range:     str = "last_30"
    startDate: Optional[str] = None
    endDate:   Optional[str] = None


# ─── Push endpoint (called by laptop after each Bifrost run) ──────────────────

class PushPayload(BaseModel):
    ids:        list[str]
    idType:     str = "seller"
    range:      str = "last_30"
    fetched_at: str
    response:   dict   # the full /api/analyze response


@app.post("/api/push")
def push_data(payload: PushPayload, authorization: str = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    if token != PUSH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized — wrong PUSH_TOKEN")

    cache_key = "_".join(sorted(payload.ids))[:64]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO analysis_cache (cache_key, seller_ids, id_type, range_str, fetched_at, response)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (cache_key) DO UPDATE SET
                    fetched_at = EXCLUDED.fetched_at,
                    response   = EXCLUDED.response
            """, (cache_key, payload.ids, payload.idType, payload.range,
                  payload.fetched_at, json.dumps(payload.response)))
        conn.commit()
    print(f"[push] saved {len(payload.ids)} sellers, key={cache_key}")
    return {"status": "ok", "cache_key": cache_key}


@app.post("/api/push/target")
def push_target(payload: dict, authorization: str = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    if token != PUSH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cache_key = payload["cache_key"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO target_cache (cache_key, fetched_at, data)
                VALUES (%s, %s, %s)
                ON CONFLICT (cache_key) DO UPDATE SET
                    fetched_at = EXCLUDED.fetched_at,
                    data       = EXCLUDED.data
            """, (cache_key, payload["fetched_at"], json.dumps(payload["data"])))
        conn.commit()
    return {"status": "ok"}


@app.post("/api/push/portfolios")
def push_portfolios(payload: list[dict], authorization: str = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    if token != PUSH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    with get_conn() as conn:
        with conn.cursor() as cur:
            for pf in payload:
                cur.execute("""
                    INSERT INTO portfolios (id, data) VALUES (%s, %s)
                    ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                """, (pf["id"], json.dumps(pf)))
        conn.commit()
    return {"status": "ok", "count": len(payload)}


# ─── Analysis endpoint (reads from Postgres cache) ────────────────────────────

@app.post("/api/analyze")
def analyze(req: AnalysisRequest):
    if not req.ids:
        raise HTTPException(400, "ids list is empty")

    cache_key = "_".join(sorted(req.ids))[:64]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT response, fetched_at FROM analysis_cache WHERE cache_key = %s",
                (cache_key,)
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(404, detail="Data not available yet. Run analysis from the laptop first.")

    resp = row["response"]
    resp["_cachedAt"] = row["fetched_at"]
    return resp


# ─── Target endpoint ──────────────────────────────────────────────────────────

@app.post("/api/target")
def get_target(req: AnalysisRequest):
    if not req.ids:
        raise HTTPException(400, "ids list is empty")

    today = date.today()
    cache_key = f"target_{'_'.join(sorted(req.ids))[:48]}_{today.strftime('%Y%m')}"

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT data FROM target_cache WHERE cache_key = %s", (cache_key,))
            row = cur.fetchone()

    if row:
        return row["data"]

    raise HTTPException(404, detail="Target data not available. Run from laptop first.")


# ─── Portfolios endpoint ──────────────────────────────────────────────────────

@app.get("/api/portfolios")
def list_portfolios():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT data FROM portfolios ORDER BY id")
            rows = cur.fetchall()

    out = []
    for row in rows:
        pf = row["data"]
        ids = pf.get("seller_ids", [])
        out.append({
            "id":        pf.get("id", ""),
            "name":      pf.get("name", ""),
            "sellers":   len(ids),
            "tags":      pf.get("tags", []),
            "sellerIds": ids,
        })
    return out


# ─── Notes / actions (no-op stubs — can be expanded later) ───────────────────

@app.post("/api/sellers/{seller_id}/notes")
def add_note(seller_id: str, payload: dict):
    return {"status": "ok"}


@app.post("/api/sellers/{seller_id}/insight/refresh")
def refresh_insight(seller_id: str, req: AnalysisRequest):
    return {"status": "ok"}


# ─── Serve built frontend ────────────────────────────────────────────────────

_DIST = Path(__file__).parent / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        f = _DIST / full_path
        if f.exists() and f.is_file():
            return FileResponse(f)
        return FileResponse(_DIST / "index.html")
