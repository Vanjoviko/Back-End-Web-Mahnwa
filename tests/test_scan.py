"""BE-02, BE-03, FR-02/03 (sisi worker), NFR-05: scan metadata, cover, manifest, fixture adapter."""
import asyncio
import json

import pytest

from .conftest import FakeSource, jpeg_bytes, png_bytes, simple_manifest


async def test_scan_normalizes_metadata_and_sorts_chapters(make_worker, source):
    w = await make_worker(source)
    source.add_image("/cv.png", png_bytes(2000))
    manifest = {
        "title": "Judul Uji", "alt": "Alt", "synopsis": "Deskripsi", "status": "completed", "author": "Kreator",
        "type": "manhua", "genres": ["Aksi", " ", 5, "Fantasi"], "year": 2024, "cover": "cv.png", "permission": "Izin tercatat",
        "chapters": [
            {"chapter": "Chapter 2", "title": "B", "date": "2026-09-30T10:00:00Z", "pages": []},
            {"chapter": "Chapter 10", "title": "", "date": "bukan tanggal", "pages": []},
            {"chapter": "Prolog", "pages": []},
            {"chapter": "Extra", "pages": []},
            {"chapter": "Chapter 10.0", "pages": []},
        ],
    }
    source.add_manifest("/s.json", manifest)
    r = await w.scan(source.url("/s.json"))
    assert r.status_code == 200
    data = r.json()
    assert data["adapter"] == "manifest" and data["adapter_label"] == "Manifest JSON"
    assert data["canonical_url"] == source.url("/s.json")
    assert data["comic"] == {"title": "Judul Uji", "alt": "Alt", "synopsis": "Deskripsi", "status": "Tamat", "author": "Kreator",
                             "type": "Manhua", "genres": ["Aksi", "Fantasi"], "year": 2024}
    assert [c["number"] for c in data["chapters"]] == ["10", "10", "2", "0", None]
    assert data["chapters"][0]["issues"] == ["duplicate_number"]
    assert data["chapters"][-1]["issues"] == ["needs_number"]
    assert data["chapters"][2]["date"] == "2026-09-30" and data["chapters"][0]["date"] in (None, "2026-09-30")
    assert data["warnings"] == []
    assert data["permission_note"] == "Izin tercatat"
    cover = data["cover"]
    assert cover["content_type"] == "image/png" and cover["bytes"] == 2000
    staged = (await w.client.get(f"/worker/v1/staging/{cover['staged_id']}", headers=w.h()))
    assert staged.status_code == 200 and staged.headers["content-type"] == "image/png" and len(staged.content) == 2000
    assert all("pages" not in c and "page_urls" not in c for c in data["chapters"])  # tidak ada URL gambar di respons scan


async def test_missing_fields_are_null_not_invented(make_worker, source):
    w = await make_worker(source)
    source.add_manifest("/s.json", {"title": "Hanya Judul", "chapters": []})
    data = (await w.scan(source.url("/s.json"))).json()
    assert data["comic"]["status"] is None and data["comic"]["author"] is None and data["comic"]["type"] is None
    assert data["comic"]["year"] is None and data["comic"]["genres"] == [] and data["cover"] is None
    assert set(data["warnings"]) == {"COVER_UNAVAILABLE", "NO_CHAPTERS", "PERMISSION_NOT_RECORDED"}
    assert data["chapters"] == []


@pytest.mark.parametrize("manifest", [{"title": ""}, {"title": "   "}, {"chapters": []}, [], {"title": "X", "chapters": "bukan list"}])
async def test_empty_title_or_bad_shape_is_parse_error_not_unknown_manga(make_worker, source, manifest):
    w = await make_worker(source)
    source.add_manifest("/s.json", manifest)
    r = await w.scan(source.url("/s.json"))
    assert r.status_code == 422 and r.json()["code"] == "SCAN_PARSE_ERROR"
    assert "Unknown Manga" not in r.text


