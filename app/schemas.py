from pydantic import BaseModel
from typing import List, Optional


class ScanRequest(BaseModel):
    source_url: str


class ChapterMeta(BaseModel):
    chapter_number: float
    title: str
    source_url: str


class ScanResponse(BaseModel):
    title: str
    cover_image_url: Optional[str]
    total_chapters: int
    chapters: List[ChapterMeta]


class ImportRequest(BaseModel):
    source_url: str
    title: str
    cover_image_url: Optional[str] = None
    chapters: List[ChapterMeta]


class ImportResponse(BaseModel):
    message: str
    manga_id: int
    total_enqueued: int


class ChapterProgressSummary(BaseModel):
    total: int
    completed: int
    downloading: int
    queued: int
    failed: int


class MangaStatusResponse(BaseModel):
    id: int
    title: str
    status: str
    progress: ChapterProgressSummary


class PageItem(BaseModel):
    page: int
    url: str


class ChapterPagesResponse(BaseModel):
    chapter_id: int
    chapter_number: float
    title: str
    status: str
    total_pages: int
    pages: List[PageItem]