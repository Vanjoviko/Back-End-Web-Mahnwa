"""BE-04..BE-09, NFR-01, NFR-02, NFR-04: engine unduhan terhadap server HTTP palsu lokal (tanpa internet)."""
import asyncio
import hashlib
import time

import pytest

from .conftest import FakeSource, jpeg_bytes, png_bytes, simple_manifest, webp_bytes


async def run_chapter(w, source, pages, *, label="Chapter 1", job_id="j1", limits=None, extra_manifest=None):
    """Daftarkan manifest 1 chapter, scan, jalankan job, kembalikan (progress, info)."""
    manifest = simple_manifest(source, [(label, pages)])
    if extra_manifest:
        manifest.update(extra_manifest)
    source.add_manifest("/s.json", manifest)
    url = source.url("/s.json")
    info = (await w.scan(url)).json()
    ref = info["chapters"][0]["ref"]
    r = await w.start_job(job_id, url, [{"key": "c1", "ref": ref}], limits=limits)
    assert r.status_code == 202, r.text
    final = await w.wait(job_id)
    return final, final["chapters"][0]


def make_pages(source, n, prefix="/p", data=None, ctype="image/png"):
    paths = []
    for i in range(1, n + 1):
        path = f"{prefix}/{i:03d}.png"
        source.add_image(path, data or png_bytes(200 + i), ctype)
        paths.append(path.lstrip("/"))
    return paths


async def test_ten_pages_ok_completed_with_ordered_files(make_worker, source):
    w = await make_worker(source)
    pages = make_pages(source, 10)
    final, ch = await run_chapter(w, source, pages)
    assert ch["status"] == "COMPLETED" and ch["pages_total"] == 10 and ch["pages_done"] == 10 and ch["attempts"] == 1
    manifest = (await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())).json()
    assert [p["page_number"] for p in manifest["pages"]] == list(range(1, 11))
    files = sorted(p.name for p in w.state.staging.chapter_dir("j1", "c1").iterdir())
    assert files == [f"{i:03d}.png" for i in range(1, 11)]
    assert not list(w.state.staging.chapter_dir("j1", "c1").glob("*.pdf"))
    first = manifest["pages"][0]
    body = (await w.client.get(f"/worker/v1/jobs/j1/chapters/c1/pages/1", headers=w.h())).content
    assert hashlib.sha256(body).hexdigest() == first["sha256"] and len(body) == first["bytes"]
    assert first["content_type"] == "image/png"


async def test_page_order_follows_source_not_completion_order(make_worker, source):
    w = await make_worker(source)
    pages = make_pages(source, 6)
    # halaman awal dibuat lambat agar selesai paling akhir
    def slow(count, handler):
        time.sleep(0.4)
        return (200, png_bytes(111), "image/png")
    source.routes["/p/001.png"] = slow
    final, ch = await run_chapter(w, source, pages)
    manifest = (await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())).json()
    assert [p["bytes"] for p in manifest["pages"]] == [111, 202, 203, 204, 205, 206]


async def test_page_7_always_500_fails_whole_chapter_and_cleans_staging(make_worker, source):
    w = await make_worker(source)
    pages = make_pages(source, 10)
    source.routes["/p/007.png"] = (500, b"", "text/plain")
    final, ch = await run_chapter(w, source, pages)
    assert ch["status"] == "FAILED" and ch["attempts"] == 3
    assert "Halaman 7" in ch["error_message"]
    assert source.hits["/p/007.png"] == 3
    assert not w.state.staging.chapter_dir("j1", "c1").exists()
    r = await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())
    assert r.status_code == 409 and r.json()["code"] == "CHAPTER_NOT_READY"


async def test_404_page_fails_chapter_with_single_attempt(make_worker, source):
    w = await make_worker(source)
    pages = make_pages(source, 3)
    source.routes["/p/002.png"] = (404, b"", "text/plain")
    final, ch = await run_chapter(w, source, pages)
    assert ch["status"] == "FAILED" and source.hits["/p/002.png"] == 1 and "Halaman 2" in ch["error_message"]
    assert ch["error_code"] == "HTTP_ERROR"


