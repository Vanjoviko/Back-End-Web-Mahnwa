"""Endpoint worker `/worker/v1/*` (spec §4.3). Semua butuh X-Worker-Token kecuali GET /health."""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Security
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from . import service
from .errors import ScanError

VERSION = "3.0.0"
ID = r"^[A-Za-z0-9_-]{1,64}$"
STAGED = r"^s_[0-9a-f]{16}$"


class ScanBody(BaseModel):
    url: str = Field(max_length=4096)


class JobChapter(BaseModel):
    key: str = Field(pattern=ID)
    ref: str = Field(min_length=1, max_length=512)


class JobLimits(BaseModel):
    max_pages: Optional[int] = Field(default=None, ge=1)
    max_chapter_bytes: Optional[int] = Field(default=None, ge=1)
    max_image_bytes: Optional[int] = Field(default=None, ge=1)


class JobBody(BaseModel):
    job_id: str = Field(pattern=ID)
    adapter: str = Field(min_length=1, max_length=64)
    source_url: str = Field(max_length=4096)
    chapters: list[JobChapter]
    limits: Optional[JobLimits] = None


def state(request: Request):
    return request.app.state.scan


# Skema OpenAPI agar Swagger menampilkan Authorize untuk header yang sama dengan FE.
# auto_error=False: header kosong tetap jatuh ke pemeriksaan di bawah (401), bukan 403 bawaan FastAPI.
worker_token_header = APIKeyHeader(
    name="X-Worker-Token",
    scheme_name="X-Worker-Token",
    auto_error=False,
    description="Sama dengan WORKER_TOKEN. Wajib untuk /worker/v1/*; tidak dipakai oleh GET /health.",
)


def require_token(request: Request, x_worker_token: Optional[str] = Security(worker_token_header)) -> None:
    expected = state(request).settings.worker_token
    if not expected:  # hanya mungkin di mode dev (divalidasi saat start)
        return
    given = (x_worker_token or "").encode()
    if not hmac.compare_digest(given, expected.encode()):
        raise ScanError("UNAUTHORIZED", "Token worker tidak valid.", status=401)


health_router = APIRouter()
router = APIRouter(prefix="/worker/v1", dependencies=[Depends(require_token)])


@health_router.get("/health")
def health(request: Request):
    s = state(request)
    return {"ok": True, "version": VERSION, "adapters": s.registry.active_names()}


@router.get("/status")
def status(request: Request):
    s = state(request)
    return {
        "disk_free_bytes": s.manager.free_disk_bytes(),
        "min_free_disk_bytes": s.settings.min_free_disk_bytes,
        "active_jobs": sum(1 for j in s.manager.jobs.values() if j.state in {"QUEUED", "RUNNING"}),
        "limits": {
            "max_pages": s.settings.max_pages_per_chapter,
            "max_chapter_bytes": s.settings.max_bytes_per_chapter,
            "max_image_bytes": s.settings.max_bytes_per_image,
        },
    }


@router.post("/scan")
async def scan(body: ScanBody, request: Request):
    s = state(request)
    return await service.run_scan(body.url, settings=s.settings, registry=s.registry, http=s.http, staging=s.staging)


@router.post("/jobs", status_code=202)
async def start_job(body: JobBody, request: Request):
    s = state(request)
    limits = body.limits.model_dump(exclude_none=True) if body.limits else None
    job = await s.manager.start(body.job_id, body.adapter, body.source_url, [(c.key, c.ref) for c in body.chapters], limits)
    return {"job_id": job.id, "state": job.state, "chapters": len(job.chapters)}


@router.get("/jobs/{job_id}/progress")
def progress(request: Request, job_id: str = Path(pattern=ID)):
    return state(request).manager.progress(job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel(request: Request, job_id: str = Path(pattern=ID)):
    return await state(request).manager.cancel(job_id)


@router.get("/jobs/{job_id}/chapters/{key}/manifest")
def chapter_manifest(request: Request, job_id: str = Path(pattern=ID), key: str = Path(pattern=ID)):
    return state(request).manager.chapter_manifest(job_id, key)


@router.get("/jobs/{job_id}/chapters/{key}/pages/{n}")
def chapter_page(request: Request, job_id: str = Path(pattern=ID), key: str = Path(pattern=ID), n: int = Path(ge=1, le=100000)):
    path, content_type = state(request).manager.page_path(job_id, key, n)
    if not path.is_file():
        raise ScanError("PAGE_NOT_FOUND", "Halaman tidak ditemukan.", status=404)
    return FileResponse(path, media_type=content_type, headers={"cache-control": "no-store", "x-content-type-options": "nosniff"})


@router.get("/staging/{staged_id}")
def staged_cover(request: Request, staged_id: str = Path(pattern=STAGED)):
    import mimetypes

    path = state(request).staging.cover_path(staged_id)
    media = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    return FileResponse(path, media_type=media, headers={"cache-control": "no-store", "x-content-type-options": "nosniff"})


@router.delete("/staging/{staged_id}")
def delete_staged(request: Request, staged_id: str = Path(pattern=STAGED)):
    state(request).staging.delete_cover(staged_id)
    return {"ok": True}


@router.delete("/jobs/{job_id}/chapters/{key}")
def delete_chapter(request: Request, job_id: str = Path(pattern=ID), key: str = Path(pattern=ID)):
    state(request).manager.delete_chapter(job_id, key)
    return {"ok": True}


@router.delete("/jobs/{job_id}")
async def delete_job(request: Request, job_id: str = Path(pattern=ID)):
    await state(request).manager.delete_job(job_id)
    return {"ok": True}


def _is_worker_path(request: Request) -> bool:
    return not request.url.path.startswith("/api/")


def install_error_handlers(app: FastAPI) -> None:
    import logging

    from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    log = logging.getLogger("scan.api")

    @app.exception_handler(ScanError)
    async def scan_error(_: Request, exc: ScanError):
        headers = {"retry-after": str(int(exc.retry_after))} if exc.retry_after is not None and exc.status in {429, 503} else None
        return JSONResponse(exc.to_dict(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if not _is_worker_path(request):
            return await request_validation_exception_handler(request, exc)  # kontrak legacy tidak berubah
        return JSONResponse({"error": "Permintaan tidak valid.", "code": "INVALID_REQUEST"}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return await http_exception_handler(request, exc)  # kontrak legacy: {"detail": ...}
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse({"error": "Endpoint tidak ditemukan." if exc.status_code == 404 else "Permintaan ditolak.", "code": code}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("unhandled_error")
        return JSONResponse({"error": "Terjadi kesalahan internal pada worker.", "code": "INTERNAL_ERROR"}, status_code=500)
