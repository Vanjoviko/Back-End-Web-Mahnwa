"""Endpoint LEGACY (`/api/manga/*`, `/api/chapters/*`) -- dinonaktifkan secara default.

Kode ini dipindahkan dari `app/main.py` tanpa perubahan perilaku, dan hanya dipasang
bila `ENABLE_LEGACY_API=true`. Endpoint legacy TIDAK memakai autentikasi worker dan TIDAK
memakai SSRF guard (lihat F-05/F-10 pada spesifikasi); jangan aktifkan di lingkungan produksi.
"""
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.models import Chapter, ChapterPage, ChapterStatus, Manga, MangaStatus
from app.schemas import (
    ChapterPagesResponse,
    ChapterProgressSummary,
    ImportRequest,
    ImportResponse,
    MangaStatusResponse,
    ScanRequest,
    ScanResponse,
)


def slugify(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '-', text)


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    # =================================================================
    # 1. SCAN MANGA (ADMIN DISCOVERY)
    # =================================================================
    @router.post("/api/manga/scan", response_model=ScanResponse, tags=["Legacy - Import"])
    def scan_manga(req: ScanRequest):
        """Scan link utama komik. (Legacy; memakai ScraperService lama.)"""
        from app.services.scraper import ScraperService  # impor malas: butuh playwright

        try:
            metadata, chapters = ScraperService.scan_manga(req.source_url)
            return {
                "title": metadata["title"],
                "cover_image_url": metadata["cover_image_url"],
                "total_chapters": len(chapters),
                "chapters": chapters
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Gagal melakukan scan: {str(exc)}")

    # =================================================================
    # 2. IMPORT MANGA (START BACKGROUND DOWNLOAD IMAGES)
    # =================================================================
    @router.post("/api/manga/import", response_model=ImportResponse, tags=["Legacy - Import"])
    def import_manga(req: ImportRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
        from app.services.worker import process_chapter_download

        slug = slugify(req.title)
        existing = db.query(Manga).filter(Manga.slug == slug).first()
        if existing:
            raise HTTPException(status_code=409, detail="Manga ini sudah terdaftar di sistem.")

        manga = Manga(
            title=req.title,
            slug=slug,
            source_url=req.source_url,
            cover_image_url=req.cover_image_url,
            status=MangaStatus.IMPORTING
        )
        db.add(manga)
        db.flush()

        chapter_records = []
        for ch in req.chapters:
            record = Chapter(
                manga_id=manga.id,
                chapter_number=ch.chapter_number,
                title=ch.title,
                source_url=ch.source_url,
                status=ChapterStatus.QUEUED
            )
            chapter_records.append(record)

        db.bulk_save_objects(chapter_records)
        db.commit()

        created_chapters = db.query(Chapter).filter(Chapter.manga_id == manga.id).all()
        for ch in created_chapters:
            background_tasks.add_task(process_chapter_download, ch.id)

        return {
            "message": "Import berhasil dimulai. Gambar sedang diunduh di latar belakang.",
            "manga_id": manga.id,
            "total_enqueued": len(created_chapters)
        }

    # =================================================================
    # 3. MONITORING PROGRESS DOWNLOAD (ADMIN DASHBOARD)
    # =================================================================
    @router.get("/api/manga/{manga_id}/status", response_model=MangaStatusResponse, tags=["Legacy - Monitoring"])
    def get_manga_progress(manga_id: int, db: Session = Depends(get_db)):
        manga = db.query(Manga).filter(Manga.id == manga_id).first()
        if not manga:
            raise HTTPException(status_code=404, detail="Manga tidak ditemukan.")

        chapters = db.query(Chapter).filter(Chapter.manga_id == manga.id).all()

        summary = ChapterProgressSummary(
            total=len(chapters),
            completed=sum(1 for c in chapters if c.status == ChapterStatus.COMPLETED),
            downloading=sum(1 for c in chapters if c.status == ChapterStatus.DOWNLOADING),
            queued=sum(1 for c in chapters if c.status == ChapterStatus.QUEUED),
            failed=sum(1 for c in chapters if c.status == ChapterStatus.FAILED)
        )

        return {
            "id": manga.id,
            "title": manga.title,
            "status": manga.status.value,
            "progress": summary
        }

    # =================================================================
    # 4. GET CHAPTER PAGES / BY IMAGE (FRONTEND READER)
    # =================================================================
    @router.get("/api/chapters/{chapter_id}/pages", response_model=ChapterPagesResponse, tags=["Legacy - Reader"])
    def get_chapter_pages(chapter_id: int, db: Session = Depends(get_db)):
        chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
        if not chapter:
            raise HTTPException(status_code=404, detail="Chapter tidak ditemukan.")

        pages = db.query(ChapterPage).filter(
            ChapterPage.chapter_id == chapter_id
        ).order_by(ChapterPage.page_number.asc()).all()

        # BE-10: basis URL dari PUBLIC_BASE_URL (path relatif bila tidak diset).
        base = settings.public_base_url
        return {
            "chapter_id": chapter.id,
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "status": chapter.status.value,
            "total_pages": len(pages),
            "pages": [
                {
                    "page": p.page_number,
                    "url": f"{base}{p.image_url}"
                }
                for p in pages
            ]
        }

    return router
