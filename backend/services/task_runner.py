import asyncio
import json as _json
import logging
import os
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Project, Chapter, Segment, Terminology
from backend.core.database import AsyncSessionLocal
from backend.services.translator import (
    translate_text_with_stage,
    translate_batch_one_pass,
    basic_quality_check,
    qa_diagnose,
    get_confirmed_term_dict,
    get_translator_config,
    build_quality_term_dict,
    resolve_genre,
    translate_with_openai_format,
)
from backend.services.exporter import export_translated_project

logger = logging.getLogger(__name__)

TRANSLATION_DELAY_SECONDS = float(os.getenv("TRANSLATION_DELAY_SECONDS", "0"))
TRANSLATION_CONCURRENCY = max(1, int(os.getenv("TRANSLATION_CONCURRENCY", "8")))
PROGRESS_UPDATE_INTERVAL = max(1, int(os.getenv("PROGRESS_UPDATE_INTERVAL", "5")))
AUTO_RETRY_QA_FAILED = os.getenv("AUTO_RETRY_QA_FAILED", "true").lower() in {"1", "true", "yes", "on"}
AUTO_RETRY_MAX_PER_SEG = max(0, int(os.getenv("AUTO_RETRY_MAX_PER_SEG", "1")))
TRANSLATION_BATCH_SIZE = max(0, int(os.getenv("TRANSLATION_BATCH_SIZE", "4")))
TRANSLATION_BATCH_MAX_CHARS = int(os.getenv("TRANSLATION_BATCH_MAX_CHARS", "2400"))
GLOSSARY_EXPAND_EVERY = max(0, int(os.getenv("GLOSSARY_EXPAND_EVERY", "3")))

CTX_PREV_SEGS = max(0, int(os.getenv("CTX_PREV_SEGS", "3")))
CTX_NEXT_SEGS = max(0, int(os.getenv("CTX_NEXT_SEGS", "2")))
CTX_PREV_MAX = int(os.getenv("CTX_PREV_MAX_CHARS", "600"))
CTX_NEXT_MAX = int(os.getenv("CTX_NEXT_MAX_CHARS", "400"))
_SENT_END_RE = re.compile(r"[.!?。！？]\s+")


def is_segment_terminal(seg: Segment) -> bool:
    return seg.status in ("completed", "qa_failed")


def _trim_at_sentence(text: str, max_chars: int, side: str) -> str:
    if len(text) <= max_chars:
        return text
    if side == "tail":
        chunk = text[-max_chars:]
        m = _SENT_END_RE.search(chunk)
        return chunk[m.end():] if m else chunk
    chunk = text[:max_chars]
    matches = list(_SENT_END_RE.finditer(chunk))
    return chunk[: matches[-1].end()] if matches else chunk


def build_context_window(
    chapter_segments: list[Segment],
    index: int,
    chapter_title: str = "",
    chapter_summary: str = "",
) -> str:
    prev_raw = "".join(
        (s.translated_text or s.original_text or "")
        for s in chapter_segments[max(0, index - CTX_PREV_SEGS):index]
    )
    next_raw = "".join((s.original_text or "") for s in chapter_segments[index + 1: index + 1 + CTX_NEXT_SEGS])
    prev = _trim_at_sentence(prev_raw, CTX_PREV_MAX, "tail")
    nxt = _trim_at_sentence(next_raw, CTX_NEXT_MAX, "head")
    pieces: list[str] = []
    if chapter_title:
        pieces.append(f"[Chapter] {chapter_title}")
    if chapter_summary:
        pieces.append(f"[Previously] {chapter_summary}")
    if prev:
        pieces.append(f"[Before]\n{prev}")
    if nxt:
        pieces.append(f"[After]\n{nxt}")
    return "\n\n".join(pieces)


async def is_project_paused(project_id: str) -> bool:
    async with AsyncSessionLocal() as state_db:
        project = await state_db.get(Project, project_id)
        return bool(project and project.status == "paused")


