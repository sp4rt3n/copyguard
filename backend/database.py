import json
import os
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://copyguard:copyguard@db:5432/copyguard"
)


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _exec(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def init_db():
    with get_conn() as conn:
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS known_stolen_videos (
                id            SERIAL PRIMARY KEY,
                show_name     TEXT NOT NULL,
                video_id      TEXT NOT NULL,
                title         TEXT,
                channel       TEXT,
                channel_id    TEXT,
                channel_url   TEXT,
                url           TEXT,
                views         INTEGER,
                upload_date   TEXT,
                is_short      INTEGER DEFAULT 0,
                stolen        INTEGER,
                confirmed     INTEGER DEFAULT NULL,
                source        TEXT DEFAULT 'youtube_search',
                platform      TEXT DEFAULT 'YouTube',
                first_found   TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS')),
                UNIQUE(show_name, video_id)
            )
        """)
        _exec(conn, "CREATE INDEX IF NOT EXISTS idx_known_show ON known_stolen_videos(show_name)")
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS search_jobs (
                id                  SERIAL PRIMARY KEY,
                query               TEXT NOT NULL,
                official_channel    TEXT,
                official_channel_id TEXT,
                platform            TEXT DEFAULT 'youtube',
                status              TEXT DEFAULT 'running',
                total_found         INTEGER DEFAULT 0,
                stolen_count        INTEGER DEFAULT 0,
                shorts_count        INTEGER DEFAULT 0,
                queries_done        INTEGER DEFAULT 0,
                queries_total       INTEGER DEFAULT 0,
                previously_known    INTEGER DEFAULT 0,
                skip_known          INTEGER DEFAULT 1,
                results             TEXT DEFAULT '[]',
                created_at          TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS'))
            )
        """)
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS scans (
                id          SERIAL PRIMARY KEY,
                filename    TEXT NOT NULL,
                file_type   TEXT NOT NULL,
                file_size   INTEGER,
                status      TEXT DEFAULT 'pending',
                scanned_at  TEXT,
                results     TEXT,
                risk_level  TEXT,
                batch_id    INTEGER,
                source_url  TEXT,
                created_at  TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS'))
            )
        """)
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS batches (
                id          SERIAL PRIMARY KEY,
                channel_url TEXT NOT NULL,
                platform    TEXT,
                total       INTEGER DEFAULT 0,
                completed   INTEGER DEFAULT 0,
                violations  INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'running',
                created_at  TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS'))
            )
        """)
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'viewer',
                created_at    TEXT DEFAULT (to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS')),
                last_login    TEXT
            )
        """)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
def _hash_pw(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def _verify_pw(password: str, hashed: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False

VALID_ROLES = {"admin", "operator", "viewer"}

def create_user(username: str, password: str, role: str = "viewer") -> int:
    if role not in VALID_ROLES:
        role = "viewer"
    with get_conn() as conn:
        cur = _exec(conn,
            "INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s) RETURNING id",
            (username.strip().lower(), _hash_pw(password), role)
        )
        return cur.fetchone()["id"]

def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT * FROM users WHERE username=%s", (username.strip().lower(),))
        row = cur.fetchone()
        return dict(row) if row else None

def verify_user(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user:
        return None
    if not _verify_pw(password, user["password_hash"]):
        return None
    with get_conn() as conn:
        _exec(conn,
            "UPDATE users SET last_login=%s WHERE id=%s",
            (datetime.utcnow().isoformat(), user["id"])
        )
    return user

def list_users() -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT id,username,role,created_at,last_login FROM users ORDER BY id")
        return [dict(r) for r in cur.fetchall()]

def delete_user(user_id: int):
    with get_conn() as conn:
        _exec(conn, "DELETE FROM users WHERE id=%s", (user_id,))

def update_user_password(user_id: int, new_password: str):
    with get_conn() as conn:
        _exec(conn,
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (_hash_pw(new_password), user_id)
        )

def update_user_role(user_id: int, role: str):
    with get_conn() as conn:
        _exec(conn, "UPDATE users SET role=%s WHERE id=%s", (role, user_id))

def user_count() -> int:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT COUNT(*) as n FROM users")
        return cur.fetchone()["n"]

def seed_admin_user(username: str, password: str):
    existing = get_user_by_username(username)
    if not existing:
        create_user(username, password, role="admin")
    elif existing["role"] != "admin":
        with get_conn() as conn:
            _exec(conn, "UPDATE users SET role='admin' WHERE username=%s", (username.strip().lower(),))


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------
def create_scan(filename: str, file_type: str, file_size: int) -> int:
    with get_conn() as conn:
        cur = _exec(conn,
            "INSERT INTO scans (filename, file_type, file_size, status) VALUES (%s,%s,%s,%s) RETURNING id",
            (filename, file_type, file_size, "scanning"),
        )
        return cur.fetchone()["id"]

def update_scan(scan_id: int, status: str, results: dict, risk_level: str):
    with get_conn() as conn:
        _exec(conn,
            "UPDATE scans SET status=%s, results=%s, risk_level=%s, scanned_at=%s WHERE id=%s",
            (status, json.dumps(results), risk_level, datetime.utcnow().isoformat(), scan_id),
        )

def get_scan(scan_id: int) -> dict | None:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT * FROM scans WHERE id=%s", (scan_id,))
        row = cur.fetchone()
        if row:
            d = dict(row)
            if d["results"]:
                d["results"] = json.loads(d["results"])
            return d
    return None

def list_scans(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT * FROM scans ORDER BY created_at DESC LIMIT %s", (limit,))
        result = []
        for row in cur.fetchall():
            d = dict(row)
            if d["results"]:
                d["results"] = json.loads(d["results"])
            result.append(d)
        return result


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------
def create_batch(channel_url: str, platform: str, total: int) -> int:
    with get_conn() as conn:
        cur = _exec(conn,
            "INSERT INTO batches (channel_url, platform, total, status) VALUES (%s,%s,%s,'running') RETURNING id",
            (channel_url, platform, total),
        )
        return cur.fetchone()["id"]

def update_batch_progress(batch_id: int):
    with get_conn() as conn:
        cur = _exec(conn,
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status IN ('completed','error') THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN risk_level IN ('medium','high') THEN 1 ELSE 0 END) as violations
               FROM scans WHERE batch_id=%s""",
            (batch_id,),
        )
        row = cur.fetchone()
        completed  = row["completed"]  or 0
        violations = row["violations"] or 0
        total      = row["total"]      or 0
        status = "completed" if completed >= total and total > 0 else "running"
        _exec(conn,
            "UPDATE batches SET completed=%s, violations=%s, status=%s WHERE id=%s",
            (completed, violations, status, batch_id),
        )

