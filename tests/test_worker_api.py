"""Keamanan layanan worker: token, CORS, path traversal, PUBLIC_BASE_URL, startup (NFR-03, BE-10, BE-11, NFR-08)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app

from .conftest import TOKEN, FakeSource, png_bytes, simple_manifest

ROOT = Path(__file__).resolve().parent.parent


async def test_health_needs_no_token(make_worker):
    w = await make_worker()
    r = await w.client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True and "adapters" in r.json()


@pytest.mark.parametrize(
    "method,path",
    [("get", "/worker/v1/status"), ("post", "/worker/v1/scan"), ("post", "/worker/v1/jobs"), ("get", "/worker/v1/jobs/j1/progress"),
     ("get", "/worker/v1/staging/s_0123456789abcdef"), ("delete", "/worker/v1/jobs/j1")],
)
async def test_missing_or_wrong_token_is_401(make_worker, method, path):
    w = await make_worker()
    assert (await getattr(w.client, method)(path)).status_code == 401
    r = await getattr(w.client, method)(path, headers={"X-Worker-Token": "salah"})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"


async def test_no_cors_headers_even_with_origin(make_worker):
    w = await make_worker()
    r = await w.client.get("/health", headers={"Origin": "https://evil.example"})
    assert not any(k.lower().startswith("access-control-") for k in r.headers)
    r = await w.client.options("/worker/v1/scan", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert not any(k.lower().startswith("access-control-") for k in r.headers)


@pytest.mark.parametrize(
    "path",
    [
        "/worker/v1/jobs/..%2f..%2fetc%2fpasswd/progress",
        "/worker/v1/jobs/../../etc/passwd/progress",
        "/worker/v1/jobs/j1/chapters/..%2fx/manifest",
        "/worker/v1/jobs/j1/chapters/../x/pages/1",
        "/worker/v1/jobs/j1/chapters/a.b/manifest",
        "/static/anything",
        "/worker/v1/staging/..%2f..%2fetc%2fpasswd",
    ],
)
async def test_path_traversal_and_bad_ids_rejected(make_worker, path):
    w = await make_worker()
    r = await w.client.get(path, headers=w.h())
    assert r.status_code in {400, 404}, (path, r.status_code)
    assert "root:" not in r.text


async def test_unknown_job_is_404_job_not_found(make_worker):
    w = await make_worker()
    r = await w.client.get("/worker/v1/jobs/nope/progress", headers=w.h())
    assert r.status_code == 404 and r.json()["code"] == "JOB_NOT_FOUND"


async def test_errors_never_leak_stack_traces(make_worker):
    w = await make_worker()
    r = await w.client.post("/worker/v1/scan", json={"bukan": "url"}, headers=w.h())
    assert r.status_code == 400 and r.json()["code"] == "INVALID_REQUEST"
    assert "Traceback" not in r.text


async def test_status_reports_disk_and_limits(make_worker):
    w = await make_worker()
    data = (await w.client.get("/worker/v1/status", headers=w.h())).json()
    assert data["disk_free_bytes"] > 0
    assert data["limits"] == {"max_pages": 300, "max_chapter_bytes": 157286400, "max_image_bytes": 15728640}


async def test_manifest_urls_use_public_base_url_and_are_relative_without_it(make_worker, source):
    source.add_image("/a/1.png", png_bytes(100))
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", ["a/1.png"])]))
    for base, expected_prefix in [("https://worker.internal:9000", "https://worker.internal:9000/worker/v1/jobs/"), ("", "/worker/v1/jobs/")]:
        w = await make_worker(source, public_base_url=base)
        info = (await w.scan(source.url("/s.json"))).json()
        ref = info["chapters"][0]["ref"]
        assert (await w.start_job("jb1", source.url("/s.json"), [{"key": "c1", "ref": ref}])).status_code == 202
        await w.wait("jb1")
        manifest = (await w.client.get("/worker/v1/jobs/jb1/chapters/c1/manifest", headers=w.h())).json()
        assert manifest["pages"][0]["url"].startswith(expected_prefix)
        assert manifest["pages"][0]["url"].endswith("/chapters/c1/pages/1")


def test_no_localhost_8000_anywhere_in_repo():
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", "__pycache__", ".pytest_cache", "staging", ".fixture-data"} for part in path.parts):
            continue
        if path.suffix in {".py", ".md", ".txt", ".example", ".ini"} and path.name != "test_worker_api.py":
            if "localhost:8000" in path.read_text(errors="ignore"):
                offenders.append(str(path))
    assert offenders == []


def test_start_refused_without_token_outside_dev_mode():
    with pytest.raises(RuntimeError, match="WORKER_TOKEN"):
        create_app(Settings(worker_token="", dev_mode=False))
    with pytest.raises(RuntimeError):
        Settings.from_env({"SCAN_ENABLE_FIXTURE": "true"}).validate()
    create_app(Settings(worker_token="x"))  # token ada: ok


def test_settings_from_env_parses_everything():
    s = Settings.from_env({
        "WORKER_TOKEN": "t", "PUBLIC_BASE_URL": "https://w.internal:9000/", "WORKER_BIND": "127.0.0.1",
        "SCAN_ALLOWED_HOSTS": " A.example , b.example:8080 ", "STAGING_DIR": "/tmp/st", "SCAN_MAX_PAGES_PER_CHAPTER": "123",
        "SCAN_HOST_MIN_DELAY_MS": "10", "ENABLE_LEGACY_API": "true",
    })
    assert s.public_base_url == "https://w.internal:9000" and s.allowed_hosts == {"a.example", "b.example:8080"}
    assert s.max_pages_per_chapter == 123 and s.host_min_delay_ms == 10 and s.legacy_enabled is True
    d = Settings.from_env({"WORKER_TOKEN": "t"})
    assert (d.max_pages_per_chapter, d.max_bytes_per_chapter, d.max_bytes_per_image) == (300, 157286400, 15728640)
    assert (d.chapter_concurrency, d.image_concurrency, d.host_max_concurrency, d.host_min_delay_ms, d.max_active_jobs) == (2, 4, 4, 500, 2)
    assert d.worker_bind == "127.0.0.1" and d.legacy_enabled is False and d.allowed_hosts == frozenset()
    assert d.enable_docs is True  # absen = Swagger menyala
    assert Settings.from_env({"WORKER_TOKEN": "t", "SCAN_ENABLE_DOCS": "  "}).enable_docs is True
    for raw in ("false", "0", "no", "off", "FALSE"):
        assert Settings.from_env({"WORKER_TOKEN": "t", "SCAN_ENABLE_DOCS": raw}).enable_docs is False
    for raw in ("true", "1", "yes", "on", "TRUE"):
        assert Settings.from_env({"WORKER_TOKEN": "t", "SCAN_ENABLE_DOCS": raw}).enable_docs is True


def _run(code: str, env_extra: dict, cwd: Path):
    import os

    env = {**os.environ, "PYTHONPATH": str(ROOT), **env_extra}
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def test_legacy_api_is_disabled_by_default_and_enabled_by_flag(tmp_path):
    probe = (
        "from fastapi.testclient import TestClient\n"
        "from app.config import Settings\n"
        "from app.main import create_app\n"
        "import os\n"
        "app = create_app(Settings(worker_token='t', legacy_enabled=os.environ.get('ENABLE_LEGACY_API')=='true'))\n"
        "c = TestClient(app)\n"
        "paths = sorted(p for p in app.openapi()['paths'] if p.startswith('/api/'))\n"
        "r2 = c.get('/api/manga/999/status')\n"
        "r3 = c.get('/static/x')\n"
        "print('RESULT', r2.status_code, r2.json().get('detail') or r2.json().get('code'), r3.status_code, ','.join(paths))\n"
    )
    off = _run(probe, {"DATABASE_URL": f"sqlite:///{tmp_path}/off.db"}, tmp_path)
    line = [l for l in off.stdout.splitlines() if l.startswith("RESULT")][0]
    assert line.split()[1] == "404" and line.split()[-1] != "x" and " 404 " in line, (off.stdout, off.stderr)
    assert "/api/manga" not in line and "/api/chapters" not in line  # POST /api/manga/scan -> 404 (rute tidak ada)
    assert not (tmp_path / "off.db").exists() and not (tmp_path / "storage").exists()  # tidak menyentuh SQLite/storage
    on = _run(probe, {"DATABASE_URL": f"sqlite:///{tmp_path}/on.db", "ENABLE_LEGACY_API": "true"}, tmp_path)
    line_on = [l for l in on.stdout.splitlines() if l.startswith("RESULT")][0]
    for path in ("/api/manga/scan", "/api/manga/import", "/api/chapters/{chapter_id}/pages", "/api/manga/{manga_id}/status"):
        assert path in line_on, (line_on, on.stderr)
    assert line_on.startswith("RESULT 404 Manga tidak ditemukan.")  # kontrak legacy {"detail": ...} tidak berubah
    assert (tmp_path / "on.db").exists()
