import ebooklib
from ebooklib import epub
import html
import logging
import os
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Project, Chapter, Segment

logger = logging.getLogger(__name__)

def _collect_epub_text_tags(soup: BeautifulSoup):
    tags = []
    for tag in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'div']):
        text = tag.get_text().strip()
        if len(text) < 2:
            continue
        if tag.parent and tag.parent.name in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li']:
            continue
        tags.append(tag)
    return tags

async def export_epub(project_id: str, db: AsyncSession, output_path: str):
    project = await db.get(Project, project_id)
    if not project:
        raise ValueError("Project not found")

    source_path = project.source_file_path
    if source_path and os.path.exists(source_path):
        try:
            book = epub.read_epub(source_path)

            stmt = select(Chapter).where(Chapter.project_id == project_id)
            chapters = (await db.execute(stmt)).scalars().all()
            chapter_map = {c.file_name: c for c in chapters if c.file_name}

            for item in book.get_items():
                if item.get_type() != ebooklib.ITEM_DOCUMENT:
                    continue

                chapter = chapter_map.get(item.get_name())
                if not chapter:
                    continue

                seg_stmt = select(Segment).where(Segment.chapter_id == chapter.id).order_by(Segment.order_index)
                segments = (await db.execute(seg_stmt)).scalars().all()
                if not segments:
                    continue

                soup = BeautifulSoup(item.get_content(), 'html.parser')
                text_tags = _collect_epub_text_tags(soup)
                replace_count = min(len(text_tags), len(segments))

                for index in range(replace_count):
                    seg = segments[index]
                    text = seg.translated_text if seg.translated_text else seg.original_text
                    text_tags[index].clear()
                    text_tags[index].append(text)

                item.set_content(str(soup).encode('utf-8'))

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            epub.write_epub(output_path, book, {})
            logger.info(f"Exported EPUB to {output_path} with resource preservation")
            return output_path
        except Exception as e:
            logger.warning(f"Source-preserving EPUB export failed, fallback to rebuild: {e}")

    book = epub.EpubBook()
    book.set_identifier(project_id)
    book.set_title(project.name.replace(".epub", " - Translated"))
    book.set_language(project.target_lang)

    stmt = select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
    chapters = (await db.execute(stmt)).scalars().all()

    epub_chapters = []
    
    for chapter in chapters:
        c = epub.EpubHtml(title=chapter.title, file_name=f"chap_{chapter.order_index}.xhtml", lang=project.target_lang)
        
        content = f"<h1>{html.escape(chapter.title)}</h1>\n"
        
        seg_stmt = select(Segment).where(Segment.chapter_id == chapter.id).order_by(Segment.order_index)
        segments = (await db.execute(seg_stmt)).scalars().all()
        
        for seg in segments:
            tag = seg.html_tag if seg.html_tag else "p"
            text = seg.translated_text if seg.translated_text else seg.original_text
            text = html.escape(text)
            content += f"<{tag}>{text}</{tag}>\n"
            
        c.content = content
        book.add_item(c)
        epub_chapters.append(c)

    book.toc = tuple(epub_chapters)
    
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    style = 'BODY {color: black; font-family: Arial, sans-serif;}'
    nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
    book.add_item(nav_css)
    
    book.spine = ['nav'] + epub_chapters
    
    epub.write_epub(output_path, book, {})
    logger.info(f"Exported EPUB to {output_path}")
    return output_path
