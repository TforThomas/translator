import io
import json
import logging
import os
import re
import asyncio
from typing import Optional

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.models.models import Project, Chapter, Segment, Terminology
from backend.services.translator import get_translator_config

logger = logging.getLogger(__name__)

PDF_SEGMENT_MAX_CHARS = int(os.getenv("PDF_SEGMENT_MAX_CHARS", "1600"))
PDF_SEGMENT_MIN_CHARS = int(os.getenv("PDF_SEGMENT_MIN_CHARS", "80"))
PDF_MERGE_BLOCKS = os.getenv("PDF_MERGE_BLOCKS", "true").lower() in {"1", "true", "yes", "on"}
PDF_SKIP_HEADER_FOOTER = os.getenv("PDF_SKIP_HEADER_FOOTER", "true").lower() in {"1", "true", "yes", "on"}
PDF_HEADER_FOOTER_MARGIN = float(os.getenv("PDF_HEADER_FOOTER_MARGIN", "40"))
PDF_MIN_FONT_SIZE = float(os.getenv("PDF_MIN_FONT_SIZE", "7.0"))
MATH_FONT_HINTS = ("CMSY", "CMMI", "CMEX", "MSAM", "MSBM", "EUFM", "STIX", "MTSY", "MTMI", "Math")


def _split_long_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[\.!\?。！？])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            for i in range(0, len(sentence), max_chars):
                part = sentence[i:i + max_chars].strip()
                if part:
                    chunks.append(part)
            current = ""
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _pdf_text_to_segments(text: str, min_chars: int, max_chars: int) -> list[str]:
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines()]
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in lines:
        if not line:
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        if re.match(r"^\d+$", line):
            continue
        if buffer and buffer[-1].endswith("-"):
            buffer[-1] = buffer[-1][:-1] + line
        else:
            buffer.append(line)
    if buffer:
        paragraphs.append(" ".join(buffer))

    merged: list[str] = []
    for paragraph in paragraphs:
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if len(paragraph) < 2:
            continue
        if len(paragraph) < min_chars and merged:
            merged[-1] = f"{merged[-1]} {paragraph}".strip()
        else:
            merged.append(paragraph)

    segments: list[str] = []
    for paragraph in merged:
        segments.extend(_split_long_text(paragraph, max_chars=max_chars))
    return [s for s in segments if len(s.strip()) >= 2]


async def extract_terms_with_llm(project_id: str, sample_text: str, db: AsyncSession):
    logger.info(f"Starting term extraction for project {project_id}, sample text length: {len(sample_text)}")
    try:
        config = await get_translator_config(db)
        client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
        )
        prompt = f"""
Extract up to 5 key proper nouns (characters, places, organizations, or sci-fi/technical concepts) from the text.
Return ONLY a JSON array of objects with keys: "original" (English), "translated" (suggested Chinese),
"type" (one of: 角色, 地点, 组织/文明, 科技, 其他).
No markdown.

Text:
{sample_text[:3000]}
"""
        response = await client.chat.completions.create(
            model=config.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:-3]
        elif content.startswith("```"):
            content = content[3:-3]
        terms = json.loads(content)
        for term in terms:
            db.add(Terminology(
                project_id=project_id,
                original_term=term.get("original", ""),
                translated_term=term.get("translated", ""),
                type=term.get("type", "其他"),
                is_confirmed=False,
            ))
        logger.info(f"Successfully extracted {len(terms)} terms for project {project_id}")
    except Exception as e:
        logger.warning(f"Failed to extract terms via LLM: {e}")


# ==== EPUB ====

def _tag_css_path(tag, soup) -> str:
    parts = []
    cur = tag
    while cur is not None and getattr(cur, "name", None) and cur is not soup:
        siblings = [s for s in cur.parent.find_all(cur.name, recursive=False)] if cur.parent else [cur]
        idx = siblings.index(cur) + 1 if cur in siblings else 1
        parts.append(f"{cur.name}[{idx}]")
        cur = cur.parent
    return "/" + "/".join(reversed(parts))


