import json
import logging
import os
import re
from pathlib import Path
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
PDF_FIRST_LINE_INDENT = os.getenv("PDF_FIRST_LINE_INDENT", "true").lower() in {"1", "true", "yes", "on"}
CN_FONT_NAME = os.getenv("CN_FONT_NAME", "china-s")
CN_FONT_BOLD = os.getenv("CN_FONT_BOLD", CN_FONT_NAME)
CN_FONT_FILE = os.getenv("CN_FONT_FILE", "")
CN_FONT_BOLD_FILE = os.getenv("CN_FONT_BOLD_FILE", "")


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _wrap_text(text: str, max_chars: int = PDF_MAX_CHARS_PER_LINE) -> list[str]:
    normalized = _normalize_spaces(text)
    if not normalized:
        return [""]
    return [normalized[i:i + max_chars] for i in range(0, len(normalized), max_chars)]


def _color_int_to_rgb(color_int):
    try:
        c = int(color_int)
        return ((c >> 16) & 0xFF) / 255.0, ((c >> 8) & 0xFF) / 255.0, (c & 0xFF) / 255.0
    except Exception:
        return 0.0, 0.0, 0.0


def _infer_alignment(line_xs0, line_xs1, rect_x0, rect_x1):
    import fitz
    if not line_xs0 or not line_xs1:
        return fitz.TEXT_ALIGN_LEFT
    width = max(rect_x1 - rect_x0, 1.0)
    left_margins = [abs(x0 - rect_x0) for x0 in line_xs0]
    right_margins = [abs(rect_x1 - x1) for x1 in line_xs1]
    if all(lm < width * 0.05 for lm in left_margins) and all(rm < width * 0.08 for rm in right_margins):
        return fitz.TEXT_ALIGN_JUSTIFY
    avg_left = sum(left_margins) / len(left_margins)
    avg_right = sum(right_margins) / len(right_margins)
    if abs(avg_left - avg_right) < width * 0.05:
        return fitz.TEXT_ALIGN_CENTER
    if avg_right < avg_left:
        return fitz.TEXT_ALIGN_RIGHT
    return fitz.TEXT_ALIGN_LEFT


_FONT_CACHE: dict[int, set[str]] = {}


def _ensure_cn_fonts(doc):
    """在需要时为 doc 所有页面嵌入中文字体文件。"""
    key = id(doc)
    inserted = _FONT_CACHE.setdefault(key, set())
    if CN_FONT_FILE and CN_FONT_NAME not in inserted and os.path.exists(CN_FONT_FILE):
        for page in doc:
            try:
                page.insert_font(fontname=CN_FONT_NAME, fontfile=CN_FONT_FILE)
            except Exception as e:
                logger.warning(f"insert_font {CN_FONT_NAME} failed: {e}")
                break
        inserted.add(CN_FONT_NAME)
    if CN_FONT_BOLD_FILE and CN_FONT_BOLD not in inserted and os.path.exists(CN_FONT_BOLD_FILE):
        for page in doc:
            try:
                page.insert_font(fontname=CN_FONT_BOLD, fontfile=CN_FONT_BOLD_FILE)
            except Exception as e:
                logger.warning(f"insert_font {CN_FONT_BOLD} failed: {e}")
                break
        inserted.add(CN_FONT_BOLD)


def _resolve_fontname(meta: dict) -> str:
    if meta.get("bold"):
        return CN_FONT_BOLD or CN_FONT_NAME or "china-s"
    return CN_FONT_NAME or "china-s"


def _insert_with_autosize(page, rect, text, fontname, fontsize, color, align):
    """逐次缩字号尝试填进 rect；仍溢出就向下扩一行。"""
    fs = float(fontsize)
    min_fs = max(7.0, fs * 0.6)
    while fs >= min_fs:
        rc = page.insert_textbox(
            rect, text,
            fontsize=fs, fontname=fontname, color=color, align=align,
        )
        if rc >= 0:
            return True
        fs -= 0.5
    try:
        import fitz
        expanded = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1 + (rect.height or fontsize))
        page.insert_textbox(
            expanded, text,
            fontsize=max(min_fs, 7.0), fontname=fontname, color=color, align=align,
        )
    except Exception:
        pass
    return False


async def _translate_toc(doc, project_id: str, db: AsyncSession):
    """根据 chapter.translated_title 改写 TOC。"""
    chapters = (await db.execute(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
    )).scalars().all()
    title_map = {(c.title or "").strip(): (c.translated_title or "").strip()
                 for c in chapters if c.title and c.translated_title}
    if not title_map:
        return
    try:
        toc = doc.get_toc(simple=False)
    except Exception:
        toc = []
    if not toc:
        return
    new_toc = []
    for entry in toc:
        level = entry[0]
        title = entry[1]
        page = entry[2]
        rest = entry[3] if len(entry) > 3 else None
        new_title = title_map.get((title or "").strip(), title)
        if rest is not None:
            new_toc.append([level, new_title, page, rest])
        else:
            new_toc.append([level, new_title, page])
    try:
        doc.set_toc(new_toc)
    except Exception as e:
        logger.warning(f"set_toc failed: {e}")


