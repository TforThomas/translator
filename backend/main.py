import os
import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from collections import defaultdict
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from pydantic import BaseModel

from backend.core.database import init_db, get_db, AsyncSessionLocal
from backend.models.models import Project, Chapter, Segment, Terminology, Settings
from backend.services.parser import parse_epub_to_db, parse_pdf_to_db
from backend.services.task_runner import start_translation_task, resume_pending_tasks, retry_failed_segments
from backend.services.exporter import (
    export_translated_project,
    build_output_path,
    get_export_media_type,
    get_project_source_ext,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="OmniTranslate API")

# 主事件循环句柄（供背景线程调度协程使用）
MAIN_LOOP: asyncio.AbstractEventLoop | None = None

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
ALLOWED_EXTENSIONS = {".epub", ".pdf"}
ALLOWED_MIME_TYPES = {"application/epub+zip", "application/epub", "application/pdf", "application/octet-stream"}
EN_WORD_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z\-']{2,}\b")
SOURCE_ALPHA_PATTERN = re.compile(r"[A-Za-z]")


class RateLimiter:
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)

    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        self.requests[client_ip] = [t for t in self.requests[client_ip] if now - t < self.window_seconds]
        if len(self.requests[client_ip]) >= self.max_requests:
            return False
        self.requests[client_ip].append(now)
        return True


rate_limiter = RateLimiter(max_requests=120, window_seconds=60)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path == "/api/health":
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.is_allowed(client_ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def sanitize_filename(filename: str) -> str:
    filename = os.path.basename(filename)
    filename = re.sub(r"[^a-zA-Z0-9_\-.]", "_", filename)
    filename = filename.lstrip(".")
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:190] + ext
    return filename if filename else "unnamed_file"


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


async def _ensure_schema_columns():
    """对老 SQLite 数据库做幂等的 ALTER TABLE，避免新增列后启动失败。"""
    async with AsyncSessionLocal() as db:
        for sql in [
            "ALTER TABLE projects ADD COLUMN genre TEXT DEFAULT 'general'",
            "ALTER TABLE chapters ADD COLUMN translated_title TEXT",
        ]:
            try:
                await db.execute(text(sql))
                await db.commit()
            except Exception:
                await db.rollback()


@app.on_event("startup")
async def startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

    os.makedirs("./backend/data", exist_ok=True)
    os.makedirs("./backend/data/uploads", exist_ok=True)
    os.makedirs("./backend/data/exports", exist_ok=True)
    await init_db()
    await _ensure_schema_columns()

    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Settings).where(Settings.id == "default"))
        if not res.scalar_one_or_none():
            db.add(Settings(
                id="default",
                openai_api_key="",
                openai_base_url="https://api.openai.com/v1",
                model_name="gpt-4o-mini",
            ))
            await db.commit()

    await resume_pending_tasks()


class ProjectCreate(BaseModel):
    name: str
    source_lang: str = "en"
    target_lang: str = "zh"
    enable_ocr: bool = False
    genre: str = "general"


@app.get("/api/projects")
async def get_projects(db: AsyncSession = Depends(get_db)):
    stmt = select(Project).order_by(Project.created_at.desc())
    return (await db.execute(stmt)).scalars().all()


@app.post("/api/projects")
async def create_project(project: ProjectCreate, db: AsyncSession = Depends(get_db)):
    new_project = Project(
        name=project.name,
        source_lang=project.source_lang,
        target_lang=project.target_lang,
        enable_ocr=project.enable_ocr,
        genre=project.genre or "general",
    )
    db.add(new_project)
    await db.commit()
    await db.refresh(new_project)
    return new_project