async def summarize_chapter_brief(chapter_text: str, translator_config) -> str:
    if not chapter_text.strip():
        return ""
    try:
        return (await translate_with_openai_format(
            translator_config,
            "Summarize the following chapter in ONE Chinese sentence (<=40 chars). Return plain text only.",
            chapter_text[:4000],
            max_retries=2,
            max_tokens=200,
        )) or ""
    except Exception:
        return ""


async def expand_glossary_from_chapter(
    project_id: str,
    chapter_text: str,
    existing: set[str],
    translator_config,
) -> list[dict]:
    if not chapter_text.strip():
        return []
    prompt = (
        "Extract up to 10 NEW proper nouns / technical terms from the text that are NOT already in this list:\n"
        f"{sorted(existing)[:80]}\n"
        "Return ONLY a JSON array of objects, each with keys: \"source\" (English), \"target\" (Chinese suggestion). "
        "No markdown, no commentary."
    )
    try:
        raw = await translate_with_openai_format(
            translator_config, prompt, chapter_text[:3500], max_retries=2, max_tokens=800,
        )
        if not raw:
            return []
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        data = _json.loads(raw)
        if isinstance(data, dict):
            for k in ("results", "data", "terms"):
                if isinstance(data.get(k), list):
                    data = data[k]; break
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            s = (d.get("source") or "").strip()
            t = (d.get("target") or "").strip()
            if s and t and s not in existing:
                out.append({"source": s, "target": t})
        return out
    except Exception:
        return []


async def translate_segment_with_pipeline(
    seg: Segment,
    project_id: str,
    db: AsyncSession,
    context: str = "",
    term_dict: dict[str, str] | None = None,
    translator_config=None,
    genre_role: str = "professional translator",
) -> bool:
    """单段 pipeline：one_pass → QA → 失败才 repair。状态区分 failed / qa_failed / repairing。"""
    effective_term_dict = term_dict or {}
    qa_term_dict = build_quality_term_dict(effective_term_dict)

    # 公式 / 图像区块在解析时已被标记 skip_translate
    try:
        meta = _json.loads(seg.html_tag) if seg.html_tag and seg.html_tag.startswith("{") else {}
    except Exception:
        meta = {}
    if meta.get("skip_translate"):
        seg.translated_text = seg.original_text
        seg.status = "completed"
        await db.commit()
        return True

    seg.status = "translating"
    await db.commit()

    final_text = await translate_text_with_stage(
        text=seg.original_text,
        project_id=project_id,
        db=db,
        context=context,
        stage="one_pass",
        term_dict=effective_term_dict,
        translator_config=translator_config,
        genre_role=genre_role,
    )
    if not final_text:
        seg.status = "failed"
        seg.retry_count += 1
        await db.commit()
        return False

    issues = qa_diagnose(seg.original_text, final_text, qa_term_dict)
    if issues:
        seg.status = "repairing"
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
            genre_role=genre_role,
            qa_issues=issues,
        )
        if repaired and not qa_diagnose(seg.original_text, repaired, qa_term_dict):
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
    genre_role: str = "professional translator",
) -> int:
    if not AUTO_RETRY_QA_FAILED or AUTO_RETRY_MAX_PER_SEG <= 0:
        return 0
    recovered = 0
    async with AsyncSessionLocal() as retry_db:
        chapter_segments = (await retry_db.execute(
            select(Segment).where(Segment.chapter_id == chapter_id).order_by(Segment.order_index)
        )).scalars().all()
        if not chapter_segments:
            return 0
        for idx, seg in enumerate(chapter_segments):
            if seg.status != "qa_failed":
                continue
            if seg.retry_count >= AUTO_RETRY_MAX_PER_SEG:
                continue
            ctx = build_context_window(chapter_segments, idx)
            ok = await translate_segment_with_pipeline(
                seg, project_id, retry_db, ctx,
                term_dict=term_dict,
                translator_config=translator_config,
                genre_role=genre_role,
            )
            if ok:
                recovered += 1
    if recovered > 0:
        logger.info(f"Auto-retry recovered {recovered} segments in chapter {chapter_id}")
    return recovered


