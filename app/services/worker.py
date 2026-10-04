import logging
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Chapter, ChapterPage, ChapterStatus, Manga, MangaStatus
from app.services.scraper import ScraperService

logger = logging.getLogger(__name__)


def process_chapter_download(chapter_id: int):
    """Fungsi worker yang dijalankan di background thread untuk mengunduh gambar per bab."""
    db: Session = SessionLocal()
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter or chapter.status == ChapterStatus.COMPLETED:
        db.close()
        return

    chapter.status = ChapterStatus.DOWNLOADING
    db.commit()
    print(f"\n[WORKER] Memulai download Chapter {chapter.chapter_number} (ID: {chapter_id})...")

    try:
        # Download gambar per lembar ke folder lokal
        pages_data = ScraperService.download_chapter_images(
            chapter_url=chapter.source_url,
            manga_id=chapter.manga_id,
            chapter_number=chapter.chapter_number
        )

        # VALIDASI KRUSIAL: Jangan biarkan status COMPLETED jika gambarnya kosong
        if not pages_data:
            raise ValueError(f"Tidak ada panel gambar yang berhasil diambil dari {chapter.source_url}")

        # Hapus data halaman lama jika ini proses retry
        db.query(ChapterPage).filter(ChapterPage.chapter_id == chapter.id).delete()

        # Simpan setiap metadata gambar ke tabel chapter_pages
        for p in pages_data:
            page_record = ChapterPage(
                chapter_id=chapter.id,
                page_number=p["page_number"],
                image_url=p["image_url"]
            )
            db.add(page_record)

        chapter.status = ChapterStatus.COMPLETED
        chapter.error_message = None
        db.commit()
        print(f"[WORKER SUKSES] Chapter {chapter.chapter_number} selesai ({len(pages_data)} halaman tersimpan).")

    except Exception as exc:
        print(f"[WORKER ERROR] Gagal Chapter {chapter.chapter_number}: {exc}")
        chapter.status = ChapterStatus.FAILED
        chapter.error_message = str(exc)
        db.commit()

    finally:
        # Cek apakah semua chapter manga ini sudah selesai
        remaining = db.query(Chapter).filter(
            Chapter.manga_id == chapter.manga_id,
            Chapter.status.in_([ChapterStatus.QUEUED, ChapterStatus.DOWNLOADING])
        ).count()

        if remaining == 0:
            manga = db.query(Manga).filter(Manga.id == chapter.manga_id).first()
            if manga:
                manga.status = MangaStatus.ACTIVE
                db.commit()
                print(f"[WORKER] Semua chapter untuk Manga ID {chapter.manga_id} telah selesai!")

        db.close()