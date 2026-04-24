import os
import zipfile
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from backend.models.models import Project
from backend.services.epub_exporter import export_epub
from backend.services.pdf_exporter import export_pdf

DEFAULT_EXPORT_EXT = ".epub"
SUPPORTED_EXPORT_EXTS = {".epub", ".pdf"}
MIME_BY_EXT = {
    ".epub": "application/epub+zip",
    ".pdf": "application/pdf",
}


def _detect_ext_from_file(file_path: str) -> str | None:
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
                        mimetype_content = zf.read("mimetype").decode("utf-8", errors="ignore").strip()
                        if mimetype_content == "application/epub+zip":
                            return ".epub"
            except Exception:
                return None
    except Exception:
        return None

    return None


def get_project_source_ext(project: Project) -> str:
    detected_ext = _detect_ext_from_file(project.source_file_path or "")
    if detected_ext in SUPPORTED_EXPORT_EXTS:
        return detected_ext

    source_candidates = [project.source_file_path or "", project.name or ""]
    for candidate in source_candidates:
        ext = Path(candidate).suffix.lower()
        if ext in SUPPORTED_EXPORT_EXTS:
            return ext
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
) -> tuple[str, str]:
    project = await db.get(Project, project_id)
    if not project:
        raise ValueError("Project not found")

    ext = get_project_source_ext(project)
    output_path = build_output_path(project_id, output_dir, ext, suffix=output_suffix)

    if ext == ".pdf":
        await export_pdf(project_id, db, output_path)
    else:
        await export_epub(project_id, db, output_path)

    return output_path, ext
