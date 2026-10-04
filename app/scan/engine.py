"""Engine job download per chapter (BE-04, BE-06, BE-07, BE-08, BE-09).

Aturan keras: chapter COMPLETED hanya jika SEMUA halaman dari `list_pages` berhasil
diunduh dan lolos validasi. Satu kegagalan (setelah retry) -> chapter FAILED tanpa
halaman parsial. State job hanya in-memory (stateless; NFR-09).
"""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..config import Settings
from .adapters import AdapterContext, AdapterRegistry, SourceAdapter
from .errors import ID_PATTERN, ScanError, redact_url
from .http import Breaker, ByteBudget, GuardedHttp
from .staging import Staging

log = logging.getLogger("scan.engine")

FINAL_CHAPTER = {"COMPLETED", "FAILED", "CANCELLED"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@dataclass
class PageFile:
    number: int
    filename: str
    content_type: str
    bytes: int
    sha256: str


@dataclass
class ChapterState:
    key: str
    ref: str
    status: str = "QUEUED"
    pages_total: int | None = None
    pages_done: int = 0
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    files: list[PageFile] = field(default_factory=list)
    released: bool = False  # staging sudah dihapus (di-ingest FE / gagal / dibatalkan)


@dataclass
class Limits:
    max_pages: int
    max_chapter_bytes: int
    max_image_bytes: int


@dataclass
class Job:
    id: str
    adapter: str
    source_url: str
    chapters: dict[str, ChapterState]
    limits: Limits
    state: str = "QUEUED"  # QUEUED|RUNNING|COMPLETED|CANCELLED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    breaker: Breaker = field(default_factory=Breaker)
    cache: dict = field(default_factory=dict)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    def touch(self) -> None:
        self.updated_at = time.time()


class PageFailure(ScanError):
    def __init__(self, page: int, source: ScanError):
        super().__init__(
            source.code,
            f"Halaman {page} gagal: {source.message}",
            status=source.status,
            retryable=False,
            retry_after=source.retry_after,
            extra=source.extra,
        )
        self.page = page


class JobManager:
    def __init__(self, settings: Settings, registry: AdapterRegistry, http: GuardedHttp, staging: Staging):
        self.settings = settings
        self.registry = registry
        self.http = http
        self.staging = staging
        self.jobs: dict[str, Job] = {}
        self.chapter_sem = asyncio.Semaphore(settings.chapter_concurrency)
        self.job_sem = asyncio.Semaphore(settings.max_active_jobs)
        self.active_chapters = 0
        self.peak_active_chapters = 0  # untuk verifikasi batas konkurensi di tes

    # ----------------------------------------------------------- API publik
    def free_disk_bytes(self) -> int:
        return shutil.disk_usage(self.staging.root).free

    async def start(self, job_id: str, adapter_name: str, source_url: str, chapters: list[tuple[str, str]], limits: dict | None) -> Job:
        if not ID_PATTERN.match(job_id or ""):
            raise ScanError("INVALID_REQUEST", "job_id tidak valid.")
        if job_id in self.jobs:
            raise ScanError("JOB_EXISTS", "Job dengan ID ini sudah ada.", status=409)
        if not chapters:
            raise ScanError("INVALID_REQUEST", "Daftar chapter kosong.")
        seen: set[str] = set()
        for key, ref in chapters:
            if not ID_PATTERN.match(key or "") or key in seen:
                raise ScanError("INVALID_REQUEST", "Key chapter tidak valid atau ganda.")
            if not isinstance(ref, str) or not ref or len(ref) > 512:
                raise ScanError("INVALID_REQUEST", "Ref chapter tidak valid.")
            seen.add(key)
        adapter = self.registry.resolve(source_url)
        if adapter.name != adapter_name:
            raise ScanError("SOURCE_NOT_SUPPORTED", "Adapter tidak sesuai dengan URL sumber.", status=422)
        if self.free_disk_bytes() < self.settings.min_free_disk_bytes:
            raise ScanError("INSUFFICIENT_DISK", "Ruang disk worker tidak mencukupi untuk memulai unduhan.", status=507)

        requested = limits or {}
        s = self.settings
        job_limits = Limits(
            max_pages=min(int(requested.get("max_pages") or s.max_pages_per_chapter), s.max_pages_per_chapter),
            max_chapter_bytes=min(int(requested.get("max_chapter_bytes") or s.max_bytes_per_chapter), s.max_bytes_per_chapter),
            max_image_bytes=min(int(requested.get("max_image_bytes") or s.max_bytes_per_image), s.max_bytes_per_image),
        )
        job = Job(
            id=job_id,
            adapter=adapter.name,
            source_url=source_url,
            chapters={key: ChapterState(key, ref) for key, ref in chapters},
            limits=job_limits,
        )
        self.jobs[job_id] = job
        job.task = asyncio.create_task(self._run_job(job, adapter), name=f"scan-job-{job_id}")
        log.info("job_start job=%s adapter=%s chapters=%d source=%s", job_id, adapter.name, len(chapters), redact_url(source_url))
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise ScanError("JOB_NOT_FOUND", "Job tidak ditemukan (worker mungkin dimulai ulang).", status=404)
        return job

    def progress(self, job_id: str) -> dict:
        job = self.get(job_id)
        return {
            "job_id": job.id,
            "state": job.state,
            "updated_at": _iso(job.updated_at),
            "chapters": [
                {
                    "key": c.key,
                    "status": c.status,
                    "pages_total": c.pages_total,
                    "pages_done": c.pages_done,
                    "attempts": c.attempts,
                    "error_code": c.error_code,
                    "error_message": c.error_message,
                }
                for c in job.chapters.values()
            ],
        }

    async def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job.task and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        return self.progress(job_id)

    def chapter_manifest(self, job_id: str, key: str) -> dict:
        chapter = self._chapter(job_id, key)
        if chapter.status != "COMPLETED" or chapter.released:
            raise ScanError("CHAPTER_NOT_READY", "Chapter belum selesai atau staging sudah dibersihkan.", status=409)
        base = self.settings.public_base_url
        return {
            "key": key,
            "status": chapter.status,
            "pages": [
                {
                    "page_number": f.number,
                    "content_type": f.content_type,
                    "bytes": f.bytes,
                    "sha256": f.sha256,
                    "url": f"{base}/worker/v1/jobs/{job_id}/chapters/{key}/pages/{f.number}",
                }
                for f in chapter.files
            ],
        }

    def page_path(self, job_id: str, key: str, number: int):
        chapter = self._chapter(job_id, key)
        if chapter.status != "COMPLETED" or chapter.released:
            raise ScanError("CHAPTER_NOT_READY", "Chapter belum selesai atau staging sudah dibersihkan.", status=409)
        for f in chapter.files:
            if f.number == number:
                return self.staging.chapter_dir(job_id, key) / f.filename, f.content_type
        raise ScanError("PAGE_NOT_FOUND", "Halaman tidak ditemukan.", status=404)

    def delete_chapter(self, job_id: str, key: str) -> None:
        chapter = self._chapter(job_id, key)
        if chapter.status in {"QUEUED", "DOWNLOADING"}:
            raise ScanError("CHAPTER_BUSY", "Chapter masih diproses.", status=409)
        self.staging.remove_dir(self.staging.chapter_dir(job_id, key))
        chapter.released = True

    async def delete_job(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.task and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        self.staging.remove_dir(self.staging.job_dir(job_id))
        self.jobs.pop(job_id, None)

    def sweep(self, now: float | None = None) -> int:
        """Bersihkan job selesai/dibatalkan yang kedaluwarsa + berkas yatim di disk."""
        now = time.time() if now is None else now
        ttl = self.settings.staging_ttl_hours * 3600
        removed = 0
        for job in list(self.jobs.values()):
            if job.finished_at is None:
                continue
            age = now - job.finished_at
            limit = self.settings.cancelled_cleanup_s if job.state == "CANCELLED" else ttl
            if age >= limit:
                self.staging.remove_dir(self.staging.job_dir(job.id))
                self.jobs.pop(job.id, None)
                removed += 1
        removed += self.staging.sweep(ttl, keep_jobs=set(self.jobs))
        return removed

    async def shutdown(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------- internal
    def _chapter(self, job_id: str, key: str) -> ChapterState:
        job = self.get(job_id)
        chapter = job.chapters.get(key)
        if not chapter:
            raise ScanError("CHAPTER_NOT_FOUND", "Chapter tidak ada pada job ini.", status=404)
        return chapter

    async def _run_job(self, job: Job, adapter: SourceAdapter) -> None:
        ctx = AdapterContext(http=self.http, settings=self.settings, logger=log, source_url=job.source_url, cache=job.cache)
        children = [asyncio.create_task(self._run_chapter(job, ch, adapter, ctx)) for ch in job.chapters.values()]
        try:
            async with self.job_sem:
                job.state = "RUNNING"
                job.started.set()
                job.touch()
                await asyncio.gather(*children)
            job.state = "COMPLETED"
        except asyncio.CancelledError:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            for ch in job.chapters.values():
                if ch.status not in FINAL_CHAPTER:
                    ch.status = "CANCELLED"
            job.state = "CANCELLED"
        finally:
            job.finished_at = time.time()
            job.touch()
            log.info("job_end job=%s state=%s", job.id, job.state)

    def _fail(self, job: Job, ch: ChapterState, exc: ScanError) -> None:
        self.staging.remove_dir(self.staging.chapter_dir(job.id, ch.key))
        ch.files = []
        ch.released = True
        ch.status = "FAILED"
        ch.error_code = exc.code
        ch.error_message = exc.message
        job.touch()
        log.warning("chapter_failed job=%s chapter=%s code=%s attempts=%d", job.id, ch.key, exc.code, ch.attempts)

    async def _run_chapter(self, job: Job, ch: ChapterState, adapter: SourceAdapter, ctx: AdapterContext) -> None:
        directory = self.staging.chapter_dir(job.id, ch.key)
        counted = False
        try:
            await job.started.wait()
            async with self.chapter_sem:
                self.active_chapters += 1
                counted = True
                self.peak_active_chapters = max(self.peak_active_chapters, self.active_chapters)
                ch.status = "DOWNLOADING"
                job.touch()
                self.staging.remove_dir(directory)
                directory.mkdir(parents=True, exist_ok=True)
                try:
                    pages = await adapter.list_pages(ch.ref, ctx)
                    if not pages:
                        raise ScanError("NO_PAGES", "Chapter tidak memiliki halaman.")
                    if len(pages) > job.limits.max_pages:
                        raise ScanError("LIMIT_PAGES", f"Chapter memiliki {len(pages)} halaman (maksimal {job.limits.max_pages}).")
                    ch.pages_total = len(pages)
                    job.touch()
                    files = await self._download_pages(job, ch, adapter, ctx, pages, directory)
                    ch.files = files
                    ch.status = "COMPLETED"
                    job.touch()
                    log.info("chapter_ok job=%s chapter=%s pages=%d attempts=%d", job.id, ch.key, len(files), ch.attempts)
                except ScanError as exc:
                    self._fail(job, ch, exc)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - jangan bocorkan detail internal ke klien
                    log.exception("chapter_error job=%s chapter=%s", job.id, ch.key)
                    self._fail(job, ch, ScanError("INTERNAL_ERROR", "Terjadi kesalahan internal saat mengunduh chapter.", status=500))
        except asyncio.CancelledError:
            self.staging.remove_dir(directory)
            ch.files = []
            ch.released = True
            if ch.status not in FINAL_CHAPTER:
                ch.status = "CANCELLED"
            job.touch()
            raise
        finally:
            if counted:
                self.active_chapters -= 1

    async def _download_pages(self, job, ch, adapter, ctx, pages, directory) -> list[PageFile]:
        budget = ByteBudget(job.limits.max_chapter_bytes)
        image_sem = asyncio.Semaphore(self.settings.image_concurrency)
        headers = dict(adapter.fetch_headers(ctx))
        results: dict[int, PageFile] = {}

        async def one(number: int, url: str) -> None:
            retries = self.settings.image_retries
            for attempt in range(1, retries + 2):
                ch.attempts = max(ch.attempts, attempt)
                try:
                    async with image_sem:
                        image = await self.http.download_image(
                            url, directory, f"{number:03d}",
                            max_bytes=job.limits.max_image_bytes, budget=budget, headers=headers, breaker=job.breaker,
                        )
                except ScanError as exc:
                    if exc.retryable and attempt <= retries:
                        backoff = self.settings.retry_backoff_base_s * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
                        delay = min(self.settings.retry_after_cap_s, max(exc.retry_after or 0.0, backoff))
                        log.info("retry job=%s chapter=%s page=%d attempt=%d code=%s delay=%.2f", job.id, ch.key, number, attempt, exc.code, delay)
                        await asyncio.sleep(delay)
                        continue
                    if exc.code == "RATE_LIMITED":  # retry habis: sumber menolak (BE-14), hentikan host ini
                        host = (urlsplit(url).hostname or "").lower()
                        message = "Sumber terus membatasi laju permintaan (HTTP 429)."
                        if exc.retry_after is not None:
                            message += f" Retry-After: {int(exc.retry_after)} detik."
                        job.breaker.trip(host, message)
                        exc = ScanError("SOURCE_REFUSED", message, status=403, retry_after=exc.retry_after)
                    raise PageFailure(number, exc) from exc
                results[number] = PageFile(number, image.path.name, image.fmt.content_type, image.size, image.sha256)
                ch.pages_done += 1
                job.touch()
                return

        tasks = [asyncio.create_task(one(i, p.url)) for i, p in enumerate(pages, start=1)]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        errors = [t.exception() for t in done if not t.cancelled() and t.exception() is not None]
        if errors:
            first = min(errors, key=lambda e: getattr(e, "page", 10**9))
            raise first
        files = [results[i] for i in range(1, len(pages) + 1)]
        # Keamanan tambahan: nomor halaman kontigu 1..N
        if [f.number for f in files] != list(range(1, len(pages) + 1)):
            raise ScanError("INTERNAL_ERROR", "Urutan halaman tidak kontigu.", status=500)
        return files
