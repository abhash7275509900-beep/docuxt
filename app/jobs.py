"""
Async job queue backed by ThreadPoolExecutor and persisted in SQLite (`jobs` table)
so multi-worker deployments share job status and finished jobs are evicted after a TTL.
Tracebacks and server filesystem paths are scrubbed from user-facing job errors.
"""
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)
_jobs = {}
_lock = Lock()

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Matches multi-segment absolute Unix paths like /tmp/foo/bar.pdf or /working_dir/.../storage/uploads/x.txt
_ABS_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._\-+]+){2,}(?::|\b)")


def _sanitize_error_message(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    msg = str(exc) or exc.__class__.__name__
    msg = _ABS_PATH_RE.sub("<file>", msg)
    msg = msg.replace("<file>:", "").strip()
    if not msg:
        msg = "operation failed"
    return msg


def _extract_file_ids(result):
    if result is None:
        return []
    if isinstance(result, str):
        return [result]
    if isinstance(result, dict) and "file_id" in result:
        return [result["file_id"]]
    if isinstance(result, list):
        out = []
        for item in result:
            out.extend(_extract_file_ids(item))
        return out
    return []


def _evict_in_memory(now: float, ttl: float = 3600.0):
    expired = [
        jid for jid, j in _jobs.items()
        if j.get("finished_at") and (now - j["finished_at"]) > ttl
    ]
    for jid in expired:
        _jobs.pop(jid, None)


def _run(job_id, fn, kwargs, owner_id, source_file_id):
    from app import db
    started_at = time.time()
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = STATUS_RUNNING
            _jobs[job_id]["started_at"] = started_at
    try:
        db.update_job_running(job_id, started_at)
    except Exception:
        pass

    try:
        result = fn(**kwargs)
        if owner_id:
            for fid in _extract_file_ids(result):
                db.register_file(fid, owner_id, kind="derived",
                                  derived_from=source_file_id, size=_safe_size(fid))
        finished_at = time.time()
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["status"] = STATUS_DONE
                _jobs[job_id]["result"] = result
                _jobs[job_id]["finished_at"] = finished_at
            _evict_in_memory(finished_at, db.JOB_TTL)
        try:
            db.update_job_done(job_id, result, finished_at)
        except Exception:
            pass
    except Exception as e:
        logger.warning("Job %s failed: %s", job_id, e)
        safe_err = _sanitize_error_message(e)
        finished_at = time.time()
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["status"] = STATUS_FAILED
                _jobs[job_id]["error"] = safe_err
                _jobs[job_id]["finished_at"] = finished_at
            _evict_in_memory(finished_at, db.JOB_TTL)
        try:
            db.update_job_failed(job_id, safe_err, finished_at)
        except Exception:
            pass


def _safe_size(file_id):
    try:
        from app import storage
        return storage.size_of(file_id)
    except Exception:
        return 0


def submit(fn, owner_id=None, source_file_id=None, **kwargs):
    from app import db
    job_id = str(uuid.uuid4())
    now = time.time()
    with _lock:
        _evict_in_memory(now, db.JOB_TTL)
        _jobs[job_id] = {
            "id": job_id,
            "owner_id": owner_id,
            "status": STATUS_QUEUED,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
    try:
        db.create_job(job_id, owner_id, now)
    except Exception:
        pass
    _executor.submit(_run, job_id, fn, kwargs, owner_id, source_file_id)
    return job_id


def get(job_id):
    from app import db
    now = time.time()
    with _lock:
        _evict_in_memory(now, db.JOB_TTL)
        mem_job = _jobs.get(job_id)
        if mem_job is not None and mem_job["status"] in (STATUS_DONE, STATUS_FAILED):
            return dict(mem_job)
    try:
        db_job = db.get_job(job_id)
        if db_job is not None:
            return db_job
    except Exception:
        pass
    with _lock:
        mem_job = _jobs.get(job_id)
        return dict(mem_job) if mem_job else None