def get_batch(batch_id: int) -> dict | None:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT * FROM batches WHERE id=%s", (batch_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        cur2 = _exec(conn,
            "SELECT id,filename,status,risk_level,results,scanned_at,source_url FROM scans WHERE batch_id=%s ORDER BY id",
            (batch_id,),
        )
        result_scans = []
        for s in cur2.fetchall():
            sd = dict(s)
            if sd.get("results"):
                sd["results"] = json.loads(sd["results"])
            result_scans.append(sd)
        d["scans"] = result_scans
        return d

def list_batches(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT * FROM batches ORDER BY created_at DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Known stolen videos
# ---------------------------------------------------------------------------
def _norm_show(show_name: str) -> str:
    return show_name.strip().lower()

def get_known_video_ids(show_name: str) -> set:
    with get_conn() as conn:
        cur = _exec(conn,
            "SELECT video_id FROM known_stolen_videos WHERE show_name=%s",
            (_norm_show(show_name),)
        )
        return {r["video_id"] for r in cur.fetchall()}

def save_known_videos(show_name: str, videos: list):
    norm = _norm_show(show_name)
    with get_conn() as conn:
        for v in videos:
            try:
                _exec(conn,
                    """INSERT INTO known_stolen_videos
                       (show_name, video_id, title, channel, channel_id, channel_url,
                        url, views, upload_date, is_short, stolen, source, platform)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (show_name, video_id) DO NOTHING""",
                    (norm, v.get("video_id"), v.get("title"), v.get("channel"),
                     v.get("channel_id"), v.get("channel_url"), v.get("url"),
                     v.get("views"), v.get("upload_date"),
                     1 if v.get("is_short") else 0,
                     1 if v.get("stolen") is True else (0 if v.get("stolen") is False else None),
                     v.get("source", "youtube_search"),
                     v.get("platform", "YouTube")),
                )
            except Exception:
                pass

def delete_known_video(show_name: str, video_id: str):
    with get_conn() as conn:
        _exec(conn,
            "DELETE FROM known_stolen_videos WHERE show_name=%s AND video_id=%s",
            (_norm_show(show_name), video_id),
        )

def clear_known_videos(show_name: str):
    with get_conn() as conn:
        _exec(conn,
            "DELETE FROM known_stolen_videos WHERE show_name=%s",
            (_norm_show(show_name),),
        )

def clear_all_known_videos() -> int:
    with get_conn() as conn:
        cur = _exec(conn, "DELETE FROM known_stolen_videos")
        n = cur.rowcount
        _exec(conn, "DELETE FROM search_jobs")
        return n

def get_all_known_for_show(show_name: str) -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn,
            "SELECT * FROM known_stolen_videos WHERE show_name=%s ORDER BY first_found DESC",
            (_norm_show(show_name),),
        )
        return [dict(r) for r in cur.fetchall()]