async def test_invalid_json_is_parse_error(make_worker, source):
    w = await make_worker(source)
    source.routes["/s.json"] = (200, b"{tidak valid", "application/json")
    r = await w.scan(source.url("/s.json"))
    assert r.status_code == 422 and r.json()["code"] == "SCAN_PARSE_ERROR"


async def test_manifest_over_5mb_rejected(make_worker, source):
    w = await make_worker(source)
    source.routes["/s.json"] = (200, b'{"title":"x","pad":"' + b"a" * 5_100_000 + b'"}', "application/json")
    r = await w.scan(source.url("/s.json"))
    assert r.status_code == 422 and r.json()["code"] == "SCAN_PARSE_ERROR" and "5 MB" in r.json()["error"]


async def test_scan_1000_chapters_is_accepted(make_worker, source):
    w = await make_worker(source)
    chapters = [{"chapter": f"Chapter {n}", "pages": []} for n in range(1, 1001)]
    source.add_manifest("/s.json", {"title": "Seribu", "chapters": chapters})
    data = (await w.scan(source.url("/s.json"))).json()
    assert len(data["chapters"]) == 1000 and data["chapters"][0]["number"] == "1000" and data["chapters"][-1]["number"] == "1"


async def test_scan_requests_to_source_are_minimal_and_no_page_fetch(make_worker, source):
    w = await make_worker(source)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", ["a.png", "b.png"])]))
    await w.scan(source.url("/s.json"))
    assert source.hits == {"/s.json": 1}  # hanya manifest (tidak ada gambar chapter)


# ---------------------------------------------------------------- URL / FR-02
@pytest.mark.parametrize("url", ["ftp://x", "https://user:pw@sumber.example/a.json", "http://127.0.0.1/a.json", "bukan url", "http://10.0.0.5/a.json", "file:///etc/passwd"])
async def test_invalid_scan_urls_rejected_without_outgoing_request(make_worker, source, url):
    w = await make_worker(source)
    r = await w.scan(url)
    assert r.status_code == 400 and r.json()["code"] in {"INVALID_URL", "SSRF_BLOCKED"}, (url, r.json())
    assert source.hits == {}


async def test_valid_host_without_adapter_is_422_listing_active_adapters(make_worker, source):
    w = await make_worker(source)
    r = await w.scan(source.url("/halaman.html"))  # host di allowlist, tetapi bukan .json
    assert r.status_code == 422 and r.json()["code"] == "SOURCE_NOT_SUPPORTED" and "manifest" in r.json()["error"]
    r = await w.scan("https://tidak-diizinkan.example/a.json")
    assert r.status_code == 422 and r.json()["code"] == "SOURCE_NOT_SUPPORTED"
    assert source.hits == {}


async def test_default_empty_allowlist_means_scan_inactive(make_worker):
    w = await make_worker(None, dev_mode=False)
    r = await w.scan("https://sumber.example/a.json")
    assert r.status_code == 422 and r.json()["code"] == "SOURCE_NOT_SUPPORTED" and "SCAN_ALLOWED_HOSTS" in r.json()["error"]


async def test_scan_timeout_returns_504(make_worker, source):
    w = await make_worker(source, scan_timeout_s=0.3, html_timeout_s=5)
    source.delay = 1.5
    source.add_manifest("/s.json", {"title": "Lambat"})
    r = await w.scan(source.url("/s.json"))
    assert r.status_code == 504 and r.json()["code"] == "SCAN_TIMEOUT"


async def test_source_403_on_scan_is_source_refused(make_worker, source):
    w = await make_worker(source)
    source.routes["/s.json"] = (403, b"no", "text/plain")
    r = await w.scan(source.url("/s.json"))
    assert r.status_code == 403 and r.json()["code"] == "SOURCE_REFUSED"
    assert source.hits["/s.json"] == 1


# ------------------------------------------------------------------ cover
async def test_cover_png_saved_as_png_even_if_url_says_jpg(make_worker, source):
    w = await make_worker(source)
    source.add_image("/cover.jpg", png_bytes(1000))
    source.add_manifest("/s.json", {"title": "T", "cover": "/cover.jpg", "chapters": []})
    cover = (await w.scan(source.url("/s.json"))).json()["cover"]
    assert cover["content_type"] == "image/png"
    assert list(w.state.staging.covers_dir.glob(f"{cover['staged_id']}.png"))


