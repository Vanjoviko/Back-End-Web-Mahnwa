import os
import re
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.database import engine, Base, get_db
from app.models import Manga, Chapter, ChapterPage, MangaStatus, ChapterStatus
from app.schemas import (
    ScanRequest, ScanResponse,
    ImportRequest, ImportResponse,
    MangaStatusResponse, ChapterProgressSummary,
    ChapterPagesResponse
)
from app.services.scraper import ScraperService
from app.services.worker import process_chapter_download

# Membuat tabel-tabel SQLite/PostgreSQL otomatis saat startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Manga Aggregator & Image Reader API",
    version="2.0.0",
    description="Sistem Scraper & Reader Manga berbasis potongan gambar (by-image)."
)

# Mount folder storage agar file gambar lokal bisa langsung dibuka di browser via /static/...
os.makedirs("storage/manga", exist_ok=True)
app.mount("/static", StaticFiles(directory="storage"), name="static")


def slugify(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '-', text)


# =====================================================================
# 1. SCAN MANGA (ADMIN DISCOVERY)
# =====================================================================
@app.post("/api/manga/scan", response_model=ScanResponse, tags=["Admin - Import"])
def scan_manga(req: ScanRequest):
    """
    Scan link utama komik (misal Kiryuu).
    Mengambil judul, gambar cover, dan daftar seluruh chapter.
    """
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


# =====================================================================
# 2. IMPORT MANGA (START BACKGROUND DOWNLOAD IMAGES)
# =====================================================================
@app.post("/api/manga/import", response_model=ImportResponse, tags=["Admin - Import"])
def import_manga(req: ImportRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Daftarkan manga ke database dan jalankan background worker
    untuk mengunduh seluruh potongan gambar per bab secara berurutan.
    """
    slug = slugify(req.title)
    existing = db.query(Manga).filter(Manga.slug == slug).first()
    if existing:
        raise HTTPException(status_code=409, detail="Manga ini sudah terdaftar di sistem.")

    # 1. Simpan Manga ke database
    manga = Manga(
        title=req.title,
        slug=slug,
        source_url=req.source_url,
        cover_image_url=req.cover_image_url,
        status=MangaStatus.IMPORTING
    )
    db.add(manga)
    db.flush()

    # 2. Bulk Insert Chapters
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

    # 3. Masukkan ke antrean download di background
    created_chapters = db.query(Chapter).filter(Chapter.manga_id == manga.id).all()
    for ch in created_chapters:
        background_tasks.add_task(process_chapter_download, ch.id)

    return {
        "message": "Import berhasil dimulai. Gambar sedang diunduh di latar belakang.",
        "manga_id": manga.id,
        "total_enqueued": len(created_chapters)
    }


# =====================================================================
# 3. MONITORING PROGRESS DOWNLOAD (ADMIN DASHBOARD)
# =====================================================================
@app.get("/api/manga/{manga_id}/status", response_model=MangaStatusResponse, tags=["Admin - Monitoring"])
def get_manga_progress(manga_id: int, db: Session = Depends(get_db)):
    """
    Pantau progres unduhan secara realtime
    (jumlah bab selesai, sedang diproses, dan yang gagal).
    """
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


# =====================================================================
# 4. GET CHAPTER PAGES / BY IMAGE (FRONTEND READER)
# =====================================================================
@app.get("/api/chapters/{chapter_id}/pages", response_model=ChapterPagesResponse, tags=["Public - Reader"])
def get_chapter_pages(chapter_id: int, db: Session = Depends(get_db)):
    """
    Endpoint untuk aplikasi web reader.
    Mengembalikan daftar URL potongan gambar terurut (halaman 1, 2, 3...).
    """
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter tidak ditemukan.")

    pages = db.query(ChapterPage).filter(
        ChapterPage.chapter_id == chapter_id
    ).order_by(ChapterPage.page_number.asc()).all()

    return {
        "chapter_id": chapter.id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "status": chapter.status.value,
        "total_pages": len(pages),
        "pages": [
            {
                "page": p.page_number,
                "url": f"http://localhost:8000{p.image_url}"
            }
            for p in pages
        ]
    }