def set_video_confirmed(show_name: str, video_id: str, confirmed: bool | None):
    norm    = _norm_show(show_name)
    db_val  = None if confirmed is None else (1 if confirmed else 0)
    with get_conn() as conn:
        cur = _exec(conn,
            """UPDATE known_stolen_videos
               SET confirmed=%s, stolen=CASE WHEN %s=1 THEN 1 ELSE stolen END
               WHERE show_name=%s AND video_id=%s""",
            (db_val, db_val, norm, video_id),
        )
        if cur.rowcount == 0:
            try:
                _exec(conn,
                    """INSERT INTO known_stolen_videos
                       (show_name, video_id, stolen, confirmed, source, platform)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (show_name, video_id) DO NOTHING""",
                    (norm, video_id,
                     1 if confirmed is True else None,
                     db_val, "manual", "YouTube"),
                )
            except Exception:
                pass

def get_confirmed_map(show_name: str) -> dict:
    with get_conn() as conn:
        cur = _exec(conn,
            "SELECT video_id, confirmed FROM known_stolen_videos WHERE show_name=%s AND confirmed IS NOT NULL",
            (_norm_show(show_name),)
        )
        return {r["video_id"]: r["confirmed"] for r in cur.fetchall()}

def get_all_known_stolen(show_name: str) -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn,
            "SELECT * FROM known_stolen_videos WHERE show_name=%s AND stolen=1 ORDER BY first_found DESC",
            (_norm_show(show_name),)
        )
        return [dict(r) for r in cur.fetchall()]

