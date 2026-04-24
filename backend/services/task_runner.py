import asyncio
import logging
import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Project, Chapter, Segment, Terminology
from backend.core.database import AsyncSessionLocal
from backend.services.translator import (
    translate_text_with_stage,
    basic_quality_check,
    get_confirmed_term_dict,
    get_translator_config,
    build_quality_term_dict,
)
from backend.services.exporter import export_translated_project

logger = logging.getLogger(__name__)

TRANSLATION_DELAY_SECONDS = float(os.getenv("TRANSLATION_DELAY_SECONDS", "0"))
TRANSLATION_CONCURRENCY = max(1, int(os.getenv("TRANSLATION_CONCURRENCY", "6")))
PROGRESS_UPDATE_INTERVAL = max(1, int(os.getenv("PROGRESS_UPDATE_INTERVAL", "5")))
AUTO_RETRY_QA_FAILED = os.getenv("AUTO_RETRY_QA_FAILED", "true").lower() in {"1", "true", "yes", "on"}
AUTO_RETRY_MAX_PER_SEG = max(0, int(os.getenv("AUTO_RETRY_MAX_PER_SEG", "1")))

def is_segment_terminal(seg: Segment) -> bool:
    if seg.status == "completed":
        return True
    if seg.status == "qa_failed":
        return True
    return False

def build_context_window(chapter_segments: list[Segment], index: int) -> str:
    parts: list[str] = []

    prev_start = max(0, index - 2)
    for i in range(prev_start, index):
        seg = chapter_segments[i]
        text = (seg.translated_text or seg.original_text or "").strip()
        if text:
            parts.append(f"[Previous {i - prev_start + 1}] {text[-260:]}")

    if index + 1 < len(chapter_segments):
        next_seg = chapter_segments[index + 1]
        next_text = (next_seg.original_text or "").strip()
        if next_text:
            parts.append(f"[Next] {next_text[:220]}")

    return "\n".join(parts)

async def is_project_paused(project_id: str) -> bool:
    async with AsyncSessionLocal() as state_db:
        project = await state_db.get(Project, project_id)
        return bool(project and project.status == "paused")

async def translate_segment_with_pipeline(
    seg: Segment,
    project_id: str,
    db: AsyncSession,
    context: str = "",
    term_dict: dict[str, str] | None = None,
    translator_config=None,
) -> bool:
    effective_term_dict = term_dict or {}
    qa_term_dict = build_quality_term_dict(effective_term_dict)

    seg.status = "drafting"
    await db.commit()
    draft = await translate_text_with_stage(
        text=seg.original_text,
        project_id=project_id,
        db=db,
        context=context,
        stage="draft",
        term_dict=effective_term_dict,
        translator_config=translator_config,
    )
    if not draft:
        seg.status = "qa_failed"
        seg.retry_count += 1
        await db.commit()
        return False

    seg.status = "polishing"
    await db.commit()
    polished = await translate_text_with_stage(
        text=seg.original_text,
        project_id=project_id,
        db=db,
        context=context,
        stage="polish",
        draft_text=draft,
        term_dict=effective_term_dict,
        translator_config=translator_config,
    )

    final_text = polished or draft
    if not basic_quality_check(seg.original_text, final_text, qa_term_dict):
        seg.status = "polishing"
        await db.commit()
        repaired = await translate_text_with_stage(
            text=seg.original_text,
            project_id=project_id,
            db=db,
            context=context,
            stage="repair",
            draft_text=final_text,
            term_dict=effective_term_dict,
            translator_config=translator_config,
        )
        if repaired and basic_quality_check(seg.original_text, repaired, qa_term_dict):
            seg.translated_text = repaired
            seg.status = "completed"
            await db.commit()
            return True

        seg.translated_text = repaired or final_text
        seg.status = "qa_failed"
        seg.retry_count += 1
        await db.commit()
        return False

    seg.translated_text = final_text
    seg.status = "completed"
    await db.commit()
    return True

async def auto_retry_qa_failed_once(
    project_id: str,
    chapter_id: str,
    term_dict: dict[str, str],
    translator_config,
) -> int:
    if not AUTO_RETRY_QA_FAILED or AUTO_RETRY_MAX_PER_SEG <= 0:
        return 0

    recovered = 0
    async with AsyncSessionLocal() as retry_db:
        seg_stmt = (
            select(Segment)
            .where(Segment.chapter_id == chapter_id)
            .order_by(Segment.order_index)
        )
        chapter_segments = (await retry_db.execute(seg_stmt)).scalars().all()
        if not chapter_segments:
            return 0

        for idx, seg in enumerate(chapter_segments):
            if seg.status != "qa_failed":
                continue
            if seg.retry_count > AUTO_RETRY_MAX_PER_SEG:
                continue

            context_text = build_context_window(chapter_segments, idx)
            ok = await translate_segment_with_pipeline(
                seg,
                project_id,
                retry_db,
                context_text,
                term_dict=term_dict,
                translator_config=translator_config,
            )
            if ok:
                recovered += 1

        if recovered > 0:
            logger.info(f"Auto-retry recovered {recovered} segments in chapter {chapter_id}")

    return recovered