async def test_retry_500_500_200_succeeds_on_third_attempt(make_worker, source):
    w = await make_worker(source)
    pages = make_pages(source, 3)
    source.routes["/p/002.png"] = lambda count, h: (200, png_bytes(300), "image/png") if count >= 3 else (500, b"", "text/plain")
    final, ch = await run_chapter(w, source, pages)
    assert ch["status"] == "COMPLETED" and ch["attempts"] == 3 and source.hits["/p/002.png"] == 3


async def test_retry_429_honors_retry_after(make_worker, source):
    w = await make_worker(source)
    pages = make_pages(source, 1)
    source.routes["/p/001.png"] = lambda count, h: (200, png_bytes(300), "image/png") if count >= 2 else (429, b"", "text/plain", {"Retry-After": "2"})
    started = time.monotonic()
    final, ch = await run_chapter(w, source, pages)
    assert ch["status"] == "COMPLETED" and ch["attempts"] == 2
    assert time.monotonic() - started >= 2.0


async def test_429_forever_ends_as_source_refused_and_stops_host(make_worker, source):
    w = await make_worker(source, retry_backoff_base_s=0.01)
    pages = make_pages(source, 2)
    source.routes["/p/001.png"] = (429, b"", "text/plain", {"Retry-After": "0"})
    final, ch = await run_chapter(w, source, pages)
    assert ch["status"] == "FAILED" and ch["error_code"] == "SOURCE_REFUSED"
    assert "Retry-After" in ch["error_message"]
    assert source.hits["/p/001.png"] == 3


async def test_403_is_source_refused_one_attempt_and_circuit_breaker_blocks_other_chapters(make_worker, source):
    w = await make_worker(source, chapter_concurrency=1)
    source.add_image("/a.png", png_bytes(100))
    source.routes["/b.png"] = (403, b"denied", "text/html")
    source.add_image("/c.png", png_bytes(100))
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", ["a.png"]), ("Chapter 2", ["b.png"]), ("Chapter 3", ["c.png"])]))
    url = source.url("/s.json")
    info = (await w.scan(url)).json()
    by_label = {c["number"]: c["ref"] for c in info["chapters"]}
    await w.start_job("j1", url, [{"key": f"c{n}", "ref": by_label[n]} for n in ("1", "2", "3")])
    final = await w.wait("j1")
    status = {c["key"]: c for c in final["chapters"]}
    assert status["c1"]["status"] == "COMPLETED"
    assert status["c2"]["status"] == "FAILED" and status["c2"]["error_code"] == "SOURCE_REFUSED" and status["c2"]["attempts"] == 1
    assert status["c3"]["status"] == "FAILED" and status["c3"]["error_code"] == "SOURCE_REFUSED"
    assert source.hits.get("/c.png", 0) == 0  # tidak ada percobaan alternatif / tidak ada request lagi
    assert source.hits["/b.png"] == 1


async def test_captcha_page_instead_of_image_is_source_refused(make_worker, source):
    w = await make_worker(source)
    pages = make_pages(source, 1)
    source.routes["/p/001.png"] = (200, b"<html>Please verify you are human - captcha</html>", "text/html")
    final, ch = await run_chapter(w, source, pages)
    assert ch["error_code"] == "SOURCE_REFUSED" and source.hits["/p/001.png"] == 1


@pytest.mark.parametrize(
    "path,data,ctype,expected_ext",
    [
        ("/x/1.png", png_bytes(500), "image/png", ".png"),
        ("/x/2.jpg", png_bytes(500), "image/png", ".png"),  # URL .jpg tetapi isi PNG
        ("/x/3.jpg", jpeg_bytes(500), "image/jpeg", ".jpg"),
        ("/x/4.jpg?x=.webp", jpeg_bytes(500), "image/jpeg", ".jpg"),  # query .webp tidak menipu
        ("/x/5.bin", webp_bytes(500), "application/octet-stream", ".webp"),
    ],
)
async def test_extension_comes_from_magic_bytes(make_worker, source, path, data, ctype, expected_ext):
    w = await make_worker(source)
    source.add_image(path.split("?")[0], data, ctype)
    final, ch = await run_chapter(w, source, [path.lstrip("/")])
    assert ch["status"] == "COMPLETED"
    assert [p.suffix for p in w.state.staging.chapter_dir("j1", "c1").iterdir()] == [expected_ext]