async def export_pdf(project_id: str, db: AsyncSession, output_path: str, mode: str = "replace"):
    """mode: 'replace' (覆盖原文) | 'bilingual' (保留原文，在下方写译文)"""
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for PDF export") from exc

    project = await db.get(Project, project_id)
    if not project:
        raise ValueError("Project not found")

    source_pdf_path = project.source_file_path
    if source_pdf_path and os.path.exists(source_pdf_path):
        segments = (await db.execute(
            select(Segment).join(Chapter, Segment.chapter_id == Chapter.id)
            .where(Chapter.project_id == project_id)
            .order_by(Chapter.order_index, Segment.order_index)
        )).scalars().all()

        page_updates: dict[int, list[tuple]] = {}
        for seg in segments:
            meta_raw = seg.html_tag or ""
            if not (meta_raw.startswith("{") and "pdf_text_block" in meta_raw):
                continue
            try:
                meta = json.loads(meta_raw)
            except Exception:
                continue
            page_index = int(meta.get("page", -1))
            bbox = meta.get("bbox", [])
            if page_index < 0 or not isinstance(bbox, list) or len(bbox) != 4:
                continue
            rect = fitz.Rect(*[float(v) for v in bbox])
            if rect.is_empty or rect.width <= 1 or rect.height <= 1:
                continue
            translated = (seg.translated_text or "").strip()
            original = (seg.original_text or "").strip()
            if not translated and not original:
                continue
            if meta.get("skip_translate") and not translated:
                # 公式 / 插图区块：原样保留
                continue
            page_updates.setdefault(page_index, []).append((rect, translated, original, meta))

        if page_updates:
            doc = fitz.open(source_pdf_path)
            _ensure_cn_fonts(doc)

            for page_index, replacements in page_updates.items():
                if page_index < 0 or page_index >= len(doc):
                    continue
                page = doc[page_index]

                for rect, translated, original, meta in replacements:
                    if mode == "bilingual":
                        continue
                    if meta.get("intersect_image"):
                        continue
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

                for rect, translated, original, meta in replacements:
                    text = translated or original
                    if not text:
                        continue
                    fontname = _resolve_fontname(meta)
                    fontsize = float(meta.get("font_size", PDF_BODY_FONT_SIZE))
                    color = _color_int_to_rgb(meta.get("color", 0))
                    align = _infer_alignment(
                        meta.get("line_xs0") or [], meta.get("line_xs1") or [],
                        rect.x0, rect.x1,
                    )
                    if mode == "bilingual" and translated:
                        try:
                            below = fitz.Rect(
                                rect.x0, rect.y1 + 2,
                                rect.x1, rect.y1 + 2 + max(rect.height, fontsize * 1.6),
                            )
                            _insert_with_autosize(page, below, translated, fontname, fontsize, color, align)
                        except Exception:
                            pass
                    else:
                        _insert_with_autosize(page, rect, text, fontname, fontsize, color, align)

            await _translate_toc(doc, project_id, db)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            doc.save(output_path, garbage=4, deflate=True, clean=True)
            doc.close()
            logger.info(f"Exported PDF (mode={mode}) to {output_path} with layout preservation")
            return output_path

    # Fallback: build a simple flow PDF
    chapters = (await db.execute(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
    )).scalars().all()

    doc = fitz.open()
    _ensure_cn_fonts(doc)
    page = doc.new_page(width=PDF_PAGE_WIDTH, height=PDF_PAGE_HEIGHT)
    y = PDF_MARGIN_TOP

    def add_line(text: str, fontsize: int, extra_spacing: int = 0, fontname: str = CN_FONT_NAME):
        nonlocal page, y
        if y + PDF_LINE_HEIGHT > PDF_PAGE_HEIGHT - PDF_MARGIN_BOTTOM:
            page = doc.new_page(width=PDF_PAGE_WIDTH, height=PDF_PAGE_HEIGHT)
            y = PDF_MARGIN_TOP
        page.insert_text((PDF_MARGIN_X, y), text, fontsize=fontsize, fontname=fontname)
        y += PDF_LINE_HEIGHT + extra_spacing

    for chapter in chapters:
        ctitle = chapter.translated_title or chapter.title or f"Chapter {chapter.order_index + 1}"
        for line in _wrap_text(ctitle, max_chars=34):
            add_line(line, fontsize=PDF_TITLE_FONT_SIZE, fontname=CN_FONT_BOLD)
        y += 6
        segments = (await db.execute(
            select(Segment).where(Segment.chapter_id == chapter.id).order_by(Segment.order_index)
        )).scalars().all()
        for seg in segments:
            text = seg.translated_text if seg.translated_text else seg.original_text
            wrapped = _wrap_text(text or "")
            for i, line in enumerate(wrapped):
                if i == 0 and PDF_FIRST_LINE_INDENT:
                    line = "    " + line
                add_line(line, fontsize=PDF_BODY_FONT_SIZE)
            y += 4
            if mode == "bilingual" and seg.translated_text and seg.original_text:
                for line in _wrap_text(seg.original_text):
                    add_line(line, fontsize=PDF_BODY_FONT_SIZE - 1, fontname=CN_FONT_NAME)
                y += 4
        y += 10

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()
    logger.info(f"Exported PDF (mode={mode}) to {output_path} with fallback flow")
    return output_path