async def _try_batch_translate(
    pending_segments: list[Segment],
    chapter_segments: list[Segment],
    project_id: str,
    term_dict: dict[str, str],
    translator_config,
    genre_role: str,
    chapter_title: str,
    chapter_summary: str,
) -> set[str]:
    """对短段做批量调用；成功的段标记 completed，返回已处理段的 id 集合。"""
    if TRANSLATION_BATCH_SIZE <= 1 or not pending_segments:
        return set()

    qa_term_dict = build_quality_term_dict(term_dict)
    handled: set[str] = set()

    batch: list[tuple[int, Segment, str]] = []
    char_sum = 0
    seg_index_map = {s.id: i for i, s in enumerate(chapter_segments)}

    async def flush():
        nonlocal batch, char_sum
        if not batch:
            return
        items = [
            {"id": idx, "text": s.original_text, "context": ctx}
            for idx, (_, s, ctx) in enumerate(batch)
        ]
        async with AsyncSessionLocal() as bdb:
            results = await translate_batch_one_pass(
                items, project_id, bdb,
                translator_config=translator_config,
                term_dict=term_dict,
                genre_role=genre_role,
            )
            for idx, (_, s, _ctx) in enumerate(batch):
                t = results.get(idx)
                seg_obj = await bdb.get(Segment, s.id)
                if not seg_obj:
                    continue
                if t and not qa_diagnose(seg_obj.original_text, t, qa_term_dict):
                    seg_obj.translated_text = t
                    seg_obj.status = "completed"
                    handled.add(seg_obj.id)
            await bdb.commit()
        batch = []
        char_sum = 0

    for s in pending_segments:
        try:
            meta = _json.loads(s.html_tag) if s.html_tag and s.html_tag.startswith("{") else {}
        except Exception:
            meta = {}
        if meta.get("skip_translate"):
            continue
        text_len = len(s.original_text or "")
        if text_len > TRANSLATION_BATCH_MAX_CHARS // 2:
            await flush()
            continue
        idx_in_chapter = seg_index_map.get(s.id, 0)
        ctx = build_context_window(chapter_segments, idx_in_chapter, chapter_title, chapter_summary)
        if char_sum + text_len > TRANSLATION_BATCH_MAX_CHARS or len(batch) >= TRANSLATION_BATCH_SIZE:
            await flush()
        batch.append((idx_in_chapter, s, ctx))
        char_sum += text_len

    await flush()
    return handled


async def retry_failed_segments(project_id: str) -> int:
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project:
            return 0
        term_dict = await get_confirmed_term_dict(project_id, db)
        translator_config = await get_translator_config(db)
        genre_role, _ = resolve_genre(project)

        failed_segments = (await db.execute(
            select(Segment).join(Chapter, Segment.chapter_id == Chapter.id)
            .where(Chapter.project_id == project_id, Segment.status.in_(["qa_failed", "failed"]))
            .order_by(Chapter.order_index, Segment.order_index)
        )).scalars().all()

        retried = 0
        for seg in failed_segments:
            chapter_segments = (await db.execute(
                select(Segment).where(Segment.chapter_id == seg.chapter_id).order_by(Segment.order_index)
            )).scalars().all()
            ctx = ""
            for idx, cs in enumerate(chapter_segments):
                if cs.id == seg.id:
                    ctx = build_context_window(chapter_segments, idx)
                    break
            ok = await translate_segment_with_pipeline(
                seg, project_id, db, ctx,
                term_dict=term_dict,
                translator_config=translator_config,
                genre_role=genre_role,
            )
            if ok:
                retried += 1

        chapters = (await db.execute(
            select(Chapter).where(Chapter.project_id == project_id)
        )).scalars().all()
        total = 0
        terminal = 0
        for ch in chapters:
            ss = (await db.execute(select(Segment).where(Segment.chapter_id == ch.id))).scalars().all()
            total += len(ss)
            t = len([s for s in ss if is_segment_terminal(s)])
            terminal += t
            ch.status = "completed" if ss and t == len(ss) else "translating"

        project.progress = int((terminal / total) * 100) if total else 0
        project.status = "completed" if project.progress == 100 else "translating"
        await db.commit()

        if project.status == "completed":
            export_dir = os.path.abspath("./backend/data/exports")
            os.makedirs(export_dir, exist_ok=True)
            await export_translated_project(project_id, db, export_dir)
        return retried