def get_known_show_stats(show_name: str) -> dict:
    with get_conn() as conn:
        cur = _exec(conn,
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN stolen=1 THEN 1 ELSE 0 END) as stolen,
                      SUM(CASE WHEN is_short=1 AND stolen=1 THEN 1 ELSE 0 END) as stolen_shorts,
                      MIN(first_found) as first_search,
                      MAX(first_found) as last_search
               FROM known_stolen_videos WHERE show_name=%s""",
            (_norm_show(show_name),)
        )
        row = cur.fetchone()
        return dict(row) if row else {}

def get_confirmed_stolen(show_name: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if show_name:
            cur = _exec(conn,
                "SELECT * FROM known_stolen_videos WHERE show_name=%s AND confirmed=1 ORDER BY first_found DESC",
                (_norm_show(show_name),),
            )
        else:
            cur = _exec(conn,
                "SELECT * FROM known_stolen_videos WHERE confirmed=1 ORDER BY show_name, first_found DESC"
            )
        return [dict(r) for r in cur.fetchall()]

def get_all_reviewed(show_name: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if show_name:
            cur = _exec(conn,
                """SELECT * FROM known_stolen_videos
                   WHERE show_name=%s AND confirmed IS NOT NULL
                   ORDER BY confirmed DESC, first_found DESC""",
                (_norm_show(show_name),),
            )
        else:
            cur = _exec(conn,
                """SELECT * FROM known_stolen_videos
                   WHERE confirmed IS NOT NULL
                   ORDER BY show_name, confirmed DESC, first_found DESC"""
            )
        return [dict(r) for r in cur.fetchall()]

def get_review_summary() -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn,
            """SELECT show_name,
                      SUM(CASE WHEN confirmed=1 THEN 1 ELSE 0 END) as stolen_count,
                      SUM(CASE WHEN confirmed=0 THEN 1 ELSE 0 END) as not_stolen_count,
                      COUNT(*) as total_reviewed,
                      MAX(first_found) as last_reviewed
               FROM known_stolen_videos
               WHERE confirmed IS NOT NULL
               GROUP BY show_name
               ORDER BY last_reviewed DESC"""
        )
        return [dict(r) for r in cur.fetchall()]

def list_confirmed_shows() -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn,
            """SELECT show_name,
                      COUNT(*) as confirmed_count,
                      MAX(first_found) as last_confirmed
               FROM known_stolen_videos
               WHERE confirmed=1
               GROUP BY show_name
               ORDER BY last_confirmed DESC"""
        )
        return [dict(r) for r in cur.fetchall()]

def list_monitored_shows() -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn,
            """SELECT show_name,
                      COUNT(*) as total_found,
                      SUM(CASE WHEN stolen=1 THEN 1 ELSE 0 END) as stolen_count,
                      MAX(first_found) as last_search
               FROM known_stolen_videos
               GROUP BY show_name
               ORDER BY last_search DESC"""
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Search jobs
# ---------------------------------------------------------------------------
def create_search_job(query: str, official_channel: str, official_channel_id: str, platform: str, queries_total: int, skip_known: bool = True) -> int:
    previously_known = len(get_known_video_ids(query)) if skip_known else 0
    with get_conn() as conn:
        cur = _exec(conn,
            """INSERT INTO search_jobs
               (query, official_channel, official_channel_id, platform, queries_total,
                previously_known, skip_known, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'running') RETURNING id""",
            (query, official_channel, official_channel_id, platform, queries_total,
             previously_known, 1 if skip_known else 0),
        )
        return cur.fetchone()["id"]

def append_search_results(job_id: int, new_results: list, is_incremental: bool = False, count_query: bool = True):
    with get_conn() as conn:
        cur = _exec(conn, "SELECT * FROM search_jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
        if not row:
            return
        existing = json.loads(row["results"] or "[]")
        seen = {r["video_id"] for r in existing if r.get("video_id")}
        for r in new_results:
            if r.get("video_id") and r["video_id"] not in seen:
                seen.add(r["video_id"])
                existing.append(r)

        stolen = sum(1 for r in existing if r.get("stolen") is True)
        shorts  = sum(1 for r in existing if r.get("is_short"))

        if count_query:
            queries_done  = (row["queries_done"] or 0) + 1
            queries_total = row["queries_total"] or 1
            status = "completed" if queries_done >= queries_total else "running"
        else:
            queries_done  = row["queries_done"] or 0
            status        = row["status"] or "running"

        _exec(conn,
            """UPDATE search_jobs SET results=%s, total_found=%s, stolen_count=%s,
               shorts_count=%s, queries_done=%s, status=%s WHERE id=%s""",
            (json.dumps(existing), len(existing), stolen, shorts, queries_done, status, job_id),
        )

def get_search_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        cur = _exec(conn, "SELECT * FROM search_jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["results"] = json.loads(d["results"] or "[]")
        return d

def list_search_jobs(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        cur = _exec(conn,
            "SELECT id,query,platform,status,total_found,stolen_count,shorts_count,queries_done,queries_total,created_at FROM search_jobs ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

def create_scan_in_batch(filename: str, file_type: str, file_size: int, batch_id: int, source_url: str) -> int:
    with get_conn() as conn:
        cur = _exec(conn,
            "INSERT INTO scans (filename, file_type, file_size, status, batch_id, source_url) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (filename, file_type, file_size, "scanning", batch_id, source_url),
        )
        return cur.fetchone()["id"]
