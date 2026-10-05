import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings

log = logging.getLogger("scan.main")


class ScanState:
    """Komponen worker yang dibuat saat startup (lifespan)."""

    def __init__(self, settings: Settings):
        from app.scan.adapters import build_registry
        from app.scan.engine import JobManager
        from app.scan.http import GuardedHttp
        from app.scan.service import make_policy
        from app.scan.staging import Staging

        self.settings = settings
        self.staging = Staging(settings.staging_dir)
        self.registry = build_registry(settings)
        self.http = GuardedHttp(settings, make_policy(settings))
        self.manager = JobManager(settings, self.registry, self.http, self.staging)


def create_app(settings: Settings | None = None) -> FastAPI:
    import asyncio

    from app.scan import api as scan_api

    settings = settings or Settings.from_env()
    settings.validate()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # NFR-11 / QA D-05: httpx mencetak URL lengkap (beserta query/token) pada INFO -> turunkan + redaksi global.
    from app.scan.logsafe import install_log_redaction, quiet_http_loggers

    quiet_http_loggers()
    install_log_redaction()
    if not settings.public_base_url:
        log.warning("PUBLIC_BASE_URL tidak diset: URL pada manifest chapter akan berupa path relatif.")
    if settings.dev_mode:
        log.warning("SCAN_DEV_MODE aktif: http:// dan host loopback yang ada di allowlist diizinkan. JANGAN dipakai di produksi.")
    if not settings.allowed_hosts and not settings.enable_fixture:
        log.warning("SCAN_ALLOWED_HOSTS kosong: tidak ada adapter sumber yang aktif (fitur scan-import nonaktif).")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = ScanState(settings)
        app.state.scan = state
        # Job in-memory hilang saat restart; bersihkan staging yatim sisa proses sebelumnya.
        state.staging.sweep(0, keep_jobs=set())

        async def sweeper():
            while True:
                await asyncio.sleep(settings.sweep_interval_s)
                try:
                    state.manager.sweep()
                except Exception:  # noqa: BLE001
                    log.exception("sweeper_gagal")

        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            await state.manager.shutdown()
            await state.http.aclose()

    # Dokumentasi interaktif menyala secara default agar API bisa dicoba dari Swagger.
    # SCAN_ENABLE_DOCS=false menutup /docs, /redoc, dan /openapi.json (pengerasan produksi).
    # Rute /worker/v1/* tetap memakai X-Worker-Token; GET /health tetap tanpa token.
    docs_on = settings.enable_docs
    if docs_on:
        log.info("Dokumentasi interaktif aktif di /docs, /redoc, dan /openapi.json (SCAN_ENABLE_DOCS=false untuk menutup).")
    else:
        log.info("Dokumentasi interaktif nonaktif (SCAN_ENABLE_DOCS=false).")
    app = FastAPI(
        title="Scan-Import Worker",
        version=scan_api.VERSION,
        description=(
            "Worker stateless untuk scan metadata komik dan unduh gambar chapter terurut. "
            "Rute /worker/v1/* membutuhkan header X-Worker-Token (tombol Authorize di Swagger). "
            "GET /health tidak membutuhkan token."
        ),
        lifespan=lifespan,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    scan_api.install_error_handlers(app)
    app.include_router(scan_api.health_router)
    app.include_router(scan_api.router)

    if settings.legacy_enabled:
        # Kode legacy (SQLite + ScraperService lama) dipertahankan di balik flag, bukan dihapus.
        from fastapi.staticfiles import StaticFiles

        from app import legacy_api
        from app.database import Base, engine

        log.warning("ENABLE_LEGACY_API aktif: endpoint /api/manga/*, /api/chapters/* dan /static tanpa autentikasi.")
        Base.metadata.create_all(bind=engine)
        os.makedirs("storage/manga", exist_ok=True)
        app.mount("/static", StaticFiles(directory="storage"), name="static")
        app.include_router(legacy_api.build_router(settings))

    return app


def __getattr__(name: str):
    # Kompatibel dengan `uvicorn app.main:app` tanpa membuat aplikasi saat modul di-import.
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(name)
