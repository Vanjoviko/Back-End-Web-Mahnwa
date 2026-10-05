"""Regresi temuan QA: D-04 (docs terbuka tanpa token) dan D-05 (log httpx mencetak URL + query/token)."""
import logging

import pytest

from app.scan.logsafe import redact_text

from .conftest import png_bytes

SECRET = "SECRETQA123"


# ---------------------------------------------------------------- D-05
async def test_d05_worker_log_never_contains_query_tokens(make_worker, source, caplog):
    caplog.set_level(logging.INFO)
    w = await make_worker(source)
    source.add_image("/cv.png", png_bytes(2000))
    source.add_manifest("/m/pages/manifest.json", {"title": "Seri Uji", "cover": f"/cv.png?token={SECRET}&sig=abc", "chapters": [{"chapter": "Chapter 1", "pages": []}]})
    r = await w.scan(source.url(f"/m/pages/manifest.json?token={SECRET}&sig=abc"))
    assert r.status_code == 200, r.text
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING, "logger httpx harus dibisukan di bawah WARNING"
    text = caplog.text
    assert SECRET not in text and "sig=abc" not in text, text
    assert "HTTP Request" not in text, "httpx tidak boleh mencetak baris permintaan"


async def test_d05_redaction_catches_library_logs_even_if_httpx_info_is_enabled(make_worker, source, caplog):
    """Lapis kedua: walau seseorang menaikkan logger httpx kembali ke INFO, token tetap diredaksi."""
    caplog.set_level(logging.INFO)
    w = await make_worker(source)
    logging.getLogger("httpx").setLevel(logging.INFO)
    try:
        source.add_manifest("/m2.json", {"title": "Seri Uji", "chapters": [{"chapter": "Chapter 1", "pages": []}]})
        r = await w.scan(source.url(f"/m2.json?token={SECRET}&sig=abc"))
        assert r.status_code == 200
    finally:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    assert SECRET not in caplog.text and "sig=abc" not in caplog.text, caplog.text
    # bukti bahwa logger httpx memang menulis (diredaksi), bukan sekadar diam
    assert any(rec.name == "httpx" for rec in caplog.records)
    assert "?[diredaksi]" in caplog.text


def test_d05_redact_text_unit():
    assert redact_text('HTTP Request: GET http://h:1/a/b.json?token=SECRETQA123&sig=abc "HTTP/1.1 200 OK"') == 'HTTP Request: GET http://h:1/a/b.json?[diredaksi] "HTTP/1.1 200 OK"'
    assert redact_text('127.0.0.1 - "GET /worker/v1/x?secret=1 HTTP/1.1" 200') == '127.0.0.1 - "GET /worker/v1/x?[diredaksi] HTTP/1.1" 200'
    assert redact_text("https://user:pass@host.example/p") == "https://[diredaksi]@host.example/p"
    assert redact_text("Apakah sudah selesai? Ya.") == "Apakah sudah selesai? Ya."
    assert redact_text("tanpa url") == "tanpa url"


def test_d05_record_factory_redacts_formatted_args(caplog):
    caplog.set_level(logging.INFO)
    from app.main import create_app  # noqa: F401  (memastikan modul terpasang)
    from app.scan.logsafe import install_log_redaction

    install_log_redaction()
    logging.getLogger("pustaka.lain").info("GET %s status=%d", f"https://x.example/a?token={SECRET}", 200)
    assert SECRET not in caplog.text
    assert "https://x.example/a?[diredaksi] status=200" in caplog.text


# ---------------------------------------------------------------- D-04
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
async def test_d04_docs_and_openapi_are_not_exposed(make_worker, path):
    w = await make_worker()
    for headers in ({}, w.h(), w.h("salah")):
        r = await w.client.get(path, headers=headers)
        assert r.status_code in {401, 404}, f"{path} -> {r.status_code}"
        assert "openapi" not in r.text.lower() and "swagger" not in r.text.lower() and "/worker/v1/scan" not in r.text


async def test_d04_health_and_worker_api_still_work(make_worker):
    w = await make_worker()
    assert (await w.client.get("/health")).status_code == 200
    assert (await w.client.get("/worker/v1/status", headers=w.h())).status_code == 200
    assert (await w.client.get("/worker/v1/status")).status_code == 401
    assert w.app.docs_url is None and w.app.redoc_url is None and w.app.openapi_url is None


