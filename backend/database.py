import sqlite3
import json
from datetime import datetime
from pathlib import Path

import os as _os
DB_PATH = Path(_os.getenv("DB_PATH", str(Path(__file__).parent / "scans.db")))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS known_stolen_videos (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
                first_found   TEXT DEFAULT (datetime('now')),
                UNIQUE(show_name, video_id)
            );
            CREATE INDEX IF NOT EXISTS idx_known_show ON known_stolen_videos(show_name);
            CREATE TABLE IF NOT EXISTS search_jobs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
                created_at          TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS scans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT NOT NULL,
                file_type   TEXT NOT NULL,
                file_size   INTEGER,
                status      TEXT DEFAULT 'pending',
                scanned_at  TEXT,
                results     TEXT,
                risk_level  TEXT,
                batch_id    INTEGER,
                source_url  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS batches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_url TEXT NOT NULL,
                platform    TEXT,
                total       INTEGER DEFAULT 0,
                completed   INTEGER DEFAULT 0,
                violations  INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'running',
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """)
        # Migrate existing tables
        for col, coltype in [("batch_id", "INTEGER"), ("source_url", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE scans ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE known_stolen_videos ADD COLUMN confirmed INTEGER DEFAULT NULL")
        except Exception:
            pass
        for col, coltype in [
            ("previously_known", "INTEGER DEFAULT 0"),
            ("skip_known",       "INTEGER DEFAULT 1"),
        ]:
            try:
                conn.execute(f"ALTER TABLE search_jobs ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        # Cross-platform columns on known_stolen_videos
        for col, coltype in [
            ("source",   "TEXT DEFAULT 'youtube_search'"),
            ("platform", "TEXT DEFAULT 'YouTube'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE known_stolen_videos ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        # Users table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role         TEXT NOT NULL DEFAULT 'viewer',
                created_at   TEXT DEFAULT (datetime('now')),
                last_login   TEXT
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
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            (username.strip().lower(), _hash_pw(password), role)
        )
        return cur.lastrowid

def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None

def verify_user(username: str, password: str) -> dict | None:
    """Return user dict if credentials valid, else None."""
    user = get_user_by_username(username)
    if not user:
        return None
    if not _verify_pw(password, user["password_hash"]):
        return None
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user["id"],))
    return user

def list_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,username,role,created_at,last_login FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

def delete_user(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))

def update_user_password(user_id: int, new_password: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_hash_pw(new_password), user_id)
        )

def update_user_role(user_id: int, role: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))

def user_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def seed_admin_user(username: str, password: str):
    """Ensure admin user exists. Creates if missing; upgrades to admin role if already exists."""
    existing = get_user_by_username(username)
    if not existing:
        create_user(username, password, role="admin")
    elif existing["role"] != "admin":
        # Ensure the env-configured user always has admin role
        with get_conn() as conn:
            conn.execute("UPDATE users SET role='admin' WHERE username=?", (username.strip().lower(),))


def create_scan(filename: str, file_type: str, file_size: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scans (filename, file_type, file_size, status) VALUES (?,?,?,?)",
            (filename, file_type, file_size, "scanning"),
        )
        return cur.lastrowid


def update_scan(scan_id: int, status: str, results: dict, risk_level: str):
    with get_conn() as conn:
        conn.execute(
            """UPDATE scans
               SET status=?, results=?, risk_level=?, scanned_at=?
               WHERE id=?""",
            (status, json.dumps(results), risk_level, datetime.utcnow().isoformat(), scan_id),
        )


def get_scan(scan_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row:
            d = dict(row)
            if d["results"]:
                d["results"] = json.loads(d["results"])
            return d
    return None


def list_scans(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scans ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d["results"]:
                d["results"] = json.loads(d["results"])
            result.append(d)
        return result


# --- Batch helpers ---

def create_batch(channel_url: str, platform: str, total: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO batches (channel_url, platform, total, status) VALUES (?,?,?,'running')",
            (channel_url, platform, total),
        )
        return cur.lastrowid


def update_batch_progress(batch_id: int):
    """Recount completed scans and violations for this batch."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status IN ('completed','error') THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN risk_level IN ('medium','high') THEN 1 ELSE 0 END) as violations
               FROM scans WHERE batch_id=?""",
            (batch_id,),
        ).fetchone()
        completed = row["completed"] or 0
        violations = row["violations"] or 0
        total = row["total"] or 0
        status = "completed" if completed >= total and total > 0 else "running"
        conn.execute(
            "UPDATE batches SET completed=?, violations=?, status=? WHERE id=?",
            (completed, violations, status, batch_id),
        )


def get_batch(batch_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        scans = conn.execute(
            "SELECT id,filename,status,risk_level,results,scanned_at,source_url FROM scans WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        result_scans = []
        for s in scans:
            sd = dict(s)
            if sd.get("results"):
                sd["results"] = json.loads(sd["results"])
            result_scans.append(sd)
        d["scans"] = result_scans
        return d


def list_batches(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Known stolen videos (persistent across searches) ---

def _norm_show(show_name: str) -> str:
    return show_name.strip().lower()


def get_known_video_ids(show_name: str) -> set:
    """Return all video IDs already found for this show."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT video_id FROM known_stolen_videos WHERE show_name=?",
            (_norm_show(show_name),)
        ).fetchall()
        return {r["video_id"] for r in rows}


