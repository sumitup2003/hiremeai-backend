from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import os
import httpx
import asyncio

from supabase import create_client, Client

# ── Supabase client ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="HireMe AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your Vercel URL after deploy
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Pydantic models ────────────────────────────────────────────────────────────
class Job(BaseModel):
    id:               Optional[str] = None
    title:            str
    company:          str
    location:         str
    job_type:         Optional[str] = None
    source:           str
    url:              str
    description:      Optional[str] = None
    fit_score:        int
    match_reason:     Optional[str] = None
    missing_skills:   Optional[str] = None
    status:           Optional[str] = "pending"   # pending | emailed | manual_apply
    recruiter_email:  Optional[str] = None
    recruiter_name:   Optional[str] = None
    recruiter_title:  Optional[str] = None
    recruiter_linkedin: Optional[str] = None
    applied_at:       Optional[str] = None

class EmailLog(BaseModel):
    job_id:          str
    to_email:        str
    subject:         str
    body:            str
    sent_at:         Optional[str] = None
    status:          Optional[str] = "sent"

class RecruiterResponse(BaseModel):
    id:              Optional[str] = None
    job_id:          Optional[str] = None
    from_email:      str
    company:         str
    subject:         str
    body:            str
    received_at:     Optional[str] = None
    response_type:   Optional[str] = "neutral"   # positive | neutral | rejection
    read:            Optional[bool] = False

class WorkflowRun(BaseModel):
    total_searched:  int
    matched:         int
    emailed:         int
    run_at:          Optional[str] = None


# ── Self-ping keepalive (prevents Render free tier from sleeping) ──────────────
async def _keepalive():
    """Pings /health every 10 min so Render free tier never idles."""
    await asyncio.sleep(60)
    self_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not self_url:
        return
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.get(f"{self_url}/health", timeout=10)
            except Exception:
                pass
            await asyncio.sleep(600)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_keepalive())

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "HireMe AI API running", "version": "1.0.0"}

@app.get("/health")
def health():
    return {"ok": True, "timestamp": datetime.utcnow().isoformat()}

# ── Jobs ───────────────────────────────────────────────────────────────────────
@app.post("/jobs", status_code=201)
def create_job(job: Job):
    """n8n calls this after scoring each job to save it."""
    data = job.dict(exclude_none=True)
    data.setdefault("applied_at", datetime.utcnow().isoformat())
    data.setdefault("status", "pending")

    # Upsert by URL to avoid duplicates across runs
    result = (
        supabase.table("jobs")
        .upsert(data, on_conflict="url")
        .execute()
    )
    return {"saved": True, "data": result.data}

