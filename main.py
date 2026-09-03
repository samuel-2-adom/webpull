"""
WebPull - FastAPI Backend
=========================
The web layer. Handles HTTP requests from the UI,
kicks off background jobs, and serves results.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import json
from pathlib import Path
import re

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv

load_dotenv()

import jobs as job_manager
from scraper import detect_site, JobLogger, normalize_url

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"


# ============================================================
# APP
# ============================================================

# ============================================================
# METRICS — simple file-based counter
# ============================================================

METRICS_FILE = Path("metrics.json")

def _load_metrics():
    if METRICS_FILE.exists():
        try:
            return json.loads(METRICS_FILE.read_text())
        except Exception:
            pass
    return {"total_scrapes": 0, "total_urls": set()}

def _save_metrics(m):
    # Convert set to list for JSON serialisation
    data = {
        "total_scrapes": m["total_scrapes"],
        "total_urls"   : list(m.get("total_urls", [])),
    }
    METRICS_FILE.write_text(json.dumps(data))

def increment_metrics(url: str):
    m = _load_metrics()
    m["total_scrapes"] = m.get("total_scrapes", 0) + 1
    urls = set(m.get("total_urls", []))
    urls.add(url)
    m["total_urls"] = urls
    _save_metrics(m)


app = FastAPI(
    title="WebPull",
    description="Universal web scraper with a clean UI.",
    version="1.0.0",
)

# Serve the frontend from /static
app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static",
)


# ============================================================
# REQUEST MODELS
# ============================================================

class DetectRequest(BaseModel):
    url: str


class FormatAssistRequest(BaseModel):
    sample: List[Any]


class ScrapeRequest(BaseModel):
    url             : str
    auth_method     : str = "none"        # none | nextauth | wordpress | html_form
    username        : Optional[str] = None
    password        : Optional[str] = None
    username_field  : str = "email"
    password_field  : str = "password"
    login_url       : Optional[str] = None
    extract_modes   : List[str] = ["title"]
    selector        : Optional[str] = None
    container       : Optional[str] = None
    fields          : Optional[Dict[str, str]] = None
    pages           : int = 1
    next_selector   : Optional[str] = None
    delay           : float = 0.5
    timeout         : int = 20


# ============================================================
# ROUTES — FRONTEND
# ============================================================

@app.get("/")
async def serve_index():
    """Serve the main UI."""
    return FileResponse("static/index.html")


# ============================================================
# ROUTES — DETECTION
# ============================================================

@app.post("/api/detect")
async def detect_endpoint(
    request: DetectRequest,
    background_tasks: BackgroundTasks,
):
    """
    Detect the site type for a given URL.
    Returns a job ID immediately.
    The detection runs in the background.
    Poll /api/jobs/{job_id} for results.
    """

    url = normalize_url(request.url)

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL."
        )

    # Create a detection-only job (excluded from history)
    job_id = job_manager.create_job({"url": url, "job_type": "detect"})

    background_tasks.add_task(
        job_manager.run_detection,
        job_id,
        url,
    )

    return {"job_id": job_id}


# ============================================================
# ROUTES — SCRAPING
# ============================================================

@app.post("/api/scrape")
async def scrape_endpoint(
    request: ScrapeRequest,
    background_tasks: BackgroundTasks,
):
    """
    Start a scrape job.
    Returns a job ID immediately — scraping runs in background.
    Poll /api/jobs/{job_id} for status and logs.
    """

    url = normalize_url(request.url)

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL."
        )

    # Build the job config
    job_config = {
        "url": url,

        "auth": {
            "method"        : request.auth_method,
            "username"      : request.username or "",
            "password"      : request.password or "",
            "username_field": request.username_field,
            "password_field": request.password_field,
            "login_url"     : request.login_url or url,
        },

        "extract": {
            "modes"    : request.extract_modes,
            "selector" : request.selector or "",
            "container": request.container or "",
            "fields"   : request.fields or {},
        },

        "settings": {
            "pages"        : request.pages,
            "next_selector": request.next_selector or "",
            "delay"        : request.delay,
            "timeout"      : request.timeout,
            "cache_dir"    : None,
        },
    }

    job_id = job_manager.create_job(job_config)

    background_tasks.add_task(
        job_manager.run_job,
        job_id,
    )

    # Track metrics
    increment_metrics(url)

    return {"job_id": job_id}


# ============================================================
# ROUTES — JOB STATUS
# ============================================================

@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """
    Get the current status of a job.
    The UI polls this every 1.5 seconds to show live progress.
    """

    job = job_manager.get_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found."
        )

    return job


@app.get("/api/jobs")
async def list_jobs():
    """Get all jobs for the history page."""
    return job_manager.get_all_jobs()


# ============================================================
# ROUTES — DOWNLOADS
# ============================================================

@app.get("/api/jobs/{job_id}/download/{fmt}")
async def download_result(job_id: str, fmt: str):
    """
    Download a result file.
    fmt = json | csv | txt
    """

    if fmt not in {"json", "csv", "txt"}:
        raise HTTPException(
            status_code=400,
            detail="Format must be json, csv, or txt."
        )

    path = job_manager.get_result_file(job_id, fmt)

    if not path:
        raise HTTPException(
            status_code=404,
            detail="Result file not found. Job may still be running."
        )

    media_types = {
        "json": "application/json",
        "csv" : "text/csv",
        "txt" : "text/plain",
    }

    return FileResponse(
        path=path,
        media_type=media_types[fmt],
        filename=f"webpull_{job_id}.{fmt}",
    )


# ============================================================
# ROUTES — AI FORMAT ASSIST (Groq)
# ============================================================

@app.post("/api/format-assist")
async def format_assist(request: FormatAssistRequest):
    """
    Send a sample of scraped data to Groq.
    Groq identifies the data type and suggests clean column names.
    Returns formatted rows for the table preview.
    """

    if not GROQ_API_KEY:
        return {"success": False, "reason": "No Groq API key configured."}

    if not request.sample:
        return {"success": False, "reason": "No data to analyse."}

    # Clean internal fields before sending to AI
    clean_sample = []
    for item in request.sample:
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        clean_sample.append(clean)

    prompt = f"""You are a data analyst. A web scraper just collected this data.