def save_known_videos(show_name: str, videos: list):
    """Persist newly discovered videos so they are skipped in future searches."""
    norm = _norm_show(show_name)
    with get_conn() as conn:
        for v in videos:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO known_stolen_videos
                       (show_name, video_id, title, channel, channel_id, channel_url,
                        url, views, upload_date, is_short, stolen, source, platform)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    """Remove one video from the known list so it surfaces again in the next scan."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM known_stolen_videos WHERE show_name=? AND video_id=?",
            (_norm_show(show_name), video_id),
        )


def clear_known_videos(show_name: str):
    """Delete ALL known videos for a show — next scan starts completely fresh."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM known_stolen_videos WHERE show_name=?",
            (_norm_show(show_name),),
        )


def clear_all_known_videos() -> int:
    """Delete ALL known videos across every show. Returns number of rows deleted."""
    with get_conn() as conn:
        n = conn.execute("DELETE FROM known_stolen_videos").rowcount
        conn.execute("DELETE FROM search_jobs")
        return n


def get_all_known_for_show(show_name: str) -> list[dict]:
    """All tracked videos for a show (stolen + official + unconfirmed), newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM known_stolen_videos WHERE show_name=? ORDER BY first_found DESC",
            (_norm_show(show_name),),
        ).fetchall()
        return [dict(r) for r in rows]


def set_video_confirmed(show_name: str, video_id: str, confirmed: bool | None):
    """Set manual confirmation status: True=confirmed stolen, False=dismissed, None=unreviewed.
    If the video isn't tracked yet, inserts a minimal record so confirmation is never lost.
    Confirming also sets stolen=1 in case the record was unclassified (stolen=NULL).
    """
    norm = _norm_show(show_name)
    db_val = None if confirmed is None else (1 if confirmed else 0)
    with get_conn() as conn:
        affected = conn.execute(
            """UPDATE known_stolen_videos
               SET confirmed=?, stolen=CASE WHEN ?=1 THEN 1 ELSE stolen END
               WHERE show_name=? AND video_id=?""",
            (db_val, db_val, norm, video_id),
        ).rowcount
        if affected == 0:
            # Row doesn't exist yet — create it so the confirmation is not lost
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO known_stolen_videos
                       (show_name, video_id, stolen, confirmed, source, platform)
                       VALUES (?,?,?,?,?,?)""",
                    (norm, video_id,
                     1 if confirmed is True else None,
                     db_val,
                     "manual", "YouTube"),
                )
            except Exception:
                pass


def get_confirmed_map(show_name: str) -> dict:
    """Return {video_id: confirmed_int_or_none} for all rows of a show that have a confirmed value."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT video_id, confirmed FROM known_stolen_videos WHERE show_name=? AND confirmed IS NOT NULL",
            (_norm_show(show_name),)
        ).fetchall()
        return {r["video_id"]: r["confirmed"] for r in rows}


def get_all_known_stolen(show_name: str) -> list[dict]:
    """Return every stolen video ever found for this show, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM known_stolen_videos
               WHERE show_name=? AND stolen=1
               ORDER BY first_found DESC""",
            (_norm_show(show_name),)
        ).fetchall()
        return [dict(r) for r in rows]


def get_known_show_stats(show_name: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN stolen=1 THEN 1 ELSE 0 END) as stolen,
                      SUM(CASE WHEN is_short=1 AND stolen=1 THEN 1 ELSE 0 END) as stolen_shorts,
                      MIN(first_found) as first_search,
                      MAX(first_found) as last_search
               FROM known_stolen_videos WHERE show_name=?""",
            (_norm_show(show_name),)
        ).fetchone()
        return dict(row) if row else {}