def _collect_epub_text_tags(soup: BeautifulSoup):
    tags = []
    for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "div"]):
        text = tag.get_text().strip()
        if len(text) < 2:
            continue
        if tag.parent and tag.parent.name in ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]:
            continue
        tags.append(tag)
    return tags


async def parse_epub_to_db(file_path: str, project_id: str, db: AsyncSession):
    try:
        book = epub.read_epub(file_path)
    except Exception as e:
        logger.error(f"Error reading EPUB: {e}")
        project = await db.get(Project, project_id)
        if project:
            project.status = "failed"
            await db.commit()
        return False

    chapter_order = 0
    sample_text_for_terms = ""
    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        title_tag = soup.find(["h1", "h2", "h3", "title"])
        chapter_title = title_tag.get_text().strip() if title_tag else f"Chapter {chapter_order + 1}"

        chapter_id = f"{project_id}_chap_{chapter_order}"
        db.add(Chapter(
            id=chapter_id,
            project_id=project_id,
            order_index=chapter_order,
            title=chapter_title,
            file_name=item.get_name(),
            status="pending",
        ))

        segment_order = 0
        for tag in _collect_epub_text_tags(soup):
            text = tag.get_text().strip()
            if len(text) < 2:
                continue
            meta = {
                "format": "epub_tag",
                "item_id": item.get_id() or item.get_name() or "",
                "tag_name": tag.name,
                "css_path": _tag_css_path(tag, soup),
            }
            db.add(Segment(
                id=f"{chapter_id}_seg_{segment_order}",
                chapter_id=chapter_id,
                order_index=segment_order,
                html_tag=json.dumps(meta, ensure_ascii=False),
                original_text=text,
                status="pending",
            ))
            if len(sample_text_for_terms) < 2000:
                sample_text_for_terms += text + "\n"
            segment_order += 1
        chapter_order += 1

    if sample_text_for_terms:
        await extract_terms_with_llm(project_id, sample_text_for_terms, db)

    project = await db.get(Project, project_id)
    if project:
        project.status = "pending_terms"
    await db.commit()
    logger.info(f"Successfully parsed EPUB project {project_id}: {chapter_order} chapters")
    return True


# ==== PDF ====

async def _ocr_page_with_tesseract(page) -> str:
    """异步 OCR：在线程池里跑，避免阻塞事件循环。"""
    def _run():
        try:
            import fitz
            import pytesseract
            from PIL import Image
            matrix = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=matrix)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            text = pytesseract.image_to_string(image)
            return text.strip()
        except Exception as e:
            logger.warning(f"OCR page failed: {e}")
            return ""
    return await asyncio.to_thread(_run)


def _is_header_footer(bbox, page_height: float, margin: float) -> bool:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    y0, y1 = float(bbox[1]), float(bbox[3])
    return y1 < margin or y0 > (page_height - margin)


def _looks_like_math(span_font: str) -> bool:
    if not span_font:
        return False
    return any(h.lower() in span_font.lower() for h in MATH_FONT_HINTS)


def _bbox_intersects_image(bbox, image_rects) -> bool:
    try:
        x0, y0, x1, y1 = bbox
    except Exception:
        return False
    for rx0, ry0, rx1, ry1 in image_rects:
        if not (x1 < rx0 or rx1 < x0 or y1 < ry0 or ry1 < y0):
            return True
    return False


