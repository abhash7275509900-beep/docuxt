"""
Metadata store (SQLite).

Supports multi-worker concurrency via WAL mode and busy_timeout (with automatic
fallback for FUSE mounts that do not support POSIX file locking), persistent
jobs with TTL eviction and startup stale-job sweep, atomic document locks with
TTL expiration, and fine-grained file/document permission resolution.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DOCENGINE_DB_PATH", os.path.join(BASE_DIR, "storage", "docengine.db"))
LOCK_TTL = int(os.environ.get("DOCENGINE_LOCK_TTL", 15 * 60))  # 15 minutes default
JOB_TTL = int(os.environ.get("DOCENGINE_JOB_TTL", 3600))        # 1 hour default

_db_lock = threading.RLock()
_fuse_fallback_cache = {}
_fuse_conn_cache = {}


def _open_sqlite(path: str):
    mode = _fuse_fallback_cache.get(path)
    if mode is False:
        conn = sqlite3.connect(path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    if mode is True:
        conn = _fuse_conn_cache.get(path)
        if conn is None:
            conn = sqlite3.connect(f"file:{path}?vfs=unix-none", uri=True, timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA journal_mode=MEMORY")
            _fuse_conn_cache[path] = conn
        return conn

    conn = None
    try:
        conn = sqlite3.connect(path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _fuse_fallback_cache[path] = False
        return conn
    except sqlite3.OperationalError:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _fuse_fallback_cache[path] = True
        conn = sqlite3.connect(f"file:{path}?vfs=unix-none", uri=True, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA journal_mode=MEMORY")
        _fuse_conn_cache[path] = conn
        return conn


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db_lock:
        conn = _open_sqlite(DB_PATH)
        conn.row_factory = sqlite3.Row
        is_fuse = _fuse_fallback_cache.get(DB_PATH) is True
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if not is_fuse:
                conn.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                original_name TEXT,
                kind TEXT NOT NULL DEFAULT 'upload',
                format TEXT,
                derived_from TEXT,
                size INTEGER,
                created_at REAL NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS annotations (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                type TEXT NOT NULL,
                data TEXT NOT NULL,
                page INTEGER,
                created_at REAL NOT NULL,
                FOREIGN KEY(file_id) REFERENCES files(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                title TEXT,
                current_version INTEGER NOT NULL DEFAULT 1,
                locked_by TEXT,
                locked_at REAL,
                created_at REAL NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS document_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                label TEXT,
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(document_id, version_number),
                FOREIGN KEY(document_id) REFERENCES documents(id),
                FOREIGN KEY(file_id) REFERENCES files(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS share_links (
                token TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                permission TEXT NOT NULL DEFAULT 'view',
                expires_at REAL,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY(file_id) REFERENCES files(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                author_id TEXT,
                author_label TEXT NOT NULL,
                text TEXT NOT NULL,
                mentions TEXT,
                page INTEGER,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY(file_id) REFERENCES files(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS document_collaborators (
                document_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                permission TEXT NOT NULL DEFAULT 'view',
                added_at REAL NOT NULL,
                PRIMARY KEY (document_id, user_id),
                FOREIGN KEY(document_id) REFERENCES documents(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                owner_id TEXT,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
        """)
        # Sweep stale queued/running jobs left over from a previous crash
        now = time.time()
        c.execute(
            """UPDATE jobs SET status = 'failed',
               error = 'server restarted before job completed',
               finished_at = ?
               WHERE status IN ('queued', 'running') AND created_at < ?""",
            (now, now - 60),
        )


# ---------- users ----------

def create_user(email, password_hash):
    user_id = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email, password_hash, time.time()),
        )
    return user_id


def get_user_by_email(email):
    if not isinstance(email, str):
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id):
    if not isinstance(user_id, str):
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


# ---------- files ----------