Look at it carefully and figure out what it represents.

Raw scraped data (sample):
{json.dumps(clean_sample, indent=2, ensure_ascii=False)[:3000]}

Respond ONLY with a valid JSON object. No explanation, no markdown, no backticks.
The JSON must have exactly these fields:

{{
  "data_type": "short description of what this data is (e.g. product listing, news articles, job listings)",
  "columns": ["Column1", "Column2", "Column3"],
  "rows": [
    {{"Column1": "value", "Column2": "value"}},
    ...
  ]
}}

Rules:
- columns must be clean, human-readable names (Title Case, no underscores)
- rows must contain the actual data from the sample, reorganised under your column names
- if data is nested (e.g. lists of links or images), flatten it — one row per item
- maximum 20 rows in your response
- only include columns that have real data
- respond with JSON only, nothing else"""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type" : "application/json",
                },
                json={
                    "model"      : GROQ_MODEL,
                    "messages"   : [{"role": "user", "content": prompt}],
                    "max_tokens" : 1500,
                    "temperature": 0.1,
                },
            )

            if res.status_code != 200:
                return {"success": False, "reason": f"Groq API error: {res.status_code}"}

            content = res.json()["choices"][0]["message"]["content"].strip()

            # Strip any accidental markdown fences
            content = content.replace("```json", "").replace("```", "").strip()

            parsed = json.loads(content)

            return {
                "success"  : True,
                "data_type": parsed.get("data_type", ""),
                "columns"  : parsed.get("columns", []),
                "rows"     : parsed.get("rows", []),
            }

    except json.JSONDecodeError:
        return {"success": False, "reason": "AI returned invalid JSON."}

    except Exception as exc:
        return {"success": False, "reason": str(exc)}


# ============================================================
# ROUTES — METRICS
# ============================================================

@app.get("/api/metrics")
async def get_metrics():
    """Return scrape usage stats for the UI counter."""
    m = _load_metrics()
    return {
        "total_scrapes" : m.get("total_scrapes", 0),
        "unique_urls"   : len(m.get("total_urls", [])),
    }


# ============================================================
# ROUTES — AI SUMMARY
# ============================================================

class SummaryRequest(BaseModel):
    job_id: str


@app.post("/api/summarise")
async def summarise(request: SummaryRequest):
    """
    Send a smart sample of the scraped data to Groq
    and return a plain-English summary.

    We send stats + 10 rows max to keep tokens low.
    """

    if not GROQ_API_KEY:
        return {"success": False, "summary": "No Groq API key configured."}

    path = job_manager.get_result_file(request.job_id, "json")

    if not path:
        return {"success": False, "summary": "Result file not found."}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"success": False, "summary": f"Could not read result: {exc}"}

    if not data:
        return {"success": False, "summary": "No data to summarise."}

    # Build a smart sample — stats + 10 rows, not the whole file
    # This keeps input tokens under ~2000 regardless of dataset size
    total_pages = len(data)
    sample_page = data[0]
    clean_page  = {k: v for k, v in sample_page.items() if not k.startswith("_")}

    # Count total records across all pages
    total_records = 0
    for page in data:
        for key, val in page.items():
            if isinstance(val, list) and len(val) > total_records:
                total_records = len(val)

    # Get a sample of actual rows
    sample_rows = []
    for page in data:
        for key, val in page.items():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                sample_rows = val[:10]
                break
        if sample_rows:
            break

    prompt = f"""You are a data analyst. A web scraper collected this data.
Write a SHORT, clear, plain-English summary of what was found.

Stats:
- Pages scraped: {total_pages}
- Total records found: {total_records if total_records > 0 else "unknown"}
- URL: {sample_page.get("_url", "unknown")}

Sample data (first 10 records or page overview):
{json.dumps(sample_rows if sample_rows else clean_page, indent=2, ensure_ascii=False)[:2000]}

Write 2-4 sentences summarising:
1. What type of data this is
2. Key numbers or patterns you notice
3. Anything interesting or notable