def _reorder_columns(blocks: list[dict], page_width: float) -> list[dict]:
    """简单双栏检测：按 bbox.x0 聚类成左右两列后串联。"""
    if not blocks:
        return blocks
    if len(blocks) < 4:
        return blocks
    mid = page_width / 2.0
    left = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2.0 <= mid]
    right = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2.0 > mid]
    if not left or not right or min(len(left), len(right)) < max(2, len(blocks) // 6):
        return blocks
    left.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
    right.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
    return left + right


async def parse_pdf_to_db(file_path: str, project_id: str, db: AsyncSession, enable_ocr: Optional[bool] = False):
    try:
        import fitz
        doc = fitz.open(file_path)
    except Exception as e:
        logger.error(f"Error reading PDF: {e}")
        project = await db.get(Project, project_id)
        if project:
            project.status = "failed"
            await db.commit()
        return False

    chapter_order = 0
    sample_text_for_terms = ""
    pages_per_chapter = 10
    total_pages = len(doc)

    for chapter_start in range(0, total_pages, pages_per_chapter):
        chapter_end = min(chapter_start + pages_per_chapter, total_pages)
        chapter_id = f"{project_id}_chap_{chapter_order}"
        db.add(Chapter(
            id=chapter_id,
            project_id=project_id,
            order_index=chapter_order,
            title=f"Pages {chapter_start + 1}-{chapter_end}",
            file_name=f"pages_{chapter_start + 1}_to_{chapter_end}",
            status="pending",
        ))
        segment_order = 0

        for page_num in range(chapter_start, chapter_end):
            page = doc[page_num]
            page_height = float(page.rect.height)
            page_width = float(page.rect.width)

            # 收集图像位置（用于公式 / 插图保护）
            image_rects = []
            try:
                for img_info in page.get_image_info(xrefs=True):
                    bbox = img_info.get("bbox")
                    if bbox and len(bbox) == 4:
                        image_rects.append(tuple(float(v) for v in bbox))
            except Exception:
                pass

            text_dict = page.get_text("dict")
            page_blocks: list[dict] = []
            page_had_text_blocks = False

            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                bbox = block.get("bbox") or [0, 0, 0, 0]
                if PDF_SKIP_HEADER_FOOTER and _is_header_footer(bbox, page_height, PDF_HEADER_FOOTER_MARGIN):
                    continue

                lines = block.get("lines", [])
                line_texts: list[str] = []
                line_xs0: list[float] = []
                line_xs1: list[float] = []
                font_sizes: list[float] = []
                font_names: list[str] = []
                bold_count = 0
                italic_count = 0
                color_int = 0
                math_lines = 0
                intersects_image = _bbox_intersects_image(bbox, image_rects)

                for line in lines:
                    spans = line.get("spans", [])
                    span_text = "".join((span.get("text") or "") for span in spans)
                    span_text = re.sub(r"\s+", " ", span_text).strip()
                    if span_text:
                        line_texts.append(span_text)
                        line_bbox = line.get("bbox") or bbox
                        line_xs0.append(float(line_bbox[0]))
                        line_xs1.append(float(line_bbox[2]))
                    line_is_math = False
                    for span in spans:
                        size = span.get("size")
                        if isinstance(size, (int, float)):
                            font_sizes.append(float(size))
                        font = span.get("font") or ""
                        font_names.append(font)
                        flags = int(span.get("flags") or 0)
                        # PyMuPDF flags: 1 superscript, 2 italic, 4 serifed, 8 monospaced, 16 bold
                        if flags & 16:
                            bold_count += 1
                        if flags & 2:
                            italic_count += 1
                        if not color_int:
                            try:
                                color_int = int(span.get("color") or 0)
                            except Exception:
                                color_int = 0
                        if _looks_like_math(font):
                            line_is_math = True
                    if line_is_math:
                        math_lines += 1

                block_text = "\n".join(line_texts).strip()
                if len(block_text) < 2:
                    continue

                avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 11.0
                if avg_font_size < PDF_MIN_FONT_SIZE:
                    avg_font_size = PDF_MIN_FONT_SIZE
                primary_font = max(set(font_names), key=font_names.count) if font_names else ""
                is_bold = bold_count >= max(1, len(font_names) // 2)
                is_italic = italic_count >= max(1, len(font_names) // 2)
                is_math = math_lines >= max(1, len(lines) // 2) or intersects_image

                meta = {
                    "format": "pdf_text_block",
                    "page": page_num,
                    "bbox": [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])],
                    "font_size": float(max(8.0, min(avg_font_size, 24.0))),
                    "font": primary_font,
                    "bold": bool(is_bold),
                    "italic": bool(is_italic),
                    "color": int(color_int),
                    "line_xs0": line_xs0,
                    "line_xs1": line_xs1,
                    "intersect_image": bool(intersects_image),
                    "skip_translate": bool(is_math),
                }
                page_blocks.append({
                    "text": block_text,
                    "meta": meta,
                    "bbox": meta["bbox"],
                })
                page_had_text_blocks = True

            page_blocks = _reorder_columns(page_blocks, page_width)

            if PDF_MERGE_BLOCKS and page_blocks:
                merged: list[dict] = []
                for blk in page_blocks:
                    if not merged:
                        merged.append(blk); continue
                    last = merged[-1]
                    same_size = abs(last["meta"]["font_size"] - blk["meta"]["font_size"]) < 0.6
                    same_font = last["meta"].get("font") == blk["meta"].get("font")
                    vertical_close = abs(blk["bbox"][1] - last["bbox"][3]) < blk["meta"]["font_size"] * 1.4
                    not_image = not (last["meta"].get("intersect_image") or blk["meta"].get("intersect_image"))
                    short_last = len(last["text"]) < PDF_SEGMENT_MAX_CHARS // 2
                    if same_size and same_font and vertical_close and not_image and short_last:
                        last["text"] = (last["text"].rstrip() + " " + blk["text"].lstrip()).strip()
                        last["bbox"] = [
                            min(last["bbox"][0], blk["bbox"][0]),
                            min(last["bbox"][1], blk["bbox"][1]),
                            max(last["bbox"][2], blk["bbox"][2]),
                            max(last["bbox"][3], blk["bbox"][3]),
                        ]
                        last["meta"]["bbox"] = last["bbox"]
                        last["meta"]["line_xs0"].extend(blk["meta"].get("line_xs0", []))
                        last["meta"]["line_xs1"].extend(blk["meta"].get("line_xs1", []))
                    else:
                        merged.append(blk)
                page_blocks = merged

            for blk in page_blocks:
                db.add(Segment(
                    id=f"{chapter_id}_seg_{segment_order}",
                    chapter_id=chapter_id,
                    order_index=segment_order,
                    html_tag=json.dumps(blk["meta"], ensure_ascii=False),
                    original_text=blk["text"],
                    status="pending",
                ))
                if len(sample_text_for_terms) < 2000:
                    sample_text_for_terms += blk["text"] + "\n"
                segment_order += 1

            if enable_ocr and not page_had_text_blocks:
                ocr_text = await _ocr_page_with_tesseract(page)
                paragraphs = _pdf_text_to_segments(ocr_text, PDF_SEGMENT_MIN_CHARS, PDF_SEGMENT_MAX_CHARS)
                for paragraph in paragraphs:
                    db.add(Segment(
                        id=f"{chapter_id}_seg_{segment_order}",
                        chapter_id=chapter_id,
                        order_index=segment_order,
                        html_tag="p",
                        original_text=paragraph,
                        status="pending",
                    ))
                    if len(sample_text_for_terms) < 2000:
                        sample_text_for_terms += paragraph + "\n"
                    segment_order += 1

        chapter_order += 1

    doc.close()

    if sample_text_for_terms:
        await extract_terms_with_llm(project_id, sample_text_for_terms, db)
    project = await db.get(Project, project_id)
    if project:
        project.status = "pending_terms"
    await db.commit()
    logger.info(f"Successfully parsed PDF project {project_id}: {chapter_order} chapters from {total_pages} pages")
    return True