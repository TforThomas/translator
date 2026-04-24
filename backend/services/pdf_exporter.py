import logging
import os
import re
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Project, Chapter, Segment

logger = logging.getLogger(__name__)

PDF_PAGE_WIDTH = 595
PDF_PAGE_HEIGHT = 842
PDF_MARGIN_X = 50
PDF_MARGIN_TOP = 60
PDF_MARGIN_BOTTOM = 60
PDF_BODY_FONT_SIZE = 11
PDF_TITLE_FONT_SIZE = 15
PDF_LINE_HEIGHT = 18
PDF_MAX_CHARS_PER_LINE = 44


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _wrap_text(text: str, max_chars: int = PDF_MAX_CHARS_PER_LINE) -> list[str]:
    normalized = _normalize_spaces(text)
    if not normalized:
        return [""]

    lines: list[str] = []
    index = 0
    while index < len(normalized):
        lines.append(normalized[index:index + max_chars])
        index += max_chars
    return lines


async def export_pdf(project_id: str, db: AsyncSession, output_path: str):
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for PDF export") from exc

    project = await db.get(Project, project_id)
    if not project:
        raise ValueError("Project not found")

    source_pdf_path = project.source_file_path
    if source_pdf_path and os.path.exists(source_pdf_path):
        stmt = (
            select(Segment)
            .join(Chapter, Segment.chapter_id == Chapter.id)
            .where(Chapter.project_id == project_id)
            .order_by(Chapter.order_index, Segment.order_index)
        )
        segments = (await db.execute(stmt)).scalars().all()

        page_updates: dict[int, list[tuple["fitz.Rect", str, float]]] = {}
        for seg in segments:
            meta_raw = seg.html_tag or ""
            if not (meta_raw.startswith("{") and "pdf_text_block" in meta_raw):
                continue

            try:
                meta = json.loads(meta_raw)
                page_index = int(meta.get("page", -1))
                bbox = meta.get("bbox", [])
                if page_index < 0 or not isinstance(bbox, list) or len(bbox) != 4:
                    continue

                rect = fitz.Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
                if rect.is_empty or rect.width <= 1 or rect.height <= 1:
                    continue

                text = (seg.translated_text or seg.original_text or "").strip()
                if not text:
                    continue

                font_size = float(meta.get("font_size", PDF_BODY_FONT_SIZE))
                font_size = max(8.0, min(font_size, 24.0))
                page_updates.setdefault(page_index, []).append((rect, text, font_size))
            except Exception:
                continue

        if page_updates:
            doc = fitz.open(source_pdf_path)
            for page_index, replacements in page_updates.items():
                if page_index < 0 or page_index >= len(doc):
                    continue
                page = doc[page_index]

                for rect, _, _ in replacements:
                    page.add_redact_annot(rect, fill=(1, 1, 1))

                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

                for rect, text, font_size in replacements:
                    page.insert_textbox(
                        rect,
                        text,
                        fontsize=font_size,
                        fontname="china-s",
                        align=fitz.TEXT_ALIGN_LEFT,
                    )

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            doc.save(output_path)
            doc.close()
            logger.info(f"Exported PDF to {output_path} with layout preservation")
            return output_path

    stmt = select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
    chapters = (await db.execute(stmt)).scalars().all()

    doc = fitz.open()
    page = doc.new_page(width=PDF_PAGE_WIDTH, height=PDF_PAGE_HEIGHT)
    y = PDF_MARGIN_TOP

    def add_line(text: str, fontsize: int, extra_spacing: int = 0):
        nonlocal page, y
        if y + PDF_LINE_HEIGHT > PDF_PAGE_HEIGHT - PDF_MARGIN_BOTTOM:
            page = doc.new_page(width=PDF_PAGE_WIDTH, height=PDF_PAGE_HEIGHT)
            y = PDF_MARGIN_TOP

        page.insert_text(
            (PDF_MARGIN_X, y),
            text,
            fontsize=fontsize,
            fontname="china-s",
        )
        y += PDF_LINE_HEIGHT + extra_spacing

    for chapter in chapters:
        chapter_title = chapter.title or f"Chapter {chapter.order_index + 1}"
        for line in _wrap_text(chapter_title, max_chars=34):
            add_line(line, fontsize=PDF_TITLE_FONT_SIZE)
        y += 6

        seg_stmt = select(Segment).where(Segment.chapter_id == chapter.id).order_by(Segment.order_index)
        segments = (await db.execute(seg_stmt)).scalars().all()

        for seg in segments:
            text = seg.translated_text if seg.translated_text else seg.original_text
            wrapped_lines = _wrap_text(text)
            for line in wrapped_lines:
                add_line(line, fontsize=PDF_BODY_FONT_SIZE)
            y += 4

        y += 10

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)
    doc.close()

    logger.info(f"Exported PDF to {output_path} with fallback flow")
    return output_path