def _infer_format_from_id(file_id):
    if not file_id or not isinstance(file_id, str):
        return None
    ext = os.path.splitext(file_id)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext == ".xlsx":
        return "xlsx"
    if ext == ".pptx":
        return "pptx"
    if ext in (".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".webp", ".bmp", ".tiff", ".tif"):
        return "image"
    if ext:
        return ext.lstrip(".")
    return None


def register_file(file_id, owner_id, original_name=None, kind="upload",
                   derived_from=None, size=0, format=None):
    if format is None:
        format = _infer_format_from_id(file_id)
    if original_name is None:
        original_name = file_id
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO files
               (id, owner_id, original_name, kind, format, derived_from, size, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_id, owner_id, original_name, kind, format, derived_from, size, time.time()),
        )


def get_file(file_id):
    if not isinstance(file_id, str):
        raise ValueError("file_id must be a string")
    with _conn() as c:
        row = c.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None


def delete_file(file_id, owner_id):
    if not isinstance(file_id, str):
        raise ValueError("file_id must be a string")
    with _conn() as c:
        row = c.execute("SELECT owner_id FROM files WHERE id = ?", (file_id,)).fetchone()
        if not row or row["owner_id"] != owner_id:
            return False
        c.execute("DELETE FROM annotations WHERE file_id = ?", (file_id,))
        c.execute("DELETE FROM share_links WHERE file_id = ?", (file_id,))
        c.execute("DELETE FROM comments WHERE file_id = ?", (file_id,))
        c.execute("DELETE FROM files WHERE id = ? AND owner_id = ?", (file_id, owner_id))
        return True


def file_used_in_document(file_id):
    with _conn() as c:
        row = c.execute("SELECT 1 FROM document_versions WHERE file_id = ? LIMIT 1", (file_id,)).fetchone()
        return row is not None


