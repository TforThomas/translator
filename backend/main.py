import os
import asyncio
import logging
import re
import mimetypes
import time
import uuid
from pathlib import Path
from collections import defaultdict
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="OmniTranslate API")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
ALLOWED_EXTENSIONS = {".epub", ".pdf"}
ALLOWED_MIME_TYPES = {"application/epub+zip", "application/epub", "application/pdf", "application/octet-stream"}
EN_WORD_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z\-']{2,}\b")
SOURCE_ALPHA_PATTERN = re.compile(r"[A-Za-z]")

# 简单的内存速率限制器
class RateLimiter:
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)
    
    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        # 清理过期的请求记录
        self.requests[client_ip] = [
            req_time for req_time in self.requests[client_ip]
            if now - req_time < self.window_seconds
        ]
        # 检查是否超过限制
        if len(self.requests[client_ip]) >= self.max_requests:
            return False
        self.requests[client_ip].append(now)
        return True

rate_limiter = RateLimiter(max_requests=120, window_seconds=60)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # 跳过健康检查
    if request.url.path == "/api/health":
        return await call_next(request)
    
    client_ip = request.client.host if request.client else "unknown"
    
    if not rate_limiter.is_allowed(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "请求过于频繁，请稍后再试"}
        )
    
    response = await call_next(request)
    return response

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """添加安全响应头"""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

def sanitize_filename(filename: str) -> str:
    """清理文件名，防止路径遍历攻击"""
    # 移除所有路径分隔符和特殊字符
    filename = os.path.basename(filename)  # 获取基本文件名
    # 只保留字母、数字、连字符、下划线和点
    filename = re.sub(r'[^a-zA-Z0-9_\-.]', '_', filename)
    # 移除开头的点（防止隐藏文件）
    filename = filename.lstrip('.')
    # 限制长度
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

@app.on_event("startup")
async def startup():
    os.makedirs("./backend/data", exist_ok=True)
    os.makedirs("./backend/data/uploads", exist_ok=True)
    os.makedirs("./backend/data/exports", exist_ok=True)
    await init_db()
    
    # Initialize default settings
    async with AsyncSessionLocal() as db:
        stmt = select(Settings).where(Settings.id == "default")
        res = await db.execute(stmt)
        if not res.scalar_one_or_none():
            db.add(Settings(id="default", openai_api_key="", openai_base_url="https://api.openai.com/v1", model_name="gpt-4o-mini"))
            await db.commit()
            
    # Resume interrupted tasks
    await resume_pending_tasks()

class ProjectCreate(BaseModel):
    name: str
    source_lang: str = "en"
    target_lang: str = "zh"
    enable_ocr: bool = False

@app.get("/api/projects")
async def get_projects(db: AsyncSession = Depends(get_db)):
    stmt = select(Project).order_by(Project.created_at.desc())
    result = await db.execute(stmt)
    projects = result.scalars().all()
    return projects

@app.post("/api/projects")
async def create_project(project: ProjectCreate, db: AsyncSession = Depends(get_db)):
    new_project = Project(
        name=project.name,
        source_lang=project.source_lang,
        target_lang=project.target_lang,
        enable_ocr=project.enable_ocr
    )
    db.add(new_project)
    await db.commit()
    await db.refresh(new_project)
    return new_project

