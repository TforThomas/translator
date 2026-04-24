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
    status = Column(String, default="created")  # created, parsing, pending_terms, translating, completed, failed
    enable_ocr = Column(Boolean, default=False)
    source_lang = Column(String, default="en")
    target_lang = Column(String, default="zh")
    progress = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    chapters = relationship("Chapter", back_populates="project", cascade="all, delete-orphan")
    terminologies = relationship("Terminology", back_populates="project", cascade="all, delete-orphan")

class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    order_index = Column(Integer)
    title = Column(String)
    status = Column(String, default="pending")  # pending, translating, completed
    file_name = Column(String)  # useful for EPUB structure rebuilding
    
    project = relationship("Project", back_populates="chapters")
    segments = relationship("Segment", back_populates="chapter", cascade="all, delete-orphan")

class Segment(Base):
    __tablename__ = "segments"

    id = Column(String, primary_key=True, default=generate_uuid)
    chapter_id = Column(String, ForeignKey("chapters.id"))
    order_index = Column(Integer)
    html_tag = Column(String)  # e.g., "p", "h1"
    original_text = Column(Text, nullable=False)
    translated_text = Column(Text)
    status = Column(String, default="pending")  # pending, translating, qa_failed, completed
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