@pytest.mark.parametrize(
    "data,ctype",
    [
        (b"<html><body>bukan gambar</body></html>", "image/jpeg"),  # HTML dengan Content-Type image/jpeg
        (b"GIF89a" + b"\x00" * 100, "image/gif"),
        (b"\x00\x00\x00\x1cftypavif" + b"\x00" * 100, "image/avif"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"),
        (png_bytes(300), "text/html"),  # Content-Type bertentangan dengan magic bytes
        (png_bytes(300), "image/jpeg"),
        (b"", "image/png"),
    ],
)
async def test_unsupported_or_contradictory_images_fail_chapter(make_worker, source, data, ctype):
    w = await make_worker(source)
    source.add_image("/x/1.png", data, ctype)
    final, ch = await run_chapter(w, source, ["x/1.png"])
    assert ch["status"] == "FAILED" and ch["error_code"] == "UNSUPPORTED_IMAGE_FORMAT"
    assert source.hits["/x/1.png"] == 1  # tidak di-retry


async def test_duplicate_consecutive_urls_are_kept_in_source_order(make_worker, source):
    w = await make_worker(source)
    source.add_image("/d/1.png", png_bytes(150))
    source.add_image("/d/2.png", png_bytes(250))
    final, ch = await run_chapter(w, source, ["d/1.png", "d/1.png", "d/2.png"])
    manifest = (await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())).json()
    assert [p["bytes"] for p in manifest["pages"]] == [150, 150, 250] and ch["pages_total"] == 3


async def test_no_pages_fails_with_no_pages(make_worker, source):
    w = await make_worker(source)
    final, ch = await run_chapter(w, source, [])
    assert ch["status"] == "FAILED" and ch["error_code"] == "NO_PAGES"


# ----------------------------------------------------------------- limit
async def test_page_301_fails_with_limit_pages_and_nothing_downloaded(make_worker, source):
    w = await make_worker(source)
    source.add_image("/t.png", png_bytes(100))
    final, ch = await run_chapter(w, source, ["t.png"] * 301)
    assert ch["status"] == "FAILED" and ch["error_code"] == "LIMIT_PAGES"
    assert source.hits.get("/t.png", 0) == 0
    assert not w.state.staging.chapter_dir("j1", "c1").exists()


async def test_exactly_300_pages_is_accepted(make_worker, source):
    w = await make_worker(source)
    source.add_image("/t.png", png_bytes(100))
    final, ch = await run_chapter(w, source, ["t.png"] * 300)
    assert ch["status"] == "COMPLETED" and ch["pages_done"] == 300


async def test_image_over_15mib_fails_with_limit_image_bytes(make_worker, source):
    w = await make_worker(source)
    source.add_image("/big.png", png_bytes(15 * 1024 * 1024 + 1))
    final, ch = await run_chapter(w, source, ["big.png"])
    assert ch["status"] == "FAILED" and ch["error_code"] == "LIMIT_IMAGE_BYTES" and "Halaman 1" in ch["error_message"]


async def test_image_exactly_15mib_is_accepted(make_worker, source):
    w = await make_worker(source)
    source.add_image("/ok.png", png_bytes(15 * 1024 * 1024))
    final, ch = await run_chapter(w, source, ["ok.png"])
    assert ch["status"] == "COMPLETED"


async def test_chapter_total_over_limit_fails_and_cleans(make_worker, source):
    w = await make_worker(source, max_bytes_per_chapter=1_000_000)
    for i in range(1, 6):
        source.add_image(f"/m/{i}.png", png_bytes(300_000))
    final, ch = await run_chapter(w, source, [f"m/{i}.png" for i in range(1, 6)])
    assert ch["status"] == "FAILED" and ch["error_code"] == "LIMIT_CHAPTER_BYTES"
    assert not w.state.staging.chapter_dir("j1", "c1").exists()