async def retry_failed_segments(project_id: str) -> int:
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project:
            return 0
        term_dict = await get_confirmed_term_dict(project_id, db)
        translator_config = await get_translator_config(db)

        stmt = (
            select(Segment)
            .join(Chapter, Segment.chapter_id == Chapter.id)
            .where(
                Chapter.project_id == project_id,
                Segment.status.in_(["qa_failed", "failed"])
            )
            .order_by(Chapter.order_index, Segment.order_index)
        )
        failed_segments = (await db.execute(stmt)).scalars().all()

        retried_count = 0
        for seg in failed_segments:
            context = ""
            segs_stmt = (
                select(Segment)
                .where(Segment.chapter_id == seg.chapter_id)
                .order_by(Segment.order_index)
            )
            chapter_segments = (await db.execute(segs_stmt)).scalars().all()
            for idx, chapter_seg in enumerate(chapter_segments):
                if chapter_seg.id == seg.id:
                    context = build_context_window(chapter_segments, idx)
                    break

            ok = await translate_segment_with_pipeline(
                seg,
                project_id,
                db,
                context,
                term_dict=term_dict,
                translator_config=translator_config,
            )
            if ok:
                retried_count += 1

        chapter_stmt = select(Chapter).where(Chapter.project_id == project_id)
        chapters = (await db.execute(chapter_stmt)).scalars().all()

        total_segments = 0
        terminal_segments = 0
        for chapter in chapters:
            seg_stmt = select(Segment).where(Segment.chapter_id == chapter.id)
            segs = (await db.execute(seg_stmt)).scalars().all()
            total_segments += len(segs)
            chapter_terminal = len([s for s in segs if is_segment_terminal(s)])
            terminal_segments += chapter_terminal
            chapter.status = "completed" if segs and chapter_terminal == len(segs) else "translating"

        project.progress = int((terminal_segments / total_segments) * 100) if total_segments else 0
        project.status = "completed" if project.progress == 100 else "translating"
        await db.commit()

        if project.status == "completed":
            export_dir = os.path.abspath("./backend/data/exports")
            os.makedirs(export_dir, exist_ok=True)
            await export_translated_project(project_id, db, export_dir)

        return retried_count

