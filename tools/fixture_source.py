"""Sumber fixture lokal untuk pengujian & demo scan-import.

SEMUA konten dibuat secara terprogram (Pillow) dan orisinal -- placeholder, bukan karya nyata.

Pemakaian:
  python -m tools.fixture_source --root .fixture-data --port 9100          # generate (bila belum ada) + serve
  python -m tools.fixture_source --root .fixture-data --generate-only      # hanya generate

Hasil:
  <root>/<nama>.json        manifest (dipakai FixtureAdapter: fixture://<nama>)
  <root>/<nama>/...         gambar; server HTTP menyajikan manifest di /<nama>/manifest.json
Seri yang dibuat:
  menara-biru   seri normal (Prolog, Chapter 1-8, 8.5, Extra), gambar JPEG/PNG/WebP campuran
  bermasalah    seri HTTP-only dengan halaman rusak (/fail/500, /fail/404) untuk uji gagal & retry
"""
from __future__ import annotations

import argparse
import json
import math
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LIME = (197, 240, 108)
DARK = (16, 17, 16)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow lama
        return ImageFont.load_default()


def make_image(path: Path, size: tuple[int, int], title: str, subtitle: str, seed: int, fmt: str | None = None) -> None:
    rng = random.Random(seed)
    w, h = size
    img = Image.new("RGB", size, DARK)
    draw = ImageDraw.Draw(img)
    for y in range(h):  # gradien vertikal gelap
        shade = int(18 + 28 * (y / h))
        draw.line([(0, y), (w, y)], fill=(shade, shade + 4, shade))
    for _ in range(9):  # bentuk acak orisinal
        cx, cy, r = rng.randint(0, w), rng.randint(0, h), rng.randint(w // 14, w // 4)
        tone = rng.choice([LIME, (60, 90, 70), (245, 168, 107), (80, 120, 100)])
        alpha = rng.randint(40, 120)
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse([cx - r, cy - r, cx + r, cy + r], fill=tone + (alpha,))
        img.paste(layer, (0, 0), layer)
    for i in range(0, w, 40):  # garis "menara"
        top = h - int((math.sin(i / 53 + seed) + 1.3) * h * 0.22)
        draw.rectangle([i, top, i + 22, h], fill=(25, 35, 30))
    draw.rectangle([0, 0, w - 1, h - 1], outline=LIME, width=4)
    draw.text((28, 28), title, fill=LIME, font=_font(max(18, w // 18)))
    draw.text((28, 28 + max(18, w // 18) + 14), subtitle, fill=(230, 235, 228), font=_font(max(14, w // 28)))
    draw.text((28, h - 44), "PLACEHOLDER ORISINAL - dibuat oleh generator", fill=(150, 160, 150), font=_font(max(12, w // 40)))
    path.parent.mkdir(parents=True, exist_ok=True)
    save_fmt = fmt or {".png": "PNG", ".webp": "WEBP"}.get(path.suffix.lower(), "JPEG")
    img.save(path, save_fmt, **({"quality": 82} if save_fmt in {"JPEG", "WEBP"} else {}))


def generate(root: Path, *, pages_per_chapter: tuple[int, int] = (4, 8)) -> None:
    root.mkdir(parents=True, exist_ok=True)
    exts = [".jpg", ".png", ".webp"]

    # --- seri normal -------------------------------------------------------
    name = "menara-biru"
    labels = ["Prolog"] + [f"Chapter {n}" for n in range(1, 9)] + ["Chapter 8.5", "Extra"]
    chapters = []
    for ci, label in enumerate(labels):
        count = pages_per_chapter[0] + (ci * 3) % (pages_per_chapter[1] - pages_per_chapter[0] + 1)
        pages = []
        for p in range(1, count + 1):
            rel = f"c{ci:02d}/{p:03d}{exts[(ci + p) % 3]}"
            make_image(root / name / rel, (720, 1100), "Menara Biru Mekar", f"{label} · halaman {p}/{count}", seed=ci * 100 + p)
            pages.append(rel)
        chapters.append({"chapter": label, "title": {"Prolog": "Sebelum Fajar", "Extra": "Cerita Sampingan"}.get(label, f"Bagian {ci}"), "date": f"2026-09-{min(28, 1 + ci * 2):02d}", "pages": pages})
    make_image(root / name / "cover.png", (600, 850), "MENARA BIRU", "MEKAR", seed=7)
    manifest = {
        "id": name,
        "title": "Menara Biru Mekar",
        "alt": "Blue Tower Bloom (placeholder)",
        "type": "Manhwa",
        "status": "ongoing",
        "genres": ["Fantasi", "Petualangan", "Drama"],
        "year": 2026,
        "author": "Studio Placeholder",
        "synopsis": "Seorang penjaga menara menemukan bunga biru yang mekar setiap kali lonceng berbunyi. Judul, gambar, dan teks ini dibuat oleh generator sebagai data uji orisinal.",
        "permission": "Fixture orisinal buatan generator; bebas dipakai untuk pengujian.",
        "cover": "cover.png",
        "chapters": chapters,
    }
    (root / f"{name}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- seri bermasalah (HTTP-only) --------------------------------------
    name = "bermasalah"
    make_image(root / name / "cover.jpg", (600, 850), "BERMASALAH", "UJI GAGAL", seed=11)
    good = []
    for p in range(1, 4):
        rel = f"ok/{p:03d}.png"
        make_image(root / name / rel, (640, 960), "Seri Bermasalah", f"halaman {p}/3", seed=500 + p)
        good.append(rel)
    manifest = {
        "id": name,
        "title": "Seri Bermasalah",
        "type": "Manhwa",
        "status": "Berjalan",
        "genres": ["Uji"],
        "author": "Generator",
        "synopsis": "Seri uji dengan halaman yang sengaja gagal.",
        "cover": "cover.jpg",
        "chapters": [
            {"chapter": "Chapter 1", "title": "Semua baik", "date": "2026-09-01", "pages": good},
            {"chapter": "Chapter 2", "title": "Halaman 2 selalu 500", "date": "2026-09-02", "pages": [good[0], "/fail/500", good[2]]},
            {"chapter": "Chapter 3", "title": "Halaman 404", "date": "2026-09-03", "pages": [good[0], "/fail/404"]},
            {"chapter": "Chapter 4", "title": "Kembar A", "date": "2026-09-04", "pages": good[:2]},
            {"chapter": "Chapter 4", "title": "Kembar B", "date": "2026-09-05", "pages": good[:1]},
        ],
    }
    (root / f"{name}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


class _Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")
    hits: list = []

    def log_message(self, *args):  # sunyi
        return

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        type(self).hits.append(path)
        if path.startswith("/fail/"):
            try:
                code = int(path.split("/")[2])
            except (IndexError, ValueError):
                code = 500
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[1] == "manifest.json":
            target = self.root / f"{parts[0]}.json"
        else:
            target = (self.root / path.lstrip("/")).resolve()
            if self.root.resolve() not in target.parents:
                self.send_response(403)
                self.end_headers()
                return
        if not target.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = target.read_bytes()
        ctype = {".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(root: Path, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("FixtureHandler", (_Handler,), {"root": root, "hits": []})
    server = ThreadingHTTPServer((host, port), handler)
    server.hits = handler.hits  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".fixture-data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="generate ulang walau sudah ada")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.force or not (root / "menara-biru.json").exists():
        generate(root)
        print(f"Fixture orisinal dibuat di {root}")
    if args.generate_only:
        return
    server = make_server(root, args.host, args.port)
    print(f"Sumber fixture berjalan di http://{args.host}:{args.port}/  (manifest: /menara-biru/manifest.json, /bermasalah/manifest.json)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
