from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

TOKEN = "test-token-123"
PNG_HEAD = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"\x00\x00\x00\rIHDR"


def png_bytes(size: int = 64, fill: int = 0) -> bytes:
    body = PNG_HEAD + bytes([fill]) * max(0, size - len(PNG_HEAD))
    return body[:size] if size >= len(PNG_HEAD) else PNG_HEAD


def jpeg_bytes(size: int = 64) -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * max(0, size - 4)


def webp_bytes(size: int = 64) -> bytes:
    return b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * max(0, size - 16)


class FakeSource:
    """Server HTTP lokal yang dapat diprogram (tanpa internet). Hanya melayani konten buatan tes."""

    def __init__(self, host: str = "127.0.0.1"):
        self.routes: dict[str, object] = {}
        self.hits: dict[str, int] = {}
        self.log: list[tuple[str, dict]] = []
        self.current = 0
        self.peak = 0
        self.delay = 0.0
        self.lock = threading.Lock()
        source = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                with source.lock:
                    source.hits[path] = source.hits.get(path, 0) + 1
                    count = source.hits[path]
                    source.current += 1
                    source.peak = max(source.peak, source.current)
                    source.log.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                try:
                    if source.delay:
                        time.sleep(source.delay)
                    route = source.routes.get(path)
                    if route is None:
                        return self.reply(404, b"", "text/plain")
                    if callable(route):
                        route = route(count, self)
                        if route is None:
                            return
                    status, body, ctype, *extra = route
                    self.reply(status, body, ctype, extra[0] if extra else None)
                finally:
                    with source.lock:
                        source.current -= 1

            def reply(self, status, body, ctype, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # klien menutup lebih awal (mis. batas ukuran tercapai)

        self.server = ThreadingHTTPServer((host, 0), Handler)
        self.server.daemon_threads = True
        self.host = host
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def hostport(self) -> str:
        return f"{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"http://{self.hostport}{path}"

    def add_image(self, path: str, data: bytes, ctype: str = "image/png"):
        self.routes[path] = (200, data, ctype)

    def add_manifest(self, path: str, manifest: dict):
        self.routes[path] = (200, json.dumps(manifest).encode(), "application/json")

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def source():
    s = FakeSource()
    yield s
    s.stop()


def make_settings(tmp_path: Path, source: FakeSource | None = None, **overrides) -> Settings:
    base = dict(
        worker_token=TOKEN,
        dev_mode=True,
        staging_dir=tmp_path / "staging",
        fixture_dir=tmp_path / "fixtures",
        allowed_hosts=frozenset({source.hostport}) if source else frozenset(),
        host_min_delay_ms=0,
        retry_backoff_base_s=0.01,
        min_free_disk_bytes=0,
        image_timeout_s=10.0,
        html_timeout_s=10.0,
        scan_timeout_s=20.0,
    )
    base.update(overrides)
    return Settings(**base)


class Worker:
    """Aplikasi + klien ASGI + pembantu polling."""

    def __init__(self, app, client, settings):
        self.app = app
        self.client = client
        self.settings = settings

    @property
    def state(self):
        return self.app.state.scan

    def h(self, token: str | None = TOKEN):
        return {"X-Worker-Token": token} if token else {}

    async def scan(self, url: str):
        return await self.client.post("/worker/v1/scan", json={"url": url}, headers=self.h())

    async def start_job(self, job_id: str, source_url: str, chapters: list[dict], adapter: str = "manifest", limits=None):
        body = {"job_id": job_id, "adapter": adapter, "source_url": source_url, "chapters": chapters}
        if limits:
            body["limits"] = limits
        return await self.client.post("/worker/v1/jobs", json=body, headers=self.h())

    async def progress(self, job_id: str):
        r = await self.client.get(f"/worker/v1/jobs/{job_id}/progress", headers=self.h())
        return r.json()

    async def wait(self, job_id: str, timeout: float = 30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = await self.progress(job_id)
            if data["state"] in {"COMPLETED", "CANCELLED"}:
                return data
            await asyncio.sleep(0.02)
        raise AssertionError(f"job {job_id} tidak selesai dalam {timeout}s: {await self.progress(job_id)}")


@pytest_asyncio.fixture
async def make_worker(tmp_path):
    created = []

    async def factory(source: FakeSource | None = None, **overrides) -> Worker:
        settings = make_settings(tmp_path, source, **overrides)
        app = create_app(settings)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker")
        created.append((ctx, client))
        return Worker(app, client, settings)

    yield factory
    for ctx, client in created:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


def simple_manifest(source: FakeSource, chapters: list[tuple[str, list[str]]], **extra) -> dict:
    manifest = {"title": "Seri Uji", "type": "Manhwa", "status": "ongoing", "chapters": [{"chapter": label, "title": "", "pages": pages} for label, pages in chapters]}
    manifest.update(extra)
    return manifest
