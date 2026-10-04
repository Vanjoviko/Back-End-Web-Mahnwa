import enum
from datetime import datetime
from sqlalchemy import Column, Integer, Float, String, Text, DateTime, ForeignKey, Enum, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class MangaStatus(str, enum.Enum):
    IMPORTING = "IMPORTING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class ChapterStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Manga(Base):
    __tablename__ = "mangas"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, index=True, nullable=False)
    source_url = Column(Text, nullable=False)
    cover_image_url = Column(Text, nullable=True)
    status = Column(Enum(MangaStatus), default=MangaStatus.IMPORTING, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    chapters = relationship("Chapter", back_populates="manga", cascade="all, delete-orphan")


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(Integer, primary_key=True, index=True)
    manga_id = Column(Integer, ForeignKey("mangas.id"), nullable=False, index=True)
    chapter_number = Column(Float, nullable=False)
    title = Column(String(255), nullable=False)
    source_url = Column(Text, nullable=False)
    status = Column(Enum(ChapterStatus), default=ChapterStatus.QUEUED, nullable=False, index=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    manga = relationship("Manga", back_populates="chapters")
    pages = relationship(
        "ChapterPage",
        back_populates="chapter",
        order_by="ChapterPage.page_number.asc()",
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("manga_id", "chapter_number", name="uq_manga_chapter"),
    )


class ChapterPage(Base):
    __tablename__ = "chapter_pages"

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id"), nullable=False, index=True)
    page_number = Column(Integer, nullable=False)  # 1, 2, 3, ...
    image_url = Column(String(500), nullable=False)  # Path endpoint statis web: /static/manga/...
    created_at = Column(DateTime, default=datetime.utcnow)

    chapter = relationship("Chapter", back_populates="pages")