async def process_project_translation(project_id: str):
    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project:
            logger.warning(f"Project {project_id} not found")
            return
        if project.status not in ["pending_terms", "translating"]:
            logger.warning(f"Project {project_id} not in pending_terms/translating, current: {project.status}")
            return
        project.status = "translating"
        await db.commit()
        try:
            term_dict = await get_confirmed_term_dict(project_id, db)
            translator_config = await get_translator_config(db)
            genre_role, _ = resolve_genre(project)
        except Exception as e:
            logger.error(f"Failed to prepare translation context for {project_id}: {e}")
            project.status = "failed"
            await db.commit()
            return

        logger.info(f"Starting translation for project {project_id} (genre_role={genre_role})")

        chapters = (await db.execute(
            select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
        )).scalars().all()
        logger.info(f"Found {len(chapters)} chapters to translate")
        paused_requested = False

        all_segments = (await db.execute(
            select(Segment).join(Chapter, Segment.chapter_id == Chapter.id)
            .where(Chapter.project_id == project_id)
        )).scalars().all()
        total_segments = len(all_segments)
        terminal_segments = len([s for s in all_segments if is_segment_terminal(s)])

        # 项目级并发池：所有章节共享同一个 semaphore
        project_semaphore = asyncio.Semaphore(TRANSLATION_CONCURRENCY)

        async def process_segment_by_id(segment_id: str, ctx: str) -> bool:
            async with project_semaphore:
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
                            seg, project_id, worker_db, ctx,
                            term_dict=term_dict,
                            translator_config=translator_config,
                            genre_role=genre_role,
                        )
                    except Exception as e:
                        logger.error(f"Failed to translate segment {segment_id}: {e}")
                        seg.status = "failed"
                        seg.retry_count += 1
                        await worker_db.commit()
                        return False

        chapter_processed = 0
        for chapter in chapters:
            if await is_project_paused(project_id):
                paused_requested = True
                break
            if chapter.status == "completed":
                continue

            chapter.status = "translating"
            await db.commit()

            segments = (await db.execute(
                select(Segment).where(Segment.chapter_id == chapter.id).order_by(Segment.order_index)
            )).scalars().all()
            pending = [s for s in segments if not is_segment_terminal(s)]

            chapter_text_for_summary = "\n".join((s.original_text or "")[:200] for s in segments[:30])
            chapter_summary = await summarize_chapter_brief(chapter_text_for_summary, translator_config)

            # 章节标题翻译
            if chapter.title and not chapter.translated_title:
                tt = await translate_text_with_stage(
                    text=chapter.title,
                    project_id=project_id,
                    db=db,
                    context="",
                    stage="one_pass",
                    term_dict=term_dict,
                    translator_config=translator_config,
                    genre_role=genre_role,
                )
                if tt:
                    chapter.translated_title = tt.strip()
                    await db.commit()

            # 1) 先试 batching
            handled_ids = await _try_batch_translate(
                pending, segments, project_id, term_dict, translator_config,
                genre_role, chapter.title or "", chapter_summary,
            )
            terminal_segments += len(handled_ids)

            # 2) 剩余走单段并发
            remaining = [s for s in pending if s.id not in handled_ids]
            tasks = []
            for s in remaining:
                idx_in_chapter = next((i for i, x in enumerate(segments) if x.id == s.id), 0)
                ctx = build_context_window(segments, idx_in_chapter, chapter.title or "", chapter_summary)
                tasks.append(asyncio.create_task(process_segment_by_id(s.id, ctx)))

            chapter_translated = 0
            chapter_paused = False
            if tasks:
                for i, task in enumerate(asyncio.as_completed(tasks), start=1):
                    if await task:
                        chapter_translated += 1
                    if await is_project_paused(project_id):
                        chapter_paused = True
                        for pt in tasks:
                            if not pt.done():
                                pt.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        break
                    if i % PROGRESS_UPDATE_INTERVAL == 0 and total_segments > 0:
                        async with AsyncSessionLocal() as pdb:
                            pp = await pdb.get(Project, project_id)
                            if pp:
                                pp.progress = int((terminal_segments + chapter_translated) / total_segments * 100)
                                await pdb.commit()
                terminal_segments += chapter_translated
            if chapter_paused:
                paused_requested = True
                break

            recovered = await auto_retry_qa_failed_once(
                project_id, chapter.id, term_dict, translator_config, genre_role,
            )
            terminal_segments += recovered

            # 3) 滚动术语扩展
            chapter_processed += 1
            if GLOSSARY_EXPAND_EVERY > 0 and chapter_processed % GLOSSARY_EXPAND_EVERY == 0:
                try:
                    chapter_text = "\n".join((s.original_text or "") for s in segments[:50])[:6000]
                    existing = set(term_dict.keys())
                    new_terms = await expand_glossary_from_chapter(
                        project_id, chapter_text, existing, translator_config,
                    )
                    if new_terms:
                        async with AsyncSessionLocal() as gdb:
                            for t in new_terms:
                                gdb.add(Terminology(
                                    project_id=project_id,
                                    original_term=t["source"],
                                    translated_term=t["target"],
                                    type="自动扩展",
                                    is_confirmed=False,
                                ))
                            await gdb.commit()
                except Exception as e:
                    logger.warning(f"Glossary expansion failed for {chapter.id}: {e}")

            if TRANSLATION_DELAY_SECONDS > 0:
                await asyncio.sleep(TRANSLATION_DELAY_SECONDS)

            async with AsyncSessionLocal() as cdb:
                cobj = await cdb.get(Chapter, chapter.id)
                refreshed = (await cdb.execute(
                    select(Segment).where(Segment.chapter_id == chapter.id)
                )).scalars().all()
                all_done = all(is_segment_terminal(s) for s in refreshed)
                if cobj:
                    cobj.status = "completed" if all_done else "translating"
                    await cdb.commit()

            if total_segments > 0:
                async with AsyncSessionLocal() as pdb:
                    pp = await pdb.get(Project, project_id)
                    if pp:
                        pp.progress = int((terminal_segments / total_segments) * 100)
                        await pdb.commit()

    async with AsyncSessionLocal() as final_db:
        fp = await final_db.get(Project, project_id)
        if not fp:
            return
        fseg = (await final_db.execute(
            select(Segment).join(Chapter, Segment.chapter_id == Chapter.id)
            .where(Chapter.project_id == project_id)
        )).scalars().all()
        ft = len(fseg)
        fterm = len([s for s in fseg if is_segment_terminal(s)])
        fp.progress = int((fterm / ft) * 100) if ft else 0

        if paused_requested or fp.status == "paused":
            fp.status = "paused"
            await final_db.commit()
            logger.info(f"Project {project_id} paused")
            return

        if ft > 0 and fterm == ft:
            fp.status = "completed"
            fp.progress = 100
            logger.info(f"Project {project_id} translation completed")
            try:
                export_dir = os.path.abspath("./backend/data/exports")
                os.makedirs(export_dir, exist_ok=True)
                output_path, output_ext = await export_translated_project(project_id, final_db, export_dir)
                logger.info(f"{output_ext.upper()} exported to {output_path}")
            except Exception as e:
                logger.error(f"Failed to export translated file: {e}")
        else:
            fp.status = "failed"
            logger.warning(f"Project {project_id} completed with failures")
        await final_db.commit()


def start_translation_task(project_id: str):
    """Schedule translation onto the main event loop (thread-safe)."""
    logger.info(f"Attempting to start translation task for project {project_id}")
    main_loop = None
    try:
        from backend.main import MAIN_LOOP as _ml
        main_loop = _ml
    except Exception:
        main_loop = None

    if main_loop is not None and main_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(process_project_translation(project_id), main_loop)
            return
        except RuntimeError:
            pass

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(process_project_translation(project_id))
    except RuntimeError:
        logger.warning(f"No running event loop, creating new one for project {project_id}")
        asyncio.run(process_project_translation(project_id))


async def resume_pending_tasks():
    async with AsyncSessionLocal() as db:
        projects = (await db.execute(
            select(Project).where(Project.status == "translating")
        )).scalars().all()
        for p in projects:
            logger.info(f"Resuming translation for project {p.id}")
            start_translation_task(p.id)