@app.get("/jobs")
def list_jobs(
    min_score:  int = Query(0,   ge=0, le=100),
    status:     Optional[str] = None,
    source:     Optional[str] = None,
    limit:      int = Query(50,  ge=1, le=200),
    offset:     int = Query(0,   ge=0),
):
    """Frontend fetches this to render the jobs list."""
    q = (
        supabase.table("jobs")
        .select("*")
        .gte("fit_score", min_score)
        .order("fit_score", desc=True)
        .order("applied_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if status:
        q = q.eq("status", status)
    if source:
        q = q.eq("source", source)

    result = q.execute()
    return {"jobs": result.data, "total": len(result.data)}

@app.patch("/jobs/{job_id}/status")
def update_job_status(job_id: str, status: str):
    """Frontend calls this when user manually updates a job status."""
    allowed = {"pending", "emailed", "manual_apply", "rejected", "interview"}
    if status not in allowed:
        raise HTTPException(400, f"status must be one of {allowed}")
    result = (
        supabase.table("jobs")
        .update({"status": status})
        .eq("id", job_id)
        .execute()
    )
    return {"updated": True, "data": result.data}

@app.get("/jobs/stats")
def job_stats():
    """Dashboard stat cards."""
    all_jobs  = supabase.table("jobs").select("fit_score, status, applied_at").execute().data
    runs      = supabase.table("workflow_runs").select("*").order("run_at", desc=True).limit(1).execute().data

    total     = len(all_jobs)
    matched   = sum(1 for j in all_jobs if j["fit_score"] >= 60)
    emailed   = sum(1 for j in all_jobs if j["status"] == "emailed")
    responses = supabase.table("recruiter_responses").select("id").execute().data

    last_run  = runs[0]["run_at"] if runs else None
    return {
        "total_searched": total,
        "matched":        matched,
        "emailed":        emailed,
        "responses":      len(responses),
        "last_run":       last_run,
    }

# ── Emails ─────────────────────────────────────────────────────────────────────
@app.post("/emails", status_code=201)
def log_email(email: EmailLog):
    """n8n calls this after sending each cold email."""
    data = email.dict(exclude_none=True)
    data.setdefault("sent_at", datetime.utcnow().isoformat())

    supabase.table("email_logs").insert(data).execute()

    # Also update the job status to 'emailed'
    supabase.table("jobs").update({"status": "emailed"}).eq("id", email.job_id).execute()

    return {"logged": True}

@app.get("/emails")
def list_emails(job_id: Optional[str] = None, limit: int = 50):
    q = supabase.table("email_logs").select("*").order("sent_at", desc=True).limit(limit)
    if job_id:
        q = q.eq("job_id", job_id)
    return {"emails": q.execute().data}

# ── Recruiter Responses ────────────────────────────────────────────────────────
@app.post("/responses", status_code=201)
def save_response(resp: RecruiterResponse):
    """n8n Gmail monitor node calls this when a recruiter replies."""
    data = resp.dict(exclude_none=True)
    data.setdefault("received_at", datetime.utcnow().isoformat())
    supabase.table("recruiter_responses").insert(data).execute()
    return {"saved": True}

@app.get("/responses")
def list_responses(read: Optional[bool] = None, limit: int = 50):
    q = (
        supabase.table("recruiter_responses")
        .select("*")
        .order("received_at", desc=True)
        .limit(limit)
    )
    if read is not None:
        q = q.eq("read", read)
    return {"responses": q.execute().data}

@app.patch("/responses/{resp_id}/read")
def mark_read(resp_id: str):
    supabase.table("recruiter_responses").update({"read": True}).eq("id", resp_id).execute()
    return {"marked_read": True}

# ── Workflow Runs ──────────────────────────────────────────────────────────────
@app.post("/runs", status_code=201)
def log_run(run: WorkflowRun):
    """n8n calls this at the end of each daily run as a summary."""
    data = run.dict()
    data.setdefault("run_at", datetime.utcnow().isoformat())
    supabase.table("workflow_runs").insert(data).execute()
    return {"logged": True}

@app.get("/runs")
def list_runs(limit: int = 10):
    result = (
        supabase.table("workflow_runs")
        .select("*")
        .order("run_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"runs": result.data}

# ── Groq proxy — generate cold email on demand from frontend ───────────────────
@app.post("/generate-email")
async def generate_email(payload: dict):
    """
    Frontend sends: { job_title, company, match_reason, recruiter_name }
    Returns:        { subject, body }
    """
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not set")

    prompt = (
        f"Write a professional cold email for a fresher job application.\n"
        f"Job: {payload.get('job_title')} at {payload.get('company')}\n"
        f"Recruiter: {payload.get('recruiter_name', 'Hiring Manager')}\n"
        f"Why I match: {payload.get('match_reason')}\n"
        f"Candidate: {payload.get('candidate_name', 'Sumit Upadhyay')}\n"
        f"Portfolio: {payload.get('portfolio_url', '')}\n"
        f"LinkedIn: {payload.get('linkedin_url', '')}\n\n"
        f"Output ONLY a JSON object with keys 'subject' and 'body'. No markdown."
    )

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=20,
        )
    content = res.json()["choices"][0]["message"]["content"].strip()
    # Strip markdown fences if Groq adds them
    content = content.replace("```json", "").replace("```", "").strip()
    import json
    try:
        parsed = json.loads(content)
    except Exception:
        parsed = {"subject": f"Application for {payload.get('job_title')}", "body": content}
    return parsed