Be specific. Use actual numbers from the data. Keep it under 100 words."""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type" : "application/json",
                },
                json={
                    "model"      : GROQ_MODEL,
                    "messages"   : [{"role": "user", "content": prompt}],
                    "max_tokens" : 200,
                    "temperature": 0.3,
                },
            )

            if res.status_code != 200:
                return {"success": False, "summary": "Groq API error."}

            summary = res.json()["choices"][0]["message"]["content"].strip()
            return {"success": True, "summary": summary}

    except Exception as exc:
        return {"success": False, "summary": f"AI unavailable: {exc}"}


# ============================================================
# ROUTES — PROFILES
# ============================================================

@app.get("/api/profiles")
async def list_profiles():
    """List available site profiles."""
    profiles_dir = Path("profiles")
    profiles     = []

    if profiles_dir.exists():
        for f in profiles_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                profiles.append(data)
            except Exception:
                pass

    return profiles


# ============================================================
# ROUTES — USER PROFILE CRUD
# ============================================================

class ProfileSaveRequest(BaseModel):
    name            : str
    description     : str = ""
    url             : str
    auth_method     : str = "none"
    username_field  : str = "email"
    password_field  : str = "password"
    login_url       : str = ""
    extract_modes   : List[str] = ["title"]
    selector        : str = ""
    container       : str = ""
    fields          : Dict[str, str] = {}
    pages           : int = 1
    next_selector   : str = ""
    delay           : float = 0.5
    timeout         : int = 20
    tags            : List[str] = []


@app.post("/api/profiles")
async def save_profile(request: ProfileSaveRequest):
    """Save a user-created profile to disk."""

    profiles_dir = Path("profiles")
    profiles_dir.mkdir(exist_ok=True)

    # Sanitise name for filename
    safe_name = re.sub(r"[^a-z0-9_-]", "_", request.name.lower().strip())
    safe_name = safe_name[:40] or "profile"

    # Avoid overwriting — append number if exists
    base     = safe_name
    counter  = 1
    filename = f"{safe_name}.json"

    while (profiles_dir / filename).exists():
        filename = f"{base}_{counter}.json"
        counter += 1

    profile_data = {
        "id"           : safe_name,
        "name"         : request.name.strip(),
        "description"  : request.description.strip(),
        "url"          : request.url,
        "auth"         : {
            "method"        : request.auth_method,
            "username_field": request.username_field,
            "password_field": request.password_field,
            "login_url"     : request.login_url,
        },
        "extract"      : {
            "modes"    : request.extract_modes,
            "selector" : request.selector,
            "container": request.container,
            "fields"   : request.fields,
        },
        "settings"     : {
            "pages"        : request.pages,
            "next_selector": request.next_selector,
            "delay"        : request.delay,
            "timeout"      : request.timeout,
        },
        "tags"         : request.tags,
        "user_created" : True,
        "created_at"   : __import__("datetime").datetime.utcnow().isoformat(),
    }

    (profiles_dir / filename).write_text(
        json.dumps(profile_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {"success": True, "id": safe_name, "filename": filename}


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    """Delete a user-created profile."""

    profiles_dir = Path("profiles")

    # Find the file
    for f in profiles_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("id") == profile_id and data.get("user_created"):
                f.unlink()
                return {"success": True}
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="Profile not found.")


@app.post("/api/profiles/{profile_id}/run")
async def run_profile(
    profile_id : str,
    body       : dict,
    background_tasks: BackgroundTasks,
):
    """
    Run a saved profile.
    Body should contain credentials: { username, password }
    Everything else comes from the saved profile config.
    """

    profiles_dir = Path("profiles")
    profile      = None

    for f in profiles_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("id") == profile_id:
                profile = data
                break
        except Exception:
            pass

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    url = normalize_url(profile.get("url", ""))
    if not url:
        raise HTTPException(status_code=400, detail="Profile has no valid URL.")

    auth         = profile.get("auth", {})
    extract      = profile.get("extract", {})
    settings     = profile.get("settings", {})

    job_config = {
        "url"     : url,
        "job_type": "scrape",
        "auth"    : {
            "method"        : auth.get("method", "none"),
            "username"      : body.get("username", ""),
            "password"      : body.get("password", ""),
            "username_field": auth.get("username_field", "email"),
            "password_field": auth.get("password_field", "password"),
            "login_url"     : auth.get("login_url", url),
        },
        "extract" : {
            "modes"    : extract.get("modes", ["title"]),
            "selector" : extract.get("selector", ""),
            "container": extract.get("container", ""),
            "fields"   : extract.get("fields", {}),
        },
        "settings": {
            "pages"        : settings.get("pages", 1),
            "next_selector": settings.get("next_selector", ""),
            "delay"        : settings.get("delay", 0.5),
            "timeout"      : settings.get("timeout", 20),
            "cache_dir"    : None,
        },
    }

    job_id = job_manager.create_job(job_config)
    increment_metrics(url)

    background_tasks.add_task(job_manager.run_job, job_id)

    return {"job_id": job_id}




# ============================================================
# ROUTES — HEALTH CHECK
# ============================================================

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}