async def process_project_translation(project_id: str):
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project:
            logger.warning(f"Project {project_id} not found")
            return
        
        # 允许从 pending_terms 或 translating 状态开始翻译
        if project.status not in ["pending_terms", "translating"]:
            logger.warning(f"Project {project_id} is not in pending_terms or translating status, current: {project.status}")
            return
        
        # 立即更新状态为 translating
        project.status = "translating"
        await db.commit()
        try:
            term_dict = await get_confirmed_term_dict(project_id, db)
            translator_config = await get_translator_config(db)
        except Exception as e:
            logger.error(f"Failed to prepare translation context for project {project_id}: {e}")
            project.status = "failed"
            await db.commit()
            return

        logger.info(f"Starting translation for project {project_id}")
        
        stmt = select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
        chapters = (await db.execute(stmt)).scalars().all()
        
        logger.info(f"Found {len(chapters)} chapters to translate")
        paused_requested = False

        all_seg_stmt = (
            select(Segment)
            .join(Chapter, Segment.chapter_id == Chapter.id)
            .where(Chapter.project_id == project_id)
        )
        all_segments = (await db.execute(all_seg_stmt)).scalars().all()
        total_segments = len(all_segments)
        terminal_segments = len([seg for seg in all_segments if is_segment_terminal(seg)])

        async def process_segment_by_id(segment_id: str, context_text: str) -> bool:
            async with AsyncSessionLocal() as worker_db:
                if await is_project_paused(project_id):
                    return False
                seg = await worker_db.get(Segment, segment_id)
                if not seg:
                    return False
                if is_segment_terminal(seg):
                    return True
                try:
                    return await translate_segment_with_pipeline(
                        seg,
                        project_id,
                        worker_db,
                        context_text,
                        term_dict=term_dict,
                        translator_config=translator_config,
                    )
                except Exception as e:
                    logger.error(f"Failed to translate segment {segment_id}: {e}")
                    seg.status = "failed"
                    seg.retry_count += 1
                    await worker_db.commit()
                    return False

        async def process_chapter_segments(chapter_segments: list[Segment]) -> tuple[int, bool]:
            semaphore = asyncio.Semaphore(TRANSLATION_CONCURRENCY)

            async def worker(segment_id: str, context_text: str) -> bool:
                async with semaphore:
                    return await process_segment_by_id(segment_id, context_text)

            tasks = []
            for index, seg in enumerate(chapter_segments):
                context_text = build_context_window(chapter_segments, index)
                tasks.append(asyncio.create_task(worker(seg.id, context_text)))

            translated_ok = 0
            paused = False
            for index, task in enumerate(asyncio.as_completed(tasks), start=1):
                if await task:
                    translated_ok += 1

                if await is_project_paused(project_id):
                    paused = True
                    for pending_task in tasks:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break

                if index % PROGRESS_UPDATE_INTERVAL == 0 and total_segments > 0:
                    async with AsyncSessionLocal() as progress_db:
                        progress_project = await progress_db.get(Project, project_id)
                        if progress_project:
                            progress_project.progress = int((terminal_segments + translated_ok) / total_segments * 100)
                            await progress_db.commit()

            return translated_ok, paused

        for chapter in chapters:
            if await is_project_paused(project_id):
                paused_requested = True
                break

            if chapter.status == "completed":
                continue
                
            chapter.status = "translating"
            await db.commit()
            
            seg_stmt = select(Segment).where(Segment.chapter_id == chapter.id).order_by(Segment.order_index)
            segments = (await db.execute(seg_stmt)).scalars().all()

            pending_segments = [seg for seg in segments if not is_segment_terminal(seg)]

            chapter_translated = 0
            if pending_segments:
                chapter_translated, chapter_paused = await process_chapter_segments(pending_segments)
                terminal_segments += chapter_translated
                if chapter_paused:
                    paused_requested = True
                    break

                recovered_count = await auto_retry_qa_failed_once(
                    project_id,
                    chapter.id,
                    term_dict,
                    translator_config,
                )
                terminal_segments += recovered_count

                if TRANSLATION_DELAY_SECONDS > 0:
                    await asyncio.sleep(TRANSLATION_DELAY_SECONDS)

            async with AsyncSessionLocal() as chapter_db:
                chapter_obj = await chapter_db.get(Chapter, chapter.id)
                seg_refresh_stmt = select(Segment).where(Segment.chapter_id == chapter.id)
                refreshed_segments = (await chapter_db.execute(seg_refresh_stmt)).scalars().all()

                all_completed = all(is_segment_terminal(s) for s in refreshed_segments)
                if chapter_obj:
                    chapter_obj.status = "completed" if all_completed else "translating"
                    await chapter_db.commit()

            if total_segments > 0:
                async with AsyncSessionLocal() as progress_db:
                    progress_project = await progress_db.get(Project, project_id)
                    if progress_project:
                        progress_project.progress = int((terminal_segments / total_segments) * 100)
                        await progress_db.commit()

        async with AsyncSessionLocal() as final_db:
            final_project = await final_db.get(Project, project_id)
            if not final_project:
                return

            if paused_requested or final_project.status == "paused":
                final_seg_stmt = (
                    select(Segment)
                    .join(Chapter, Segment.chapter_id == Chapter.id)
                    .where(Chapter.project_id == project_id)
                )
                final_segments = (await final_db.execute(final_seg_stmt)).scalars().all()
                final_total = len(final_segments)
                final_terminal = len([seg for seg in final_segments if is_segment_terminal(seg)])
                final_project.progress = int((final_terminal / final_total) * 100) if final_total else 0
                final_project.status = "paused"
                await final_db.commit()
                logger.info(f"Project {project_id} paused")
                return

            final_seg_stmt = (
                select(Segment)
                .join(Chapter, Segment.chapter_id == Chapter.id)
                .where(Chapter.project_id == project_id)
            )
            final_segments = (await final_db.execute(final_seg_stmt)).scalars().all()
            final_total = len(final_segments)
            final_terminal = len([seg for seg in final_segments if is_segment_terminal(seg)])
            final_project.progress = int((final_terminal / final_total) * 100) if final_total else 0

            if final_total > 0 and final_terminal == final_total:
                final_project.status = "completed"
                final_project.progress = 100
                logger.info(f"Project {project_id} translation completed")

                try:
                    export_dir = os.path.abspath("./backend/data/exports")
                    os.makedirs(export_dir, exist_ok=True)
                    output_path, output_ext = await export_translated_project(project_id, final_db, export_dir)
                    logger.info(f"{output_ext.upper()} exported to {output_path}")
                except Exception as e:
                    logger.error(f"Failed to export translated file: {e}")
            else:
                final_project.status = "failed"
                logger.warning(f"Project {project_id} completed with failures")

            await final_db.commit()

def start_translation_task(project_id: str):
    """启动翻译任务"""
    logger.info(f"Attempting to start translation task for project {project_id}")
    try:
        loop = asyncio.get_running_loop()
        logger.info(f"Got running event loop, creating task for project {project_id}")
        loop.create_task(process_project_translation(project_id))
    except RuntimeError:
        # 没有运行中的事件循环，创建新线程运行
        logger.warning(f"No running event loop, creating new one for project {project_id}")
        asyncio.run(process_project_translation(project_id))

async def resume_pending_tasks():
    async with AsyncSessionLocal() as db:
        stmt = select(Project).where(Project.status == "translating")
        projects = (await db.execute(stmt)).scalars().all()
        for p in projects:
            logger.info(f"Resuming translation for project {p.id}")
            start_translation_task(p.id)
