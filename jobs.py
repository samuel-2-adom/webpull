"""
WebPull - Job Manager
=====================
Manages background scrape jobs.
Each job gets a unique ID, runs in the background,
and stores its results so the UI can poll for status.

No Redis, no Celery — just FastAPI BackgroundTasks
and in-memory + file storage. Simple and deployable
on Oracle free tier with zero extra services.
"""

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from scraper import JobLogger, run_scrape, detect_site


# ============================================================
# JOB STATES
# ============================================================

STATE_PENDING   = "pending"
STATE_DETECTING = "detecting"
STATE_RUNNING   = "running"
STATE_DONE      = "done"
STATE_FAILED    = "failed"


# ============================================================
# IN-MEMORY JOB STORE
# Jobs live in memory while running, then get persisted to disk
# ============================================================

_jobs: Dict[str, dict] = {}

RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(exist_ok=True)


# ============================================================
# JOB CREATION
# ============================================================

def create_job(job_config: dict) -> str:
    """
    Create a new job and return its ID.
    The actual scraping runs in the background.
    """

    job_id = str(uuid.uuid4())[:8]  # short but unique enough

    _jobs[job_id] = {
        "id"        : job_id,
        "url"       : job_config.get("url", ""),
        "state"     : STATE_PENDING,
        "type"      : job_config.get("job_type", "scrape"),  # "scrape" | "detect"
        "created_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "log"       : [],
        "result"    : None,
        "error"     : None,
        "config"    : job_config,
    }

    return job_id


# ============================================================
# JOB EXECUTION
# ============================================================

def run_job(job_id: str):
    """
    Execute a scrape job.
    Called by FastAPI BackgroundTasks — runs in the background
    so the HTTP response returns immediately.
    """

    if job_id not in _jobs:
        return

    job    = _jobs[job_id]
    logger = JobLogger()

    try:
        job["state"] = STATE_RUNNING
        config       = job["config"]

        # Run the scraper
        result = run_scrape(config, logger)

        # Store result
        job["log"]         = logger.entries
        job["result"]      = result
        job["finished_at"] = datetime.utcnow().isoformat()

        if result.get("success"):
            job["state"] = STATE_DONE
            _persist_job(job_id, result)
        else:
            job["state"] = STATE_FAILED
            job["error"] = "Scrape failed — check the job log for details."

    except Exception as exc:
        job["state"]       = STATE_FAILED
        job["error"]       = str(exc)
        job["log"]         = logger.entries
        job["finished_at"] = datetime.utcnow().isoformat()


def run_detection(job_id: str, url: str):
    """
    Run site detection as a background task.
    Stores the detection result in the job so the UI
    can show what was found without blocking.
    """

    if job_id not in _jobs:
        return

    job    = _jobs[job_id]
    logger = JobLogger()

    job["state"] = STATE_DETECTING

    try:
        detection = detect_site(url, logger)

        job["detection"] = detection
        job["log"]       = logger.entries
        job["state"]     = STATE_PENDING  # back to pending, waiting for user config

    except Exception as exc:
        job["state"] = STATE_FAILED
        job["error"] = str(exc)
        job["log"]   = logger.entries


# ============================================================
# PERSIST TO DISK
# ============================================================

def _persist_job(job_id: str, result: dict):
    """
    Save completed job outputs to disk so they survive restarts.
    """

    job_dir = RESULTS_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    if result.get("json"):
        (job_dir / "results.json").write_text(
            result["json"], encoding="utf-8"
        )

    if result.get("csv"):
        (job_dir / "results.csv").write_text(
            result["csv"], encoding="utf-8"
        )

    if result.get("txt"):
        (job_dir / "results.txt").write_text(
            result["txt"], encoding="utf-8"
        )

    # Save a job metadata file
    meta = {
        "id"         : job_id,
        "url"        : _jobs[job_id]["url"],
        "created_at" : _jobs[job_id]["created_at"],
        "finished_at": _jobs[job_id]["finished_at"],
        "pages"      : len(result.get("results", [])),
    }

    (job_dir / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


# ============================================================
# JOB QUERIES
# ============================================================

def get_job(job_id: str) -> Optional[dict]:
    """Get job state (without full result data to keep it light)."""

    if job_id not in _jobs:
        # Try loading from disk
        return _load_from_disk(job_id)

    job = _jobs[job_id]

    return {
        "id"         : job["id"],
        "url"        : job["url"],
        "state"      : job["state"],
        "created_at" : job["created_at"],
        "finished_at": job.get("finished_at"),
        "log"        : job.get("log", []),
        "error"      : job.get("error"),
        "detection"  : job.get("detection"),
        "has_results": (job.get("result") or {}).get("success", False),
    }


def get_all_jobs() -> list:
    """Get a summary list of all jobs (for the history page)."""

    jobs = []

    # In-memory jobs — only show scrape jobs, not detection jobs
    for job in _jobs.values():
        if job.get("type") == "detect":
            continue
        jobs.append({
            "id"         : job["id"],
            "url"        : job["url"],
            "state"      : job["state"],
            "created_at" : job["created_at"],
            "finished_at": job.get("finished_at"),
            "has_results": (job.get("result") or {}).get("success", False),
        })

    # Add any disk-only jobs not in memory
    for job_dir in RESULTS_DIR.iterdir():
        if job_dir.is_dir() and job_dir.name not in _jobs:
            meta_file = job_dir / "meta.json"
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                jobs.append({
                    "id"         : meta["id"],
                    "url"        : meta["url"],
                    "state"      : STATE_DONE,
                    "created_at" : meta["created_at"],
                    "finished_at": meta.get("finished_at"),
                    "has_results": True,
                })

    # Sort newest first
    jobs.sort(
        key=lambda j: j.get("created_at", ""),
        reverse=True,
    )

    return jobs


def get_result_file(job_id: str, fmt: str) -> Optional[Path]:
    """
    Return the path to a result file for download.
    fmt = "json" | "csv" | "txt"
    """

    # Check in-memory first
    if job_id in _jobs:
        job = _jobs[job_id]
        if job.get("result", {}).get("success"):
            job_dir = RESULTS_DIR / job_id
            path    = job_dir / f"results.{fmt}"
            if path.exists():
                return path

    # Check disk
    path = RESULTS_DIR / job_id / f"results.{fmt}"
    if path.exists():
        return path

    return None


def _load_from_disk(job_id: str) -> Optional[dict]:
    """Load job metadata from disk if not in memory."""

    meta_file = RESULTS_DIR / job_id / "meta.json"

    if not meta_file.exists():
        return None

    meta = json.loads(meta_file.read_text())

    return {
        "id"         : meta["id"],
        "url"        : meta["url"],
        "state"      : STATE_DONE,
        "created_at" : meta["created_at"],
        "finished_at": meta.get("finished_at"),
        "log"        : [],
        "error"      : None,
        "detection"  : None,
        "has_results": True,
    }