@app.post("/api/projects/{project_id}/upload")
async def upload_project_file(project_id: str, background_tasks: BackgroundTasks, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")
    if file.content_type and file.content_type not in ALLOWED_MIME_TYPES:
        logger.warning(f"Suspicious MIME type: {file.content_type}")

    safe_filename = sanitize_filename(file.filename)
    upload_dir = os.path.abspath("./backend/data/uploads")
    file_path = os.path.join(upload_dir, f"{project_id}_{safe_filename}")
    if not file_path.startswith(upload_dir):
        raise HTTPException(status_code=400, detail="Invalid file path")

    file_content = await file.read()
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size: 100MB")
    if len(file_content) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    with open(file_path, "wb") as f:
        f.write(file_content)

    logger.info(f"File uploaded for project {project_id}: {safe_filename}")

    project.source_file_path = file_path
    project.status = "parsing"
    await db.commit()

    async def parse_task(file_path, project_id, enable_ocr):
        async with AsyncSessionLocal() as session:
            try:
                ext = Path(file_path).suffix.lower()
                if ext == ".pdf":
                    success = await parse_pdf_to_db(file_path, project_id, session, enable_ocr)
                else:
                    success = await parse_epub_to_db(file_path, project_id, session)
                if success:
                    logger.info(f"Successfully parsed project {project_id}")
                else:
                    logger.error(f"Parser returned failure for project {project_id}")
            except Exception as e:
                logger.error(f"Failed to parse project {project_id}: {e}")
                async with AsyncSessionLocal() as error_session:
                    p = await error_session.get(Project, project_id)
                    if p:
                        p.status = "failed"
                        await error_session.commit()

    background_tasks.add_task(parse_task, file_path, project_id, project.enable_ocr)
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapters = (await db.execute(select(Chapter).where(Chapter.project_id == project_id))).scalars().all()
    for chapter in chapters:
        segments = (await db.execute(select(Segment).where(Segment.chapter_id == chapter.id))).scalars().all()
        for segment in segments:
            await db.delete(segment)
        await db.delete(chapter)

    terms = (await db.execute(select(Terminology).where(Terminology.project_id == project_id))).scalars().all()
    for term in terms:
        await db.delete(term)

    if project.source_file_path and os.path.exists(project.source_file_path):
        try:
            os.remove(project.source_file_path)
        except Exception as e:
            logger.warning(f"Failed to delete source file: {e}")

    await db.delete(project)
    await db.commit()
    return {"ok": True}


@app.get("/api/projects/{project_id}/status")
async def get_project_status(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chapters = (await db.execute(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
    )).scalars().all()

    chapter_data = []
    segment_summary = {
        "pending": 0, "translating": 0, "drafting": 0, "polishing": 0,
        "repairing": 0, "completed": 0, "qa_failed": 0, "failed": 0,
    }
    quality_summary = {
        "translated_segments": 0,
        "auto_repaired_segments": 0,
        "qa_failed_segments": 0,
        "high_english_residue_segments": 0,
        "too_short_translation_segments": 0,
        "avg_retry_count": 0.0,
    }
    total_retry_count = 0
    retry_counted_segments = 0

    def is_terminal_segment(seg: Segment) -> bool:
        return seg.status in ("completed", "qa_failed")

    for c in chapters:
        segments = (await db.execute(select(Segment).where(Segment.chapter_id == c.id))).scalars().all()
        total = len(segments)
        terminal = len([s for s in segments if is_terminal_segment(s)])
        progress = int((terminal / total) * 100) if total > 0 else 0

        chapter_segment_summary = {k: 0 for k in segment_summary}
        for seg in segments:
            status = seg.status if seg.status in chapter_segment_summary else "pending"
            chapter_segment_summary[status] += 1
            segment_summary[status] += 1

            original_text = (seg.original_text or "").strip()
            translated_text = (seg.translated_text or "").strip()

            if status == "qa_failed":
                quality_summary["qa_failed_segments"] += 1
            if status == "completed" and seg.retry_count > 0:
                quality_summary["auto_repaired_segments"] += 1
            if status in {"completed", "qa_failed"}:
                quality_summary["translated_segments"] += 1

            if translated_text:
                source_alpha_count = len(SOURCE_ALPHA_PATTERN.findall(original_text))
                translated_words = EN_WORD_PATTERN.findall(translated_text)
                translated_alpha_count = sum(len(w) for w in translated_words)
                if source_alpha_count >= 20 and translated_alpha_count / max(1, source_alpha_count) > 0.45:
                    quality_summary["high_english_residue_segments"] += 1
                original_len = len(original_text)
                translated_len = len(translated_text)
                if original_len > 30 and translated_len < max(6, int(original_len * 0.2)):
                    quality_summary["too_short_translation_segments"] += 1

            total_retry_count += max(0, seg.retry_count)
            retry_counted_segments += 1

        chapter_data.append({
            "id": c.id,
            "title": c.title,
            "translated_title": c.translated_title,
            "status": c.status,
            "progress": progress,
            "segment_summary": chapter_segment_summary,
        })

    if retry_counted_segments > 0:
        quality_summary["avg_retry_count"] = round(total_retry_count / retry_counted_segments, 2)

    return {
        "id": project.id,
        "name": project.name,
        "genre": getattr(project, "genre", "general"),
        "progress": project.progress,
        "status": project.status,
        "chapters": chapter_data,
        "segment_summary": segment_summary,
        "quality_summary": quality_summary,
    }


class TermConfirm(BaseModel):
    confirmed: bool


class TermUpdate(BaseModel):
    translated_term: str


@app.get("/api/projects/{project_id}/terms")
async def get_project_terms(project_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(Terminology).where(Terminology.project_id == project_id)
    return (await db.execute(stmt)).scalars().all()


@app.post("/api/terms/{term_id}/confirm")
async def confirm_term(term_id: str, db: AsyncSession = Depends(get_db)):
    term = await db.get(Terminology, term_id)
    if not term:
        raise HTTPException(status_code=404, detail="Term not found")
    term.is_confirmed = True
    await db.commit()
    return {"ok": True}


@app.post("/api/terms/{term_id}/update")
async def update_term(term_id: str, data: TermUpdate, db: AsyncSession = Depends(get_db)):
    term = await db.get(Terminology, term_id)
    if not term:
        raise HTTPException(status_code=404, detail="Term not found")
    term.translated_term = data.translated_term
    await db.commit()
    return {"ok": True}


class RetryTasksRequest(BaseModel):
    project_id: str


class TaskControlRequest(BaseModel):
    project_id: str


@app.post("/api/tasks/retry")
async def retry_project_failed_tasks(payload: RetryTasksRequest):
    retried_count = await retry_failed_segments(payload.project_id)
    return {"ok": True, "retried": retried_count}


@app.post("/api/tasks/pause")
async def pause_project_task(payload: TaskControlRequest, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status in ["completed", "failed"]:
        return {"ok": True, "status": project.status}
    project.status = "paused"
    await db.commit()
    return {"ok": True, "status": "paused"}


@app.post("/api/tasks/resume")
async def resume_project_task(payload: TaskControlRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status == "completed":
        return {"ok": True, "status": "completed"}
    if project.status == "failed":
        return {"ok": True, "status": "failed"}
    project.status = "translating"
    await db.commit()
    background_tasks.add_task(start_translation_task, payload.project_id)
    return {"ok": True, "status": "translating"}


@app.post("/api/projects/{project_id}/terms/confirm_all")
async def confirm_all_terms(project_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    logger.info(f"Confirming all terms for project {project_id}")
    terms = (await db.execute(select(Terminology).where(Terminology.project_id == project_id))).scalars().all()
    logger.info(f"Found {len(terms)} terms to confirm")
    for t in terms:
        t.is_confirmed = True

    project = await db.get(Project, project_id)
    if project and project.status == "pending_terms":
        project.status = "translating"
        await db.commit()
        logger.info(f"Project {project_id} status changed to translating")
        background_tasks.add_task(start_translation_task, project_id)
        logger.info(f"Translation task scheduled for project {project_id}")
    elif project:
        logger.warning(f"Project {project_id} status is {project.status}, not pending_terms")

    await db.commit()
    return {"ok": True}


@app.post("/api/projects/{project_id}/export")
async def export_project(project_id: str, mode: str = "replace", db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    export_dir = os.path.abspath("./backend/data/exports")
    output_path, _ = await export_translated_project(project_id, db, export_dir, mode=mode)
    return {"ok": True, "path": output_path}


@app.get("/api/projects/{project_id}/download")
async def download_project(project_id: str, mode: str = "replace", db: AsyncSession = Depends(get_db)):
    if not re.match(r"^[a-zA-Z0-9_-]+$", project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    if mode not in ("replace", "bilingual"):
        raise HTTPException(status_code=400, detail="Invalid mode")

    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    export_dir = os.path.abspath("./backend/data/exports")
    expected_ext = get_project_source_ext(project)
    try:
        output_suffix = f"_dl_{uuid.uuid4().hex[:8]}"
        output_path, output_ext = await export_translated_project(
            project_id, db, export_dir, output_suffix=output_suffix, mode=mode,
        )
    except Exception as e:
        logger.error(f"Export failed for project {project_id}: {e}")
        raise HTTPException(status_code=500, detail="Export failed, please retry")

    if output_ext != expected_ext:
        logger.error(f"Export format mismatch for project {project_id}: expected {expected_ext}, got {output_ext}")
        raise HTTPException(status_code=500, detail=f"Export format mismatch: expected {expected_ext}")

    if not output_path.startswith(export_dir):
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Translated file not found")

    base_name = os.path.splitext(project.name)[0] if project.name else f"translated_{project_id}"
    return FileResponse(
        output_path,
        media_type=get_export_media_type(output_ext),
        filename=f"{base_name}_translated{output_ext}",
        headers={
            "X-Export-Format": output_ext.lstrip(".").lower(),
            "X-Export-Mode": mode,
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


class SettingsUpdate(BaseModel):
    openai_api_key: str
    openai_base_url: str
    model_name: str


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "1.1.0"}


@app.get("/api/providers")
async def get_providers():
    return {
        "providers": [
            {"id": "siliconflow", "name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1",
             "models": ["deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen2.5-72B-Instruct", "THUDM/glm-4-9b-chat", "meta-llama/Meta-Llama-3.1-405B-Instruct"],
             "icon": "🟣", "description": "国内高速，支持多种开源模型"},
            {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
             "models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"], "icon": "🟢", "description": "官方 GPT 系列模型"},
            {"id": "google", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
             "models": ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-pro"], "icon": "🔵", "description": "Google 原生 API，速度快"},
            {"id": "ollama", "name": "Ollama 本地", "base_url": "http://localhost:11434/v1",
             "models": ["llama3", "qwen2.5", "deepseek-r1", "mistral"], "icon": "🟠", "description": "本地部署，免费使用"},
            {"id": "custom", "name": "自定义", "base_url": "", "models": [],
             "icon": "⚙️", "description": "兼容 OpenAI API 格式的服务"},
        ]
    }


def mask_api_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "****"
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@app.get("/api/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    settings = (await db.execute(select(Settings).where(Settings.id == "default"))).scalar_one_or_none()
    if not settings:
        return {
            "openai_api_key": "",
            "openai_base_url": "https://api.siliconflow.cn/v1",
            "model_name": "deepseek-ai/DeepSeek-V3.2",
        }
    return {
        "openai_api_key": settings.openai_api_key,
        "openai_base_url": settings.openai_base_url,
        "model_name": settings.model_name,
    }


@app.post("/api/settings")
async def update_settings(data: SettingsUpdate, db: AsyncSession = Depends(get_db)):
    settings = (await db.execute(select(Settings).where(Settings.id == "default"))).scalar_one_or_none()
    if settings:
        settings.openai_api_key = data.openai_api_key
        settings.openai_base_url = data.openai_base_url
        settings.model_name = data.model_name
    else:
        settings = Settings(id="default", **data.model_dump())
        db.add(settings)
    await db.commit()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)