@app.post("/api/projects/{project_id}/upload")
async def upload_project_file(project_id: str, background_tasks: BackgroundTasks, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    # 验证项目是否存在
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # 验证文件扩展名
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    # 验证 MIME 类型
    if file.content_type and file.content_type not in ALLOWED_MIME_TYPES:
        logger.warning(f"Suspicious MIME type: {file.content_type}")
        # 不直接拒绝，但记录警告（某些客户端可能发送错误的 MIME 类型）

    # 清理文件名，防止路径遍历攻击
    safe_filename = sanitize_filename(file.filename)
    
    # 使用安全的上传目录路径
    upload_dir = os.path.abspath("./backend/data/uploads")
    file_path = os.path.join(upload_dir, f"{project_id}_{safe_filename}")
    
    # 确保文件路径在上传目录内（防止路径遍历）
    if not file_path.startswith(upload_dir):
        raise HTTPException(status_code=400, detail="Invalid file path")
    
    # 读取并验证文件大小
    file_content = await file.read()
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size: 100MB")
    
    if len(file_content) == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    
    # 写入文件
    with open(file_path, "wb") as f:
        f.write(file_content)

    logger.info(f"File uploaded for project {project_id}: {safe_filename}")
    
    project.source_file_path = file_path
    project.status = "parsing"
    await db.commit()

    async def parse_task(file_path, project_id, enable_ocr):
        async with AsyncSessionLocal() as session:
            try:
                # 根据文件扩展名选择解析器
                file_ext = Path(file_path).suffix.lower()
                if file_ext == ".pdf":
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
                    project = await error_session.get(Project, project_id)
                    if project:
                        project.status = "failed"
                        await error_session.commit()

    background_tasks.add_task(parse_task, file_path, project_id, project.enable_ocr)
    return {"ok": True}

@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """删除项目及其所有相关数据"""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # 删除相关的章节和段落
    stmt_chapters = select(Chapter).where(Chapter.project_id == project_id)
    chapters = (await db.execute(stmt_chapters)).scalars().all()
    
    for chapter in chapters:
        # 删除章节下的所有段落
        stmt_segments = select(Segment).where(Segment.chapter_id == chapter.id)
        segments = (await db.execute(stmt_segments)).scalars().all()
        for segment in segments:
            await db.delete(segment)
        await db.delete(chapter)
    
    # 删除相关的术语
    stmt_terms = select(Terminology).where(Terminology.project_id == project_id)
    terms = (await db.execute(stmt_terms)).scalars().all()
    for term in terms:
        await db.delete(term)
    
    # 删除项目文件
    if project.source_file_path and os.path.exists(project.source_file_path):
        try:
            os.remove(project.source_file_path)
        except Exception as e:
            logger.warning(f"Failed to delete source file: {e}")
    
    # 删除项目
    await db.delete(project)
    await db.commit()
    
    return {"ok": True}

@app.get("/api/projects/{project_id}/status")
async def get_project_status(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    stmt = select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
    chapters = (await db.execute(stmt)).scalars().all()

    chapter_data = []
    segment_summary = {
        "pending": 0,
        "drafting": 0,
        "polishing": 0,
        "completed": 0,
        "qa_failed": 0,
        "failed": 0
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
        if seg.status == "completed":
            return True
        if seg.status == "qa_failed":
            return True
        return False

    for c in chapters:
        seg_stmt = select(Segment).where(Segment.chapter_id == c.id)
        segments = (await db.execute(seg_stmt)).scalars().all()
        total = len(segments)
        terminal = len([s for s in segments if is_terminal_segment(s)])
        progress = int((terminal / total) * 100) if total > 0 else 0

        chapter_segment_summary = {
            "pending": 0,
            "drafting": 0,
            "polishing": 0,
            "completed": 0,
            "qa_failed": 0,
            "failed": 0
        }
        for seg in segments:
            status = seg.status if seg.status in chapter_segment_summary else "pending"
            chapter_segment_summary[status] += 1
            if status in segment_summary:
                segment_summary[status] += 1

            original_text = (seg.original_text or "").strip()
            translated_text = (seg.translated_text or "").strip()

            if status == "qa_failed":
                quality_summary["qa_failed_segments"] += 1
            if status == "completed" and seg.retry_count > 0:
                quality_summary["auto_repaired_segments"] += 1

            # 以状态为主判断“已产出译文”，避免部分历史数据 translated_text 为空导致统计为 0
            if status in {"completed", "qa_failed"}:
                quality_summary["translated_segments"] += 1

            if translated_text:
                source_alpha_count = len(SOURCE_ALPHA_PATTERN.findall(original_text))
                translated_words = EN_WORD_PATTERN.findall(translated_text)
                translated_alpha_count = sum(len(word) for word in translated_words)
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
            "status": c.status,
            "progress": progress,
            "segment_summary": chapter_segment_summary
        })

    if retry_counted_segments > 0:
        quality_summary["avg_retry_count"] = round(total_retry_count / retry_counted_segments, 2)

    return {
        "id": project.id,
        "name": project.name,
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
    result = await db.execute(stmt)
    terms = result.scalars().all()
    return terms

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
    
    stmt = select(Terminology).where(Terminology.project_id == project_id)
    terms = (await db.execute(stmt)).scalars().all()
    logger.info(f"Found {len(terms)} terms to confirm")
    
    for t in terms:
        t.is_confirmed = True
    
    project = await db.get(Project, project_id)
    if project and project.status == "pending_terms":
        project.status = "translating"
        await db.commit()
        logger.info(f"Project {project_id} status changed to translating")
        
        # 使用 background_tasks 启动翻译，确保 DB commit 完成后再启动
        background_tasks.add_task(start_translation_task, project_id)
        logger.info(f"Translation task scheduled for project {project_id}")
    elif project:
        logger.warning(f"Project {project_id} status is {project.status}, not pending_terms")

    await db.commit()
    return {"ok": True}

@app.post("/api/projects/{project_id}/export")
async def export_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """手动导出译文文件（按原始格式导出）"""
    
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    export_dir = os.path.abspath("./backend/data/exports")
    output_path, _ = await export_translated_project(project_id, db, export_dir)
    return {"ok": True, "path": output_path}

@app.get("/api/projects/{project_id}/download")
async def download_project(project_id: str, db: AsyncSession = Depends(get_db)):
    # 验证 project_id 格式，防止路径遍历
    if not re.match(r'^[a-zA-Z0-9_-]+$', project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # 使用安全的路径构建（严格按原始格式，每次即时导出，避免历史文件干扰）
    export_dir = os.path.abspath("./backend/data/exports")
    expected_ext = get_project_source_ext(project)
    try:
        output_suffix = f"_dl_{uuid.uuid4().hex[:8]}"
        output_path, output_ext = await export_translated_project(
            project_id,
            db,
            export_dir,
            output_suffix=output_suffix,
        )
    except Exception as e:
        logger.error(f"Export failed for project {project_id}: {e}")
        raise HTTPException(status_code=500, detail="Export failed, please retry")

    if output_ext != expected_ext:
        logger.error(
            f"Export format mismatch for project {project_id}: expected {expected_ext}, got {output_ext}"
        )
        raise HTTPException(status_code=500, detail=f"Export format mismatch: expected {expected_ext}")
    
    # 确保文件路径在导出目录内
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
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )

class SettingsUpdate(BaseModel):
    openai_api_key: str
    openai_base_url: str
    model_name: str

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}

@app.get("/api/providers")
async def get_providers():
    """获取支持的 API 提供商列表"""
    return {
        "providers": [
            {
                "id": "siliconflow",
                "name": "硅基流动",
                "base_url": "https://api.siliconflow.cn/v1",
                "models": [
                    "deepseek-ai/DeepSeek-V3.2",
                    "Qwen/Qwen2.5-72B-Instruct",
                    "THUDM/glm-4-9b-chat",
                    "meta-llama/Meta-Llama-3.1-405B-Instruct"
                ],
                "icon": "🟣",
                "description": "国内高速，支持多种开源模型"
            },
            {
                "id": "openai",
                "name": "OpenAI",
                "base_url": "https://api.openai.com/v1",
                "models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
                "icon": "🟢",
                "description": "官方 GPT 系列模型"
            },
            {
                "id": "google",
                "name": "Google Gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "models": ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-pro"],
                "icon": "🔵",
                "description": "Google 原生 API，速度快"
            },
            {
                "id": "ollama",
                "name": "Ollama 本地",
                "base_url": "http://localhost:11434/v1",
                "models": ["llama3", "qwen2.5", "deepseek-r1", "mistral"],
                "icon": "🟠",
                "description": "本地部署，免费使用"
            },
            {
                "id": "custom",
                "name": "自定义",
                "base_url": "",
                "models": [],
                "icon": "⚙️",
                "description": "兼容 OpenAI API 格式的服务"
            }
        ]
    }

def mask_api_key(key: str) -> str:
    """隐藏 API Key，只显示前4位和后4位"""
    if not key or len(key) <= 8:
        return "****"
    return key[:4] + "*" * (len(key) - 8) + key[-4:]

@app.get("/api/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    stmt = select(Settings).where(Settings.id == "default")
    settings = (await db.execute(stmt)).scalar_one_or_none()
    if not settings:
        return {
            "openai_api_key": "",
            "openai_base_url": "https://api.siliconflow.cn/v1",
            "model_name": "deepseek-ai/DeepSeek-V3.2"
        }
    return {
        "openai_api_key": settings.openai_api_key,
        "openai_base_url": settings.openai_base_url,
        "model_name": settings.model_name
    }

@app.post("/api/settings")
async def update_settings(data: SettingsUpdate, db: AsyncSession = Depends(get_db)):
    stmt = select(Settings).where(Settings.id == "default")
    settings = (await db.execute(stmt)).scalar_one_or_none()
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