async def test_limits_from_fe_can_only_lower_never_raise(make_worker, source):
    w = await make_worker(source)
    source.add_image("/t.png", png_bytes(100))
    final, ch = await run_chapter(w, source, ["t.png"] * 5, limits={"max_pages": 3})
    assert ch["error_code"] == "LIMIT_PAGES"
    job = w.state.manager.jobs["j1"]
    assert job.limits.max_pages == 3
    source.add_manifest("/s2.json", simple_manifest(source, [("Chapter 1", ["t.png"])]))
    info = (await w.scan(source.url("/s2.json"))).json()
    await w.start_job("j2", source.url("/s2.json"), [{"key": "c1", "ref": info["chapters"][0]["ref"]}],
                      limits={"max_pages": 10**6, "max_chapter_bytes": 10**12, "max_image_bytes": 10**12})
    lim = w.state.manager.jobs["j2"].limits
    assert (lim.max_pages, lim.max_chapter_bytes, lim.max_image_bytes) == (300, 157286400, 15728640)


async def test_300_pages_totalling_about_150mib_completes(make_worker, source):
    """NFR-01: chapter 300 halaman ~150 MiB, tiap gambar < 15 MiB -> COMPLETED."""
    w = await make_worker(source, host_max_concurrency=8, image_concurrency=8)
    each = 524_000  # 300 x 524_000 = 157_200_000 B  (<= 157_286_400 = 150 MiB)
    source.add_image("/h.png", png_bytes(each))
    final, ch = await run_chapter(w, source, ["h.png"] * 300)
    assert ch["status"] == "COMPLETED" and ch["pages_done"] == 300
    manifest = (await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())).json()
    assert sum(p["bytes"] for p in manifest["pages"]) == 300 * each


# ------------------------------------------------------------ konkurensi
async def test_concurrency_limits_chapters_and_host(make_worker, source):
    w = await make_worker(source, chapter_concurrency=2, image_concurrency=4, host_max_concurrency=3)
    source.delay = 0.03
    for i in range(1, 7):
        source.add_image(f"/q/{i}.png", png_bytes(120))
    chapters = [(f"Chapter {n}", [f"q/{i}.png" for i in range(1, 7)]) for n in range(1, 21)]
    source.add_manifest("/s.json", simple_manifest(source, chapters))
    info = (await w.scan(source.url("/s.json"))).json()
    await w.start_job("j1", source.url("/s.json"), [{"key": f"c{i}", "ref": c["ref"]} for i, c in enumerate(info["chapters"])])
    seen_downloading = 0
    while True:
        data = await w.progress("j1")
        seen_downloading = max(seen_downloading, sum(1 for c in data["chapters"] if c["status"] == "DOWNLOADING"))
        if data["state"] in {"COMPLETED", "CANCELLED"}:
            break
        await asyncio.sleep(0.005)
    assert all(c["status"] == "COMPLETED" for c in data["chapters"])
    assert seen_downloading <= 2 and w.state.manager.peak_active_chapters <= 2
    assert source.peak <= 3  # SCAN_HOST_MAX_CONCURRENCY (manifest+gambar ke satu host)


async def test_host_min_delay_is_enforced_between_requests(make_worker, source):
    w = await make_worker(source, host_min_delay_ms=120, image_concurrency=4)
    pages = make_pages(source, 5)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", pages)]))
    info = (await w.scan(source.url("/s.json"))).json()
    t0 = time.monotonic()
    await w.start_job("j1", source.url("/s.json"), [{"key": "c1", "ref": info["chapters"][0]["ref"]}])
    await w.wait("j1")
    # 5 gambar + 1 manifest (di-cache) -> minimal 4 jeda antar gambar
    assert time.monotonic() - t0 >= 4 * 0.12 * 0.9


async def test_max_active_jobs_queues_extra_jobs(make_worker, source):
    w = await make_worker(source, max_active_jobs=1, chapter_concurrency=4)
    source.delay = 0.05
    pages = make_pages(source, 3)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", pages)]))
    info = (await w.scan(source.url("/s.json"))).json()
    ref = info["chapters"][0]["ref"]
    await w.start_job("ja", source.url("/s.json"), [{"key": "c1", "ref": ref}])
    await w.start_job("jb", source.url("/s.json"), [{"key": "c1", "ref": ref}])
    await asyncio.sleep(0.05)
    assert (await w.progress("jb"))["state"] == "QUEUED"
    assert (await w.progress("jb"))["chapters"][0]["status"] == "QUEUED"
    await w.wait("ja")
    await w.wait("jb")