# ---------------------------------------------------------------- D-09 (regresi akibat D-05: args dikosongkan -> uvicorn.access "Logging error")
def _make_record(name, msg, args):
    return logging.getLogRecordFactory()(name, logging.INFO, __file__, 1, msg, args, None)


def test_d09_redaction_preserves_args_shape_and_types():
    from app.scan.logsafe import install_log_redaction

    install_log_redaction()
    # bentuk uvicorn.access: (client_addr, method, full_path, http_version, status_code)
    r = _make_record("uvicorn.access", '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5555", "GET", f"/health?a=1&token={SECRET}", "1.1", 200))
    assert isinstance(r.args, tuple) and len(r.args) == 5, r.args
    assert r.args[2] == "/health?[diredaksi]" and r.args[4] == 200 and isinstance(r.args[4], int)
    assert r.args[0] == "127.0.0.1:5555" and r.args[1] == "GET" and r.args[3] == "1.1"
    assert SECRET not in r.getMessage()
    # args bertipe dict (%(nama)s) tetap dict dengan kunci yang sama
    r = _make_record("lib", "url=%(u)s n=%(n)d", ({"u": f"https://x.example/a?token={SECRET}", "n": 3},))
    assert isinstance(r.args, dict) and set(r.args) == {"u", "n"} and r.args["n"] == 3
    assert SECRET not in r.getMessage() and r.getMessage() == "url=https://x.example/a?[diredaksi] n=3"
    # objek non-str (httpx.URL) tetap argumen tunggal; tipe boleh menjadi str hanya bila memuat rahasia
    import httpx

    r = _make_record("httpx", "HTTP Request: %s %s", ("GET", httpx.URL(f"http://h/p?token={SECRET}")))
    assert len(r.args) == 2 and SECRET not in r.getMessage()
    # tanpa rahasia: record tidak disentuh
    r = _make_record("lib", "aman %s %d", ("x", 1))
    assert r.args == ("x", 1) and r.msg == "aman %s %d"
    # rahasia lintas-batas (templat + argumen) tetap tertutup, dan getMessage() tidak pernah melempar
    r = _make_record("lib", "GET %s?%s", ("http://h/p", f"token={SECRET}"))
    assert SECRET not in r.getMessage()
    # pesan tanpa args dengan rahasia di templat
    r = _make_record("lib", f"mengambil http://h/p?token={SECRET}", ())
    assert SECRET not in r.getMessage()


def test_d09_uvicorn_access_formatter_works_with_redacted_record(capsys):
    """Handler asli uvicorn (AccessFormatter butuh 5 args) tidak boleh menghasilkan 'Logging error'."""
    import io

    from uvicorn.logging import AccessFormatter

    from app.scan.logsafe import install_log_redaction

    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    lg = logging.getLogger("uvicorn.access")
    lg.addHandler(handler)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        lg.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", f"/health?a=1&token={SECRET}", "1.1", 200)
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old)
    err = capsys.readouterr().err
    assert "Logging error" not in err and "Traceback" not in err and "ValueError" not in err, err
    out = stream.getvalue()
    assert 'GET /health?[diredaksi] HTTP/1.1" 200' in out, out
    assert SECRET not in out


async def test_d09_real_uvicorn_request_with_query_no_logging_error(tmp_path, capfd):
    """Worker sungguhan di bawah uvicorn (log akses default): request ber-query -> tanpa 'Logging error', rahasia diredaksi."""
    import asyncio
    import socket

    import httpx
    import uvicorn

    from app.main import create_app

    from .conftest import make_settings

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = create_app(make_settings(tmp_path))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn tidak start"
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/health?a=1&token={SECRET}")
            assert r.status_code == 200
            r = await client.get(f"http://127.0.0.1:{port}/worker/v1/status?sig=abc&token={SECRET}", headers={"X-Worker-Token": "test-token-123"})
            assert r.status_code in {200, 401}
        await asyncio.sleep(0.2)
    finally:
        server.should_exit = True
        await task
    captured = capfd.readouterr()
    text = captured.out + captured.err
    assert "Logging error" not in text and "Traceback" not in text and "ValueError" not in text, text
    assert SECRET not in text and "sig=abc" not in text, text
    assert 'GET /health?[diredaksi] HTTP/1.1" 200' in text, text
