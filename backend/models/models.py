import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Boolean, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from backend.core.database import Base


def generate_uuid():
    return str(uuid.uuid4())


class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    source_file_path = Column(String)
    # created, parsing, pending_terms, translating, paused, completed, failed
    status = Column(String, default="created")
    enable_ocr = Column(Boolean, default=False)
    source_lang = Column(String, default="en")
    target_lang = Column(String, default="zh")
    progress = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    # novel | academic | technical | general
    genre = Column(String, default="general")

    chapters = relationship("Chapter", back_populates="project", cascade="all, delete-orphan")
    terminologies = relationship("Terminology", back_populates="project", cascade="all, delete-orphan")


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    order_index = Column(Integer)
    title = Column(String)
    translated_title = Column(String, nullable=True)
    # pending, translating, completed
    status = Column(String, default="pending")
    file_name = Column(String)  # useful for EPUB structure rebuilding

    project = relationship("Project", back_populates="chapters")
    segments = relationship("Segment", back_populates="chapter", cascade="all, delete-orphan")


class Segment(Base):
    __tablename__ = "segments"

    id = Column(String, primary_key=True, default=generate_uuid)
    chapter_id = Column(String, ForeignKey("chapters.id"))
    order_index = Column(Integer)
    # 对 EPUB：JSON {format:"epub_tag", css_path, item_id, tag_name}
    # 对 PDF：JSON {format:"pdf_text_block", page, bbox, font_size, font, flags, color, ...}
    html_tag = Column(String)
    original_text = Column(Text, nullable=False)
    translated_text = Column(Text)
    # pending, translating, repairing, completed, qa_failed, failed
    status = Column(String, default="pending")
    retry_count = Column(Integer, default=0)

    chapter = relationship("Chapter", back_populates="segments")


class Terminology(Base):
    __tablename__ = "terminologies"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    original_term = Column(String, nullable=False)
    translated_term = Column(String)
    type = Column(String)
    is_confirmed = Column(Boolean, default=False)

    project = relationship("Project", back_populates="terminologies")


class Settings(Base):
    __tablename__ = "settings"

    id = Column(String, primary_key=True, default="default")
    openai_api_key = Column(String, default="")
    openai_base_url = Column(String, default="https://api.openai.com/v1")
    model_name = Column(String, default="gpt-4o-mini")