# --------------------------------------------------------------- progress
async def test_progress_is_cheap_monotonic_and_makes_no_source_requests(make_worker, source):
    w = await make_worker(source)
    source.delay = 0.04
    pages = make_pages(source, 12)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", pages)]))
    info = (await w.scan(source.url("/s.json"))).json()
    await w.start_job("j1", source.url("/s.json"), [{"key": "c1", "ref": info["chapters"][0]["ref"]}])
    done_seen = []
    while True:
        before = sum(source.hits.values())
        data = await w.progress("j1")
        data2 = await w.progress("j1")
        assert sum(source.hits.values()) - before <= 4  # hanya aktivitas unduhan berjalan; progress tidak memicu request
        done_seen.append(data["chapters"][0]["pages_done"])
        if data["state"] == "COMPLETED":
            break
        await asyncio.sleep(0.01)
    assert done_seen == sorted(done_seen)
    stable_hits = sum(source.hits.values())
    for _ in range(5):
        await w.progress("j1")
    assert sum(source.hits.values()) == stable_hits  # job selesai: progress berulang = 0 request ke sumber


async def test_progress_before_listing_has_null_pages_total(make_worker, source):
    w = await make_worker(source, max_active_jobs=1)
    source.delay = 0.1
    pages = make_pages(source, 2)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", pages)]))
    info = (await w.scan(source.url("/s.json"))).json()
    ref = info["chapters"][0]["ref"]
    await w.start_job("ja", source.url("/s.json"), [{"key": "c1", "ref": ref}])
    await w.start_job("jb", source.url("/s.json"), [{"key": "c1", "ref": ref}])
    assert (await w.progress("jb"))["chapters"][0]["pages_total"] is None
    await w.wait("ja"); await w.wait("jb")


# ----------------------------------------------------------------- cancel
async def test_cancel_stops_requests_and_cleans_partial_files(make_worker, source):
    w = await make_worker(source, chapter_concurrency=1, image_concurrency=1)
    source.delay = 0.15
    pages = make_pages(source, 20)
    chapters = [(f"Chapter {n}", pages) for n in range(1, 4)]
    source.add_manifest("/s.json", simple_manifest(source, chapters))
    info = (await w.scan(source.url("/s.json"))).json()
    await w.start_job("j1", source.url("/s.json"), [{"key": f"c{i}", "ref": c["ref"]} for i, c in enumerate(info["chapters"])])
    await asyncio.sleep(0.5)
    r = await w.client.post("/worker/v1/jobs/j1/cancel", headers=w.h())
    assert r.status_code == 200 and r.json()["state"] == "CANCELLED"
    assert all(c["status"] == "CANCELLED" for c in r.json()["chapters"])
    hits_at_cancel = sum(source.hits.values())
    await asyncio.sleep(0.6)
    assert sum(source.hits.values()) == hits_at_cancel  # tidak ada request baru
    assert not any(w.state.staging.job_dir("j1").iterdir())


async def test_cancel_keeps_completed_chapters_available(make_worker, source):
    w = await make_worker(source, chapter_concurrency=1)
    pages = make_pages(source, 2)
    slow = make_pages(source, 8, prefix="/slow")
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", pages), ("Chapter 2", slow)]))
    for i in range(1, 9):
        source.routes[f"/slow/{i:03d}.png"] = lambda c, h: (time.sleep(0.2), (200, png_bytes(100), "image/png"))[1]
    info = (await w.scan(source.url("/s.json"))).json()
    by = {c["number"]: c["ref"] for c in info["chapters"]}
    await w.start_job("j1", source.url("/s.json"), [{"key": "c1", "ref": by["1"]}, {"key": "c2", "ref": by["2"]}])
    for _ in range(100):
        p = await w.progress("j1")
        if p["chapters"][0]["status"] == "COMPLETED":
            break
        await asyncio.sleep(0.02)
    await w.client.post("/worker/v1/jobs/j1/cancel", headers=w.h())
    p = await w.progress("j1")
    st = {c["key"]: c["status"] for c in p["chapters"]}
    assert st == {"c1": "COMPLETED", "c2": "CANCELLED"}
    assert (await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())).status_code == 200


# ---------------------------------------------------------- staging/sweeper
async def test_delete_chapter_and_job_cleanup(make_worker, source):
    w = await make_worker(source)
    final, ch = await run_chapter(w, source, make_pages(source, 2))
    d = w.state.staging.chapter_dir("j1", "c1")
    assert d.exists()
    assert (await w.client.delete("/worker/v1/jobs/j1/chapters/c1", headers=w.h())).status_code == 200
    assert not d.exists()
    assert (await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())).status_code == 409
    assert (await w.client.delete("/worker/v1/jobs/j1", headers=w.h())).status_code == 200
    assert (await w.client.get("/worker/v1/jobs/j1/progress", headers=w.h())).status_code == 404
    assert not w.state.staging.job_dir("j1").exists()


async def test_sweeper_removes_orphans_older_than_ttl_and_cancelled_jobs(make_worker, source):
    import os

    w = await make_worker(source, staging_ttl_hours=1, cancelled_cleanup_s=60)
    st = w.state.staging
    orphan = st.jobs_dir / "yatim"; orphan.mkdir(); (orphan / "x").write_text("x")
    fresh = st.jobs_dir / "baru"; fresh.mkdir()
    cover_old = st.covers_dir / "s_0123456789abcdef.png"; cover_old.write_bytes(b"x")
    old = time.time() - 2 * 3600
    for p in (orphan, cover_old):
        os.utime(p, (old, old))
    removed = w.state.manager.sweep()
    assert not orphan.exists() and not cover_old.exists() and fresh.exists() and removed == 2
    # job yang dibatalkan dibersihkan setelah cancelled_cleanup_s
    final, ch = await run_chapter(w, source, make_pages(source, 2), job_id="jx")
    w.state.manager.jobs["jx"].state = "CANCELLED"
    w.state.manager.sweep(now=w.state.manager.jobs["jx"].finished_at + 61)
    assert "jx" not in w.state.manager.jobs and not st.job_dir("jx").exists()


async def test_startup_sweeps_orphaned_staging(tmp_path, source):
    import httpx
    from app.main import create_app
    from .conftest import make_settings

    settings = make_settings(tmp_path, source)
    (settings.staging_dir / "jobs" / "lama").mkdir(parents=True)
    (settings.staging_dir / "jobs" / "lama" / "f").write_text("x")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert not (settings.staging_dir / "jobs" / "lama").exists()


def test_staging_not_under_static_mount():
    from pathlib import Path
    from app.config import BASE_DIR

    assert "storage" not in Path(BASE_DIR / "staging").parts


async def test_insufficient_disk_rejects_job(make_worker, source):
    w = await make_worker(source, min_free_disk_bytes=10**15)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", ["a.png"])]))
    info = (await w.scan(source.url("/s.json"))).json()
    r = await w.start_job("j1", source.url("/s.json"), [{"key": "c1", "ref": info["chapters"][0]["ref"]}])
    assert r.status_code == 507 and r.json()["code"] == "INSUFFICIENT_DISK"


async def test_job_validation_errors(make_worker, source):
    w = await make_worker(source)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", [])]))
    url = source.url("/s.json")
    ref = (await w.scan(url)).json()["chapters"][0]["ref"]
    assert (await w.start_job("j1", url, [])).status_code == 400
    assert (await w.start_job("j1", url, [{"key": "../x", "ref": ref}])).status_code == 400
    assert (await w.start_job("bad id", url, [{"key": "c", "ref": ref}])).status_code == 400
    assert (await w.start_job("j1", url, [{"key": "c", "ref": ref}, {"key": "c", "ref": ref}])).status_code == 400
    assert (await w.start_job("j1", url, [{"key": "c", "ref": ref}], adapter="fixture")).status_code == 422
    assert (await w.start_job("j1", "https://lain.example/x.json", [{"key": "c", "ref": ref}])).status_code == 422
    assert (await w.start_job("j1", url, [{"key": "c", "ref": ref}])).status_code == 202
    assert (await w.start_job("j1", url, [{"key": "c", "ref": ref}])).status_code == 409


# ------------------------------------------------------------------ SSRF
async def test_image_url_to_private_ip_fails_ssrf_blocked_without_request(make_worker, source):
    w = await make_worker(source)
    listener = FakeSource(host="127.0.0.2")  # loopback lain, TIDAK di-allowlist
    try:
        listener.add_image("/secret.png", png_bytes(100))
        final, ch = await run_chapter(w, source, [listener.url("/secret.png")])
        assert ch["status"] == "FAILED" and ch["error_code"] == "SSRF_BLOCKED"
        assert listener.hits == {}  # tidak ada request ke IP privat
    finally:
        listener.stop()