def list_files_for_user(owner_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM files WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def user_owns_file(user_id, file_id):
    f = get_file(file_id)
    return f is not None and f["owner_id"] == user_id


_PERM_ORDER = {None: -1, "view": 0, "comment": 1, "edit": 2, "owner": 3}


def get_file_permission(user_id, file_id):
    """Returns the highest permission user_id has on file_id:
    'owner', 'edit', 'comment', 'view', or None.
    Checks direct file ownership first, then all documents containing file_id."""
    f = get_file(file_id)
    if f is None:
        return None
    if f["owner_id"] == user_id:
        return "owner"
    best = None
    with _conn() as c:
        rows = c.execute(
            """SELECT d.owner_id, dc.permission
               FROM document_versions dv
               JOIN documents d ON d.id = dv.document_id
               LEFT JOIN document_collaborators dc
                 ON dc.document_id = d.id AND dc.user_id = ?
               WHERE dv.file_id = ?""",
            (user_id, file_id),
        ).fetchall()
        for r in rows:
            if r["owner_id"] == user_id:
                return "owner"
            perm = r["permission"]
            if perm and _PERM_ORDER.get(perm, -1) > _PERM_ORDER.get(best, -1):
                best = perm
    return best


def user_can_access_file_via_document(user_id, file_id):
    return get_file_permission(user_id, file_id) is not None


# ---------- annotations ----------

def add_annotation(file_id, owner_id, ann_type, data_json, page=None):
    ann_id = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            """INSERT INTO annotations (id, file_id, owner_id, type, data, page, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ann_id, file_id, owner_id, ann_type, data_json, page, time.time()),
        )
    return ann_id


def get_annotation(annotation_id):
    if not isinstance(annotation_id, str):
        raise ValueError("annotation_id must be a string")
    with _conn() as c:
        row = c.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        return dict(row) if row else None


def update_annotation(annotation_id, owner_id, ann_type, data_json, page):
    if not isinstance(annotation_id, str):
        raise ValueError("annotation_id must be a string")
    with _conn() as c:
        cur = c.execute(
            """UPDATE annotations SET type = ?, data = ?, page = ?
               WHERE id = ? AND owner_id = ?""",
            (ann_type, data_json, page, annotation_id, owner_id),
        )
        return cur.rowcount > 0


def list_annotations(file_id):
    if not isinstance(file_id, str):
        raise ValueError("file_id must be a string")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM annotations WHERE file_id = ? ORDER BY created_at ASC",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_annotation(annotation_id, owner_id):
    if not isinstance(annotation_id, str):
        raise ValueError("annotation_id must be a string")
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM annotations WHERE id = ? AND owner_id = ?",
            (annotation_id, owner_id),
        )
        return cur.rowcount > 0


# ---------- documents & version history ----------

def create_document(owner_id, file_id, title=None):
    doc_id = str(uuid.uuid4())
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO documents (id, owner_id, title, current_version, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (doc_id, owner_id, title, now),
        )
        c.execute(
            "INSERT INTO document_versions (document_id, version_number, file_id, "
            "label, created_by, created_at) VALUES (?, 1, ?, ?, ?, ?)",
            (doc_id, file_id, "initial version", owner_id, now),
        )
    return doc_id


def get_document(doc_id):
    if not isinstance(doc_id, str):
        raise ValueError("doc_id must be a string")
    with _conn() as c:
        row = c.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None


def list_documents_for_user(owner_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM documents WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_documents_shared_with(user_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT d.*, dc.permission as your_permission FROM documents d "
            "JOIN document_collaborators dc ON dc.document_id = d.id "
            "WHERE dc.user_id = ? ORDER BY d.created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_document_version(doc_id, file_id, created_by, label=None):
    for _ in range(5):
        try:
            with _conn() as c:
                row = c.execute(
                    "SELECT MAX(version_number) as m FROM document_versions WHERE document_id = ?",
                    (doc_id,),
                ).fetchone()
                next_version = (row["m"] or 0) + 1
                now = time.time()
                c.execute(
                    "INSERT INTO document_versions (document_id, version_number, file_id, "
                    "label, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, next_version, file_id, label, created_by, now),
                )
                c.execute(
                    "UPDATE documents SET current_version = ? WHERE id = ?",
                    (next_version, doc_id),
                )
            return next_version
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("concurrent version update conflict")


def list_document_versions(doc_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version_number ASC",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_document_version(doc_id, version_number):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM document_versions WHERE document_id = ? AND version_number = ?",
            (doc_id, version_number),
        ).fetchone()
        return dict(row) if row else None


def is_document_locked_by_other(doc_id, user_id):
    """Returns True if doc_id currently has an active (unexpired) lock held by another user."""
    now = time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT locked_by, locked_at FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if not row or not row["locked_by"]:
            return False
        if row["locked_at"] and (now - row["locked_at"]) >= LOCK_TTL:
            return False
        return row["locked_by"] != user_id


def set_document_lock(doc_id, user_id):
    """Atomic lock acquisition with TTL expiration support."""
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            """UPDATE documents SET locked_by = ?, locked_at = ?
               WHERE id = ? AND (locked_by IS NULL OR locked_by = ? OR locked_at < ?)""",
            (user_id, now, doc_id, user_id, now - LOCK_TTL),
        )
        return cur.rowcount == 1


def release_document_lock(doc_id, user_id):
    """Releases an active lock if held by user_id OR if user_id is the document owner."""
    now = time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT owner_id, locked_by, locked_at FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if not row or not row["locked_by"]:
            return False
        if row["locked_at"] and (now - row["locked_at"]) >= LOCK_TTL:
            c.execute("UPDATE documents SET locked_by = NULL, locked_at = NULL WHERE id = ?", (doc_id,))
            return False
        if row["locked_by"] != user_id and row["owner_id"] != user_id:
            return False
        c.execute(
            "UPDATE documents SET locked_by = NULL, locked_at = NULL WHERE id = ?",
            (doc_id,),
        )
        return True


# ---------- document collaborators ----------

def add_collaborator(doc_id, user_id, permission="view"):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO document_collaborators (document_id, user_id, "
            "permission, added_at) VALUES (?, ?, ?, ?)",
            (doc_id, user_id, permission, time.time()),
        )


def remove_collaborator(doc_id, user_id):
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM document_collaborators WHERE document_id = ? AND user_id = ?",
            (doc_id, user_id),
        )
        c.execute(
            "UPDATE documents SET locked_by = NULL, locked_at = NULL WHERE id = ? AND locked_by = ?",
            (doc_id, user_id),
        )
        return cur.rowcount > 0


def get_collaborator_permission(doc_id, user_id):
    with _conn() as c:
        row = c.execute(
            "SELECT permission FROM document_collaborators WHERE document_id = ? AND user_id = ?",
            (doc_id, user_id),
        ).fetchone()
        return row["permission"] if row else None


def list_collaborators(doc_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT dc.user_id, dc.permission, dc.added_at, u.email FROM document_collaborators dc "
            "JOIN users u ON u.id = dc.user_id WHERE dc.document_id = ? ORDER BY dc.added_at ASC",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- sharing ----------

def create_share_link(file_id, owner_id, permission="view", expires_at=None):
    token = uuid.uuid4().hex
    with _conn() as c:
        c.execute(
            "INSERT INTO share_links (token, file_id, owner_id, permission, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token, file_id, owner_id, permission, expires_at, time.time()),
        )
    return token


def get_share_link(token):
    if not isinstance(token, str):
        raise ValueError("token must be a string")
    with _conn() as c:
        row = c.execute("SELECT * FROM share_links WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None


def revoke_share_link(token, owner_id):
    if not isinstance(token, str):
        raise ValueError("token must be a string")
    with _conn() as c:
        cur = c.execute(
            "UPDATE share_links SET revoked = 1 WHERE token = ? AND owner_id = ?",
            (token, owner_id),
        )
        return cur.rowcount > 0


def list_share_links_for_file(file_id):
    if not isinstance(file_id, str):
        raise ValueError("file_id must be a string")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM share_links WHERE file_id = ? ORDER BY created_at DESC",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- comments ----------

def add_comment(file_id, author_id, author_label, text, mentions_json=None, page=None):
    comment_id = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            "INSERT INTO comments (id, file_id, author_id, author_label, text, "
            "mentions, page, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (comment_id, file_id, author_id, author_label, text, mentions_json,
             page, time.time()),
        )
    return comment_id


def get_comment(comment_id):
    if not isinstance(comment_id, str):
        raise ValueError("comment_id must be a string")
    with _conn() as c:
        row = c.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        return dict(row) if row else None


def list_comments(file_id):
    if not isinstance(file_id, str):
        raise ValueError("file_id must be a string")
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM comments WHERE file_id = ? ORDER BY created_at ASC",
            (file_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_comment(comment_id):
    if not isinstance(comment_id, str):
        raise ValueError("comment_id must be a string")
    with _conn() as c:
        cur = c.execute("UPDATE comments SET resolved = 1 WHERE id = ?", (comment_id,))
        return cur.rowcount > 0


def delete_comment(comment_id, author_id):
    if not isinstance(comment_id, str):
        raise ValueError("comment_id must be a string")
    with _conn() as c:
        cur = c.execute(
            """DELETE FROM comments WHERE id = ? AND (
                   author_id = ?
                   OR file_id IN (SELECT id FROM files WHERE owner_id = ?)
               )""",
            (comment_id, author_id, author_id),
        )
        return cur.rowcount > 0


# ---------- persistent jobs (multi-worker safe + TTL eviction) ----------

def create_job(job_id, owner_id, created_at):
    cutoff = time.time() - JOB_TTL
    with _conn() as c:
        c.execute("DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
        c.execute(
            "INSERT INTO jobs (id, owner_id, status, result_json, error, created_at) VALUES (?, ?, ?, NULL, NULL, ?)",
            (job_id, owner_id, "queued", created_at),
        )


def update_job_running(job_id, started_at):
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (started_at, job_id),
        )


def update_job_done(job_id, result, finished_at):
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET status = 'done', result_json = ?, finished_at = ? WHERE id = ?",
            (json.dumps(result), finished_at, job_id),
        )


def update_job_failed(job_id, error_msg, finished_at):
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (error_msg, finished_at, job_id),
        )


def get_job(job_id):
    if not isinstance(job_id, str):
        raise ValueError("job_id must be a string")
    cutoff = time.time() - JOB_TTL
    with _conn() as c:
        c.execute("DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        result = json.loads(d["result_json"]) if d.get("result_json") is not None else None
        return {
            "id": d["id"],
            "owner_id": d["owner_id"],
            "status": d["status"],
            "result": result,
            "error": d["error"],
            "created_at": d["created_at"],
            "started_at": d["started_at"],
            "finished_at": d["finished_at"],
        }