async def test_cover_6mb_gives_warning_and_null_cover(make_worker, source):
    w = await make_worker(source)
    source.add_image("/cover.png", png_bytes(6_000_000))
    source.add_manifest("/s.json", {"title": "T", "cover": "cover.png", "chapters": []})
    data = (await w.scan(source.url("/s.json"))).json()
    assert data["cover"] is None and "COVER_UNAVAILABLE" in data["warnings"]


async def test_cover_pointing_to_private_ip_gives_ssrf_warning_without_request(make_worker, source):
    w = await make_worker(source)
    spy = FakeSource(host="127.0.0.2")
    try:
        spy.add_image("/c.png", png_bytes(500))
        source.add_manifest("/s.json", {"title": "T", "cover": spy.url("/c.png"), "chapters": []})
        r = await w.scan(source.url("/s.json"))
        data = r.json()
        assert r.status_code == 200 and data["cover"] is None and "SSRF_BLOCKED" in data["warnings"] and "COVER_UNAVAILABLE" in data["warnings"]
        assert spy.hits == {}
    finally:
        spy.stop()


async def test_cover_not_an_image_is_warning_not_failure(make_worker, source):
    w = await make_worker(source)
    source.routes["/cover.png"] = (200, b"<html>bukan gambar</html>", "text/html")
    source.add_manifest("/s.json", {"title": "T", "cover": "cover.png", "chapters": []})
    r = await w.scan(source.url("/s.json"))
    assert r.status_code == 200 and r.json()["cover"] is None and "COVER_UNAVAILABLE" in r.json()["warnings"]


async def test_relative_cover_resolved_against_manifest_url(make_worker, source):
    w = await make_worker(source)
    source.add_image("/series/x/cover.jpg", jpeg_bytes(900), "image/jpeg")
    source.add_manifest("/series/x/manifest.json", {"title": "T", "cover": "cover.jpg", "chapters": []})
    cover = (await w.scan(source.url("/series/x/manifest.json"))).json()["cover"]
    assert cover["content_type"] == "image/jpeg"


async def test_canonical_url_normalization(make_worker, source):
    from app.scan.service import canonical_url

    assert canonical_url("HTTPS://Sumber.Example:443/Series/judul/?utm_source=x&b=2&a=1#frag") == "https://sumber.example/Series/judul?a=1&b=2"
    assert canonical_url("https://sumber.example//a//b/") == "https://sumber.example/a/b"
    assert canonical_url("fixture://menara-biru") == "fixture://menara-biru"


async def test_manifest_page_pointing_outside_allowlist_is_rejected_at_download(make_worker, source):
    w = await make_worker(source)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", ["https://cdn-asing.example/1.png"])]))
    info = (await w.scan(source.url("/s.json"))).json()
    await w.start_job("j1", source.url("/s.json"), [{"key": "c1", "ref": info["chapters"][0]["ref"]}])
    final = await w.wait("j1")
    assert final["chapters"][0]["error_code"] == "HOST_NOT_ALLOWED"


async def test_stale_ref_after_manifest_change_fails_cleanly(make_worker, source):
    w = await make_worker(source)
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 1", ["a.png"])]))
    info = (await w.scan(source.url("/s.json"))).json()
    source.add_manifest("/s.json", simple_manifest(source, [("Chapter 99", ["a.png"])]))
    await w.start_job("j1", source.url("/s.json"), [{"key": "c1", "ref": info["chapters"][0]["ref"]}])
    final = await w.wait("j1")
    assert final["chapters"][0]["status"] == "FAILED" and final["chapters"][0]["error_code"] == "SCAN_PARSE_ERROR"


# ---------------------------------------------------------- FixtureAdapter
@pytest.fixture
def fixtures(tmp_path):
    from tools.fixture_source import generate

    root = tmp_path / "fixtures"
    generate(root, pages_per_chapter=(3, 5))
    return root