@pytest.mark.parametrize("target", ["http://10.0.0.5/a.png", "http://169.254.169.254/latest/meta-data", "http://[::1]/a.png", "http://2130706433/a.png"])
async def test_image_urls_to_blocked_addresses(make_worker, source, target):
    w = await make_worker(source)
    final, ch = await run_chapter(w, source, [target])
    assert ch["error_code"] == "SSRF_BLOCKED"


async def test_redirect_to_loopback_is_blocked_at_each_hop(make_worker, source):
    w = await make_worker(source)
    victim = FakeSource(host="127.0.0.2")
    try:
        victim.add_image("/internal.png", png_bytes(100))
        source.routes["/r/1.png"] = (302, b"", "text/plain", {"Location": victim.url("/internal.png")})
        final, ch = await run_chapter(w, source, ["r/1.png"])
        assert ch["error_code"] == "SSRF_BLOCKED" and victim.hits == {}
    finally:
        victim.stop()


async def test_redirect_within_allowlisted_host_is_followed_and_loops_are_capped(make_worker, source):
    w = await make_worker(source)
    source.add_image("/real.png", png_bytes(100))
    source.routes["/ok/1.png"] = (302, b"", "text/plain", {"Location": "/real.png"})
    final, ch = await run_chapter(w, source, ["ok/1.png"])
    assert ch["status"] == "COMPLETED"
    source.routes["/loop/1.png"] = (302, b"", "text/plain", {"Location": "/loop/1.png"})
    final, ch = await run_chapter(w, source, ["loop/1.png"], job_id="j2")
    assert ch["status"] == "FAILED" and ch["error_code"] == "TOO_MANY_REDIRECTS"


async def test_image_on_host_outside_allowlist_is_rejected(make_worker, source):
    w = await make_worker(source)
    final, ch = await run_chapter(w, source, ["https://cdn-lain.example/a.png"])
    assert ch["error_code"] == "HOST_NOT_ALLOWED"


async def test_dns_pinning_connects_to_resolved_ip_and_sends_original_host(tmp_path, source):
    from app.scan.http import GuardedHttp
    from app.scan.netguard import GuardPolicy
    from .conftest import make_settings

    source.add_image("/img.png", png_bytes(100))
    resolved = []

    async def resolver(host, port):
        resolved.append(host)
        return ["127.0.0.1"]

    settings = make_settings(tmp_path)
    policy = GuardPolicy(allowed_hosts=frozenset({f"pinned.example:{source.port}"}), allow_http=True, allow_private_for_allowlisted=True, resolver=resolver)
    http = GuardedHttp(settings, policy)
    try:
        data, _ = await http.fetch_bytes(f"http://pinned.example:{source.port}/img.png", max_bytes=10_000, too_large=Exception("x"), timeout=5)
        assert len(data) == 100 and resolved == ["pinned.example"]  # satu kali resolve, IP dipakai untuk koneksi
        assert source.log[-1][1]["host"] == f"pinned.example:{source.port}"
        assert source.log[-1][1]["user-agent"].startswith("LembarScan/1.0")
    finally:
        await http.aclose()


async def test_fetch_headers_cannot_override_user_agent(tmp_path, source):
    from app.scan.http import GuardedHttp
    from app.scan.netguard import GuardPolicy
    from .conftest import make_settings

    source.add_image("/img.png", png_bytes(100))
    settings = make_settings(tmp_path, source)
    http = GuardedHttp(settings, GuardPolicy(allowed_hosts=frozenset({source.hostport}), allow_http=True, allow_private_for_allowlisted=True))
    try:
        await http.fetch_bytes(source.url("/img.png"), max_bytes=10_000, too_large=Exception("x"), timeout=5,
                               headers={"User-Agent": "Mozilla/5.0 Chrome", "Referer": "https://sumber.example/", "Cookie": "x=1"})
        headers = source.log[-1][1]
        assert headers["user-agent"] == "LembarScan/1.0" and headers["referer"] == "https://sumber.example/" and "cookie" not in headers
    finally:
        await http.aclose()