def get_confirmed_stolen(show_name: str | None = None) -> list[dict]:
    """Return all manually confirmed stolen videos, optionally filtered by show."""
    with get_conn() as conn:
        if show_name:
            rows = conn.execute(
                """SELECT * FROM known_stolen_videos
                   WHERE show_name=? AND confirmed=1
                   ORDER BY first_found DESC""",
                (_norm_show(show_name),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM known_stolen_videos
                   WHERE confirmed=1
                   ORDER BY show_name, first_found DESC"""
            ).fetchall()
        return [dict(r) for r in rows]


def get_all_reviewed(show_name: str | None = None) -> list[dict]:
    """Return all manually reviewed videos (confirmed=1 stolen OR confirmed=0 not-stolen),
    optionally filtered by show, newest first within each show."""
    with get_conn() as conn:
        if show_name:
            rows = conn.execute(
                """SELECT * FROM known_stolen_videos
                   WHERE show_name=? AND confirmed IS NOT NULL
                   ORDER BY confirmed DESC, first_found DESC""",
                (_norm_show(show_name),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM known_stolen_videos
                   WHERE confirmed IS NOT NULL
                   ORDER BY show_name, confirmed DESC, first_found DESC"""
            ).fetchall()
        return [dict(r) for r in rows]


def get_review_summary() -> list[dict]:
    """Per-show counts of stolen vs not-stolen confirmed videos."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT show_name,
                      SUM(CASE WHEN confirmed=1 THEN 1 ELSE 0 END) as stolen_count,
                      SUM(CASE WHEN confirmed=0 THEN 1 ELSE 0 END) as not_stolen_count,
                      COUNT(*) as total_reviewed,
                      MAX(first_found) as last_reviewed
               FROM known_stolen_videos
               WHERE confirmed IS NOT NULL
               GROUP BY show_name
               ORDER BY last_reviewed DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def list_confirmed_shows() -> list[dict]:
    """Return shows that have at least one confirmed stolen video, with counts."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT show_name,
                      COUNT(*) as confirmed_count,
                      MAX(first_found) as last_confirmed
               FROM known_stolen_videos
               WHERE confirmed=1
               GROUP BY show_name
               ORDER BY last_confirmed DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def list_monitored_shows() -> list[dict]:
    """Return distinct show names with their total known stolen count."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT show_name,
                      COUNT(*) as total_found,
                      SUM(CASE WHEN stolen=1 THEN 1 ELSE 0 END) as stolen_count,
                      MAX(first_found) as last_search
               FROM known_stolen_videos
               GROUP BY show_name
               ORDER BY last_search DESC""",
        ).fetchall()
        return [dict(r) for r in rows]


# --- Search job helpers ---

def create_search_job(query: str, official_channel: str, official_channel_id: str, platform: str, queries_total: int, skip_known: bool = True) -> int:
    previously_known = len(get_known_video_ids(query)) if skip_known else 0
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO search_jobs
               (query, official_channel, official_channel_id, platform, queries_total,
                previously_known, skip_known, status)
               VALUES (?,?,?,?,?,?,?,'running')""",
            (query, official_channel, official_channel_id, platform, queries_total,
             previously_known, 1 if skip_known else 0),
        )
        return cur.lastrowid


def append_search_results(job_id: int, new_results: list, is_incremental: bool = False, count_query: bool = True):
    """Merge new_results into the job, deduplicating by video_id.
    Set count_query=False for supplemental RSS / channel scrape phases that should
    not advance the queries_done counter or flip status to completed.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM search_jobs WHERE id=?", (job_id,)).fetchone()
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

        conn.execute(
            """UPDATE search_jobs SET results=?, total_found=?, stolen_count=?,
               shorts_count=?, queries_done=?, status=? WHERE id=?""",
            (json.dumps(existing), len(existing), stolen, shorts, queries_done, status, job_id),
        )


def get_search_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM search_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["results"] = json.loads(d["results"] or "[]")
        return d


def list_search_jobs(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,query,platform,status,total_found,stolen_count,shorts_count,queries_done,queries_total,created_at FROM search_jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_scan_in_batch(filename: str, file_type: str, file_size: int, batch_id: int, source_url: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scans (filename, file_type, file_size, status, batch_id, source_url) VALUES (?,?,?,?,?,?)",
            (filename, file_type, file_size, "scanning", batch_id, source_url),
        )
        return cur.lastrowid

