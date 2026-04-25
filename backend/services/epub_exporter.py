import html
import json
import logging
import os
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.models.models import Project, Chapter, Segment

logger = logging.getLogger(__name__)


def _tag_css_path(tag, soup) -> str:
    parts = []
    cur = tag
    while cur is not None and getattr(cur, "name", None) and cur is not soup:
        siblings = [s for s in cur.parent.find_all(cur.name, recursive=False)] if cur.parent else [cur]
        idx = siblings.index(cur) + 1 if cur in siblings else 1
        parts.append(f"{cur.name}[{idx}]")
        cur = cur.parent
    return "/" + "/".join(reversed(parts))


def _find_by_css_path(soup, css_path: str):
    if not css_path:
        return None
    parts = [p for p in css_path.split("/") if p]
    cur = soup
    for part in parts:
        m = part.rsplit("[", 1)
        name = m[0]
        try:
            idx = int(m[1].rstrip("]")) - 1 if len(m) == 2 else 0
        except Exception:
            idx = 0
        siblings = list(cur.find_all(name, recursive=False)) if hasattr(cur, "find_all") else []
        if 0 <= idx < len(siblings):
            cur = siblings[idx]
        else:
            return None
    return cur


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


async def export_epub(project_id: str, db: AsyncSession, output_path: str, mode: str = "replace"):
    project = await db.get(Project, project_id)
    if not project:
        raise ValueError("Project not found")

    source_path = project.source_file_path
    if source_path and os.path.exists(source_path):
        try:
            book = epub.read_epub(source_path)
            chapters = (await db.execute(
                select(Chapter).where(Chapter.project_id == project_id)
            )).scalars().all()
            chapter_by_file = {c.file_name: c for c in chapters if c.file_name}

            for item in book.get_items():
                if item.get_type() != ebooklib.ITEM_DOCUMENT:
                    continue
                ch = chapter_by_file.get(item.get_name())
                if not ch:
                    continue
                segments = (await db.execute(
                    select(Segment).where(Segment.chapter_id == ch.id).order_by(Segment.order_index)
                )).scalars().all()
                if not segments:
                    continue

                soup = BeautifulSoup(item.get_content(), "html.parser")

                meta_segs = []
                fallback_segs = []
                for s in segments:
                    if s.html_tag and s.html_tag.startswith("{"):
                        try:
                            meta = json.loads(s.html_tag)
                            if meta.get("format") == "epub_tag" and meta.get("css_path"):
                                meta_segs.append((s, meta))
                                continue
                        except Exception:
                            pass
                    fallback_segs.append(s)

                used_tag_ids = set()
                for s, meta in meta_segs:
                    tag = _find_by_css_path(soup, meta.get("css_path") or "")
                    if not tag:
                        fallback_segs.append(s)
                        continue
                    text = s.translated_text if s.translated_text else s.original_text
                    if mode == "bilingual" and s.translated_text and s.original_text:
                        original = s.original_text
                        tag.clear()
                        tag.append(text or "")
                        new_p = soup.new_tag(meta.get("tag_name") or "p")
                        new_p.append(original)
                        tag.insert_after(new_p)
                    else:
                        tag.clear()
                        tag.append(text or "")
                    used_tag_ids.add(id(tag))

                if fallback_segs:
                    text_tags = [t for t in _collect_epub_text_tags(soup) if id(t) not in used_tag_ids]
                    for i, s in enumerate(fallback_segs):
                        if i >= len(text_tags):
                            break
                        text = s.translated_text if s.translated_text else s.original_text
                        text_tags[i].clear()
                        text_tags[i].append(text or "")

                item.set_content(str(soup).encode("utf-8"))

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            epub.write_epub(output_path, book, {})
            logger.info(f"Exported EPUB (mode={mode}) to {output_path} with css_path matching")
            return output_path
        except Exception as e:
            logger.warning(f"Source-preserving EPUB export failed, fallback to rebuild: {e}")

    # Fallback rebuild
    book = epub.EpubBook()
    book.set_identifier(project_id)
    book.set_title(project.name.replace(".epub", " - Translated"))
    book.set_language(project.target_lang)

    chapters = (await db.execute(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.order_index)
    )).scalars().all()

    epub_chapters = []
    for chapter in chapters:
        title = chapter.translated_title or chapter.title or ""
        c = epub.EpubHtml(title=title, file_name=f"chap_{chapter.order_index}.xhtml", lang=project.target_lang)
        content = f"<h1>{html.escape(title)}</h1>\n"
        segments = (await db.execute(
            select(Segment).where(Segment.chapter_id == chapter.id).order_by(Segment.order_index)
        )).scalars().all()
        for seg in segments:
            tag_name = "p"
            try:
                if seg.html_tag and seg.html_tag.startswith("{"):
                    tag_name = json.loads(seg.html_tag).get("tag_name", "p")
                elif seg.html_tag:
                    tag_name = seg.html_tag
            except Exception:
                pass
            text = seg.translated_text if seg.translated_text else seg.original_text
            text = html.escape(text or "")
            content += f"<{tag_name}>{text}</{tag_name}>\n"
            if mode == "bilingual" and seg.translated_text and seg.original_text:
                content += f"<{tag_name} class=\"orig\">{html.escape(seg.original_text)}</{tag_name}>\n"
        c.content = content
        book.add_item(c)
        epub_chapters.append(c)

    book.toc = tuple(epub_chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    style = "BODY {color: black; font-family: Arial, sans-serif;} .orig {color:#666;font-size:0.9em;}"
    nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
    book.add_item(nav_css)
    book.spine = ["nav"] + epub_chapters

    epub.write_epub(output_path, book, {})
    logger.info(f"Exported EPUB (mode={mode}) to {output_path}")
    return output_path