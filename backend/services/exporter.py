import logging
import os
import zipfile
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from backend.models.models import Project
from backend.services.epub_exporter import export_epub
from backend.services.pdf_exporter import export_pdf

logger = logging.getLogger(__name__)

# 默认走 PDF。只有在明确识别为 EPUB 时才输出 EPUB。
DEFAULT_EXPORT_EXT = ".pdf"
SUPPORTED_EXPORT_EXTS = {".epub", ".pdf"}
MIME_BY_EXT = {".epub": "application/epub+zip", ".pdf": "application/pdf"}


def _detect_ext_from_file(file_path: str) -> str | None:
    """通过读文件头判断真实类型，比扩展名更可靠。源文件不存在时返回 None。"""
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "rb") as f:
            header = f.read(8)
        if header.startswith(b"%PDF-"):
            return ".pdf"
        if header.startswith(b"PK"):
            try:
                with zipfile.ZipFile(file_path, "r") as zf:
                    if "mimetype" in zf.namelist():
                        mt = zf.read("mimetype").decode("utf-8", errors="ignore").strip()
                        if mt == "application/epub+zip":
                            return ".epub"
            except Exception:
                return None
    except Exception:
        return None
    return None


def _ext_from_suffix(value: str) -> str | None:
    e = Path(value or "").suffix.lower()
    return e if e in SUPPORTED_EXPORT_EXTS else None


def get_project_source_ext(project: Project) -> str:
    """决定导出格式：始终与上传源文件保持一致。

    1) 先按文件名后缀判断（即便磁盘上的源文件已被清理，也能命中）；
    2) 再按真实文件头判断；
    3) 兜底默认 PDF（与解析器优先级保持一致），避免 PDF 项目被误导出成 EPUB。
    """
    src_path = getattr(project, "source_file_path", "") or ""
    name = getattr(project, "name", "") or ""

    # 1) 文件名后缀
    for cand in (src_path, name):
        e = _ext_from_suffix(cand)
        if e:
            logger.debug("export ext resolved by suffix: %r -> %s", cand, e)
            return e

    # 2) 文件头识别
    ext = _detect_ext_from_file(src_path)
    if ext in SUPPORTED_EXPORT_EXTS:
        logger.debug("export ext resolved by header: %r -> %s", src_path, ext)
        return ext

    # 3) 兜底：默认 PDF
    logger.warning(
        "Cannot detect source ext for project %s (path=%r, name=%r), fallback to %s",
        getattr(project, "id", "?"), src_path, name, DEFAULT_EXPORT_EXT,
    )
    return DEFAULT_EXPORT_EXT


def build_output_path(project_id: str, output_dir: str, ext: str, suffix: str = "") -> str:
    export_ext = ext if ext in SUPPORTED_EXPORT_EXTS else DEFAULT_EXPORT_EXT
    os.makedirs(output_dir, exist_ok=True)
    safe_suffix = suffix or ""
    return os.path.join(output_dir, f"{project_id}_translated{safe_suffix}{export_ext}")


def get_export_media_type(ext: str) -> str:
    return MIME_BY_EXT.get(ext, MIME_BY_EXT[DEFAULT_EXPORT_EXT])


async def export_translated_project(
    project_id: str,
    db: AsyncSession,
    output_dir: str,
    output_suffix: str = "",
    mode: str = "replace",
) -> tuple[str, str]:
    project = await db.get(Project, project_id)
    if not project:
        raise ValueError("Project not found")

    ext = get_project_source_ext(project)
    output_path = build_output_path(project_id, output_dir, ext, suffix=output_suffix)

    logger.info(
        "Exporting project %s as %s (mode=%s, source=%r) -> %s",
        project_id, ext, mode, project.source_file_path, output_path,
    )

    if ext == ".pdf":
        await export_pdf(project_id, db, output_path, mode=mode)
    else:
        await export_epub(project_id, db, output_path, mode=mode)
    return output_path, ext