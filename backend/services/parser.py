import io
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import re
import json
import logging
import os
from typing import Optional
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Project, Chapter, Segment, Terminology
from backend.services.translator import get_translator_config

logger = logging.getLogger(__name__)

PDF_SEGMENT_MAX_CHARS = int(os.getenv("PDF_SEGMENT_MAX_CHARS", "1600"))
PDF_SEGMENT_MIN_CHARS = int(os.getenv("PDF_SEGMENT_MIN_CHARS", "80"))

def _split_long_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r'(?<=[\.!\?。！？])\s+', text)
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

        if re.match(r'^\d+$', line):
            continue

        if buffer and buffer[-1].endswith('-'):
            buffer[-1] = buffer[-1][:-1] + line
        else:
            buffer.append(line)

    if buffer:
        paragraphs.append(" ".join(buffer))

    merged: list[str] = []
    for paragraph in paragraphs:
        paragraph = re.sub(r'\s+', ' ', paragraph).strip()
        if len(paragraph) < 2:
            continue
        if len(paragraph) < min_chars and merged:
            merged[-1] = f"{merged[-1]} {paragraph}".strip()
        else:
            merged.append(paragraph)

    segments: list[str] = []
    for paragraph in merged:
        segments.extend(_split_long_text(paragraph, max_chars=max_chars))

    return [segment for segment in segments if len(segment.strip()) >= 2]

async def extract_terms_with_llm(project_id: str, sample_text: str, db: AsyncSession):
    """
    Extract key terminologies from a sample text using LLM.
    """
    logger.info(f"Starting term extraction for project {project_id}, sample text length: {len(sample_text)}")
    try:
        config = await get_translator_config(db)
        logger.info(f"Got translator config: provider={config.provider}, model={config.model}")
        client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout
        )
        model = config.model
        
        prompt = f"""
        Extract up to 5 key proper nouns (characters, places, organizations, or sci-fi concepts) from the following text.
        Return the result ONLY as a valid JSON array of objects with the following keys:
        "original" (the term in English), "translated" (a suggested transliteration or translation in Chinese), "type" (one of: 角色, 地点, 组织/文明, 科技).
        Do not include any markdown formatting, just the raw JSON.
        
        Text:
        {sample_text[:3000]}
        """
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500
        )
        content = response.choices[0].message.content.strip()
        # Clean markdown code blocks if any
        if content.startswith("```json"):
            content = content[7:-3]
        elif content.startswith("```"):
            content = content[3:-3]
        
        logger.info(f"LLM returned term extraction response: {content[:200]}...")
            
        terms = json.loads(content)
        logger.info(f"Parsed {len(terms)} terms from LLM response")
        
        for term in terms:
            new_term = Terminology(
                project_id=project_id,
                original_term=term.get("original", ""),
                translated_term=term.get("translated", ""),
                type=term.get("type", "其他"),
                is_confirmed=False
            )
            db.add(new_term)
        
        logger.info(f"Successfully extracted {len(terms)} terms for project {project_id}")
            
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM response as JSON: {e}")
    except Exception as e:
        logger.warning(f"Failed to extract terms via LLM: {e}")
        # 不再添加 dummy term，让术语表保持为空

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
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            content = item.get_content()
            soup = BeautifulSoup(content, 'html.parser')
            
            title_tag = soup.find(['h1', 'h2', 'h3', 'title'])
            chapter_title = title_tag.get_text().strip() if title_tag else f"Chapter {chapter_order + 1}"
            
            chapter_id = f"{project_id}_chap_{chapter_order}"
            chapter = Chapter(
                id=chapter_id,
                project_id=project_id,
                order_index=chapter_order,
                title=chapter_title,
                file_name=item.get_name(),
                status="pending"
            )
            db.add(chapter)
            
            segment_order = 0
            for tag in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'div']):
                text = tag.get_text().strip()
                if len(text) < 2 or re.match(r'^\s*$', text):
                    continue
                
                if tag.parent.name in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li']:
                    continue
                    
                segment = Segment(
                    id=f"{chapter_id}_seg_{segment_order}",
                    chapter_id=chapter_id,
                    order_index=segment_order,
                    html_tag=tag.name,
                    original_text=text,
                    status="pending"
                )
                db.add(segment)
                
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
    logger.info(f"Successfully parsed project {project_id}: {chapter_order} chapters")
    return True

def _ocr_page_with_tesseract(page) -> str:
    """对单页 PDF 执行 OCR（可选依赖，失败时返回空字符串）"""
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

async def parse_pdf_to_db(file_path: str, project_id: str, db: AsyncSession, enable_ocr: Optional[bool] = False):
    """解析 PDF 文件到数据库"""
    try:
        import fitz  # PyMuPDF
        
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
    
    # 将 PDF 按页面分组为章节（每 10 页为一个章节）
    pages_per_chapter = 10
    total_pages = len(doc)
    
    for chapter_start in range(0, total_pages, pages_per_chapter):
        chapter_end = min(chapter_start + pages_per_chapter, total_pages)
        chapter_title = f"Pages {chapter_start + 1}-{chapter_end}"
        
        chapter_id = f"{project_id}_chap_{chapter_order}"
        chapter = Chapter(
            id=chapter_id,
            project_id=project_id,
            order_index=chapter_order,
            title=chapter_title,
            file_name=f"pages_{chapter_start + 1}_to_{chapter_end}",
            status="pending"
        )
        db.add(chapter)
        
        segment_order = 0
        
        for page_num in range(chapter_start, chapter_end):
            page = doc[page_num]
            text_dict = page.get_text("dict")
            page_had_text_blocks = False

            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue

                lines = block.get("lines", [])
                line_texts: list[str] = []
                font_sizes: list[float] = []

                for line in lines:
                    spans = line.get("spans", [])
                    span_text = "".join((span.get("text") or "") for span in spans)
                    span_text = re.sub(r"\s+", " ", span_text).strip()
                    if span_text:
                        line_texts.append(span_text)
                    for span in spans:
                        size = span.get("size")
                        if isinstance(size, (int, float)):
                            font_sizes.append(float(size))

                block_text = "\n".join(line_texts).strip()
                if len(block_text) < 2:
                    continue

                page_had_text_blocks = True
                bbox = block.get("bbox") or [0, 0, 0, 0]
                avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 11.0
                meta = {
                    "format": "pdf_text_block",
                    "page": page_num,
                    "bbox": [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])],
                    "font_size": float(max(8.0, min(avg_font_size, 24.0))),
                }

                segment = Segment(
                    id=f"{chapter_id}_seg_{segment_order}",
                    chapter_id=chapter_id,
                    order_index=segment_order,
                    html_tag=json.dumps(meta, ensure_ascii=False),
                    original_text=block_text,
                    status="pending"
                )
                db.add(segment)

                if len(sample_text_for_terms) < 2000:
                    sample_text_for_terms += block_text + "\n"

                segment_order += 1

            if enable_ocr and not page_had_text_blocks:
                ocr_text = _ocr_page_with_tesseract(page)
                paragraphs = _pdf_text_to_segments(
                    ocr_text,
                    min_chars=PDF_SEGMENT_MIN_CHARS,
                    max_chars=PDF_SEGMENT_MAX_CHARS,
                )
                for paragraph in paragraphs:
                    segment = Segment(
                        id=f"{chapter_id}_seg_{segment_order}",
                        chapter_id=chapter_id,
                        order_index=segment_order,
                        html_tag="p",
                        original_text=paragraph,
                        status="pending"
                    )
                    db.add(segment)

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