async def test_fixture_adapter_end_to_end_without_network(make_worker, fixtures, tmp_path):
    w = await make_worker(None, enable_fixture=True, fixture_dir=fixtures, allowed_hosts=frozenset())
    data = (await w.scan("fixture://menara-biru")).json()
    assert data["adapter"] == "fixture" and data["comic"]["title"] == "Menara Biru Mekar" and data["comic"]["status"] == "Berjalan"
    numbers = [c["number"] for c in data["chapters"]]
    assert numbers == ["8.5", "8", "7", "6", "5", "4", "3", "2", "1", "0", None]  # Prolog -> 0, Extra -> null
    assert data["cover"]["content_type"] == "image/png"
    assert data["warnings"] == []
    chapters = [{"key": f"c{i}", "ref": c["ref"]} for i, c in enumerate(data["chapters"])]
    assert (await w.start_job("jf", "fixture://menara-biru", chapters, adapter="fixture")).status_code == 202
    final = await w.wait("jf")
    assert all(c["status"] == "COMPLETED" for c in final["chapters"]), final
    exts = set()
    for c in final["chapters"]:
        manifest = (await w.client.get(f"/worker/v1/jobs/jf/chapters/{c['key']}/manifest", headers=w.h())).json()
        assert [p["page_number"] for p in manifest["pages"]] == list(range(1, c["pages_total"] + 1))
        exts |= {p["content_type"] for p in manifest["pages"]}
    assert exts == {"image/png", "image/jpeg", "image/webp"}


async def test_fixture_adapter_disabled_by_default(make_worker, fixtures):
    w = await make_worker(None, enable_fixture=False, fixture_dir=fixtures, dev_mode=True)
    r = await w.scan("fixture://menara-biru")
    assert r.status_code in {400, 422}


async def test_fixture_path_traversal_blocked(make_worker, fixtures):
    (fixtures / "rahasia.txt").write_text("rahasia")
    w = await make_worker(None, enable_fixture=True, fixture_dir=fixtures)
    manifest = {"title": "X", "chapters": [{"chapter": "Chapter 1", "pages": ["../rahasia.txt"]}]}
    (fixtures / "jahat.json").write_text(json.dumps(manifest))
    (fixtures / "jahat").mkdir()
    ref = (await w.scan("fixture://jahat")).json()["chapters"][0]["ref"]
    await w.start_job("jt", "fixture://jahat", [{"key": "c1", "ref": ref}], adapter="fixture")
    final = await w.wait("jt")
    assert final["chapters"][0]["status"] == "FAILED"
    r = await w.scan("fixture://../etc/passwd")
    assert r.status_code == 400


async def test_bermasalah_series_over_http_matches_spec_ac(make_worker, fixtures):
    """Seri fixture dengan halaman 500/404 lewat server HTTP fixture buatan sendiri."""
    from tools.fixture_source import make_server
    import threading

    server = make_server(fixtures, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        w = await make_worker(None, allowed_hosts=frozenset({f"127.0.0.1:{port}"}))
        url = f"http://127.0.0.1:{port}/bermasalah/manifest.json"
        data = (await w.scan(url)).json()
        by_title = {c["title"]: c for c in data["chapters"]}
        assert by_title["Kembar A"]["issues"] == ["duplicate_number"] and by_title["Kembar B"]["issues"] == ["duplicate_number"]
        await w.start_job("jh", url, [{"key": f"c{i}", "ref": c["ref"]} for i, c in enumerate(data["chapters"])])
        final = await w.wait("jh")
        states = {data["chapters"][int(c["key"][1:])]["title"]: c for c in final["chapters"]}
        assert states["Semua baik"]["status"] == "COMPLETED"
        assert states["Halaman 2 selalu 500"]["status"] == "FAILED" and "Halaman 2" in states["Halaman 2 selalu 500"]["error_message"]
        assert states["Halaman 404"]["status"] == "FAILED" and states["Halaman 404"]["attempts"] == 1
    finally:
        server.shutdown()
