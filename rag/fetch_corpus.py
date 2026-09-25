"""Fetch the regulatory corpus listed in data/corpus/manifest.json and extract text.

    uv run python -m rag.fetch_corpus            # download what curl can reach, extract text
    uv run python -m rag.fetch_corpus --verify   # recompute sha256 for every raw file present, update manifest
    uv run python -m rag.fetch_corpus --only sar_guidance_narrative

Rules (from research/gap4):
  * FinCEN PDFs and the OFAC CSVs download with a browser User-Agent.
  * Every FATF URL and both FFIEC pages return 403 to non-browser clients: the script prints the
    exact URL to open in a browser and the file name to save under data/corpus/raw/, then treats the
    file as present on the next run.
  * Text extraction: `pdftotext -layout` (poppler) keeps form-feed page breaks, which chunk.py uses for
    page provenance; pypdf is the fallback when pdftotext is missing. HTML is reduced to headings and
    paragraphs with the standard-library parser (no external deps).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from ops.console import Col, Table, detail, header, ok, step, summary, warn

from . import config

MANIFEST_PATH = config.CORPUS_DIR / "manifest.json"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    return json.loads(path.read_text())


def save_manifest(m: dict, path: Path = MANIFEST_PATH) -> None:
    path.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")


def download(url: str, dest: Path, user_agent: str, retries: int = 3) -> bool:
    """Download with a browser UA, following redirects. Returns True on success."""
    import requests  # local import so tests without network can import the module

    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=120, allow_redirects=True, stream=True)
            if r.status_code == 200:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as f:
                    for block in r.iter_content(1 << 16):
                        f.write(block)
                return True
            warn(f"HTTP {r.status_code} for {url}")
            if r.status_code in (403, 404):
                return False
        except Exception as e:  # noqa: BLE001 - report and retry
            warn(f"attempt {attempt}/{retries} failed: {type(e).__name__}")
            detail(str(e)[:160])
        time.sleep(2 * attempt)
    return False


def pdf_to_text(pdf: Path, txt: Path, pages: list[int] | None) -> None:
    """pdftotext -layout with form feeds between pages; pypdf fallback (also emits \\f)."""
    txt.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("pdftotext"):
        cmd = ["pdftotext", "-layout"]
        if pages:
            cmd += ["-f", str(pages[0]), "-l", str(pages[1])]
        cmd += [str(pdf), str(txt)]
        subprocess.run(cmd, check=True)
        return
    from pypdf import PdfReader  # fallback

    reader = PdfReader(str(pdf))
    first, last = (pages[0], pages[1]) if pages else (1, len(reader.pages))
    parts = []
    for i in range(first - 1, min(last, len(reader.pages))):
        parts.append(reader.pages[i].extract_text() or "")
    txt.write_text("\f".join(parts))


class _HtmlText(HTMLParser):
    """Keep h1-h4 as heading lines (marked with '## ') and p/li as paragraphs; drop nav/script/style."""

    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg"}
    BLOCK = {"p", "li", "div", "section", "article", "td", "th", "tr", "br", "table"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip = 0
        self._heading: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("h1", "h2", "h3", "h4"):
            self._flush()
            self._heading = tag
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("h1", "h2", "h3", "h4"):
            text = " ".join("".join(self._buf).split())
            self._buf = []
            if text:
                self.out.append("\n## " + text + "\n")
            self._heading = None
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text:
            self.out.append(text + "\n")

    def text(self) -> str:
        self._flush()
        return "".join(self.out)


def html_to_text(html_path: Path, txt: Path) -> None:
    p = _HtmlText()
    p.feed(html_path.read_text(errors="replace"))
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text(p.text())


def process_document(doc: dict, ua: str, verify_only: bool = False, report: list | None = None) -> dict:
    """Fetch (if reachable), hash, and extract text for one manifest entry. Returns the updated entry.

    `report` (optional) collects one (doc_id, media, status, sha, bytes, text) row per document for the
    caller's table; it is display only and never touches the manifest that is written back to disk.
    """
    def row(status: str, sha: str = "", nbytes: str = "", text: str = "") -> None:
        if report is not None:
            report.append((doc.get("doc_id", "?"), doc.get("media", ""), status, sha, nbytes, text))

    if not doc.get("include"):
        row("not included")
        return doc
    media = doc.get("media")
    if media in ("inline", "markdown"):
        row("handled by rag.chunk")
        return doc  # README and inline notes are handled by chunk.py directly
    raw = config.CORPUS_RAW / doc["local_name"]
    if not raw.exists() and not verify_only:
        url = doc.get("pdf_url") or doc["url"]
        if doc.get("browser_only"):
            warn(f"{doc['doc_id']}: 403s for non-browser clients")
            detail(f"open {url} in a browser and save it as {raw}")
            row("browser download needed")
            return doc
        step(f"{doc['doc_id']}: downloading {url}")
        if not download(url, raw, ua):
            warn(f"{doc['doc_id']}: download failed; if it 403s, save it by hand to {raw}")
            row("download failed")
            return doc
    if not raw.exists():
        row("missing" if verify_only else "not downloaded")
        return doc
    digest = sha256_of(raw)
    if doc.get("sha256_status") == "verified" and doc.get("sha256") not in ("", "placeholder", digest):
        warn(f"{doc['doc_id']}: sha256 changed - manifest {doc['sha256'][:12]} != file {digest[:12]}")
    doc["sha256"] = digest
    doc["sha256_status"] = "verified"
    doc["bytes"] = raw.stat().st_size
    doc["fetched_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    txt = config.CORPUS_TEXT / f"{doc['doc_id']}.txt"
    if media == "pdf":
        pdf_to_text(raw, txt, doc.get("pages"))
    elif media == "html":
        html_to_text(raw, txt)
    doc["text_path"] = str(txt.relative_to(config.REPO_ROOT)) if txt.is_relative_to(config.REPO_ROOT) else str(txt)
    row("ok", digest[:12], f"{doc['bytes']:,}", txt.name)
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true", help="only hash/extract files already present")
    ap.add_argument("--only", help="doc_id to process")
    ap.add_argument("--ofac", action="store_true", help="also fetch sdn.csv and alt.csv (see rag.ofac)")
    args = ap.parse_args(argv)
    config.ensure_dirs()
    m = load_manifest()
    header("rag.fetch_corpus",
           "hash the regulatory corpus and extract its text (pdftotext -layout keeps page breaks)",
           {"manifest": str(MANIFEST_PATH), "raw": str(config.CORPUS_RAW), "text": str(config.CORPUS_TEXT),
            "mode": "verify only (no download)" if args.verify else "download what curl can reach",
            "only": args.only or "every document", "ofac": "yes" if args.ofac else "no",
            "documents": len(m["documents"])})
    rows: list = []
    for i, doc in enumerate(m["documents"]):
        if args.only and doc["doc_id"] != args.only:
            continue
        m["documents"][i] = process_document(doc, m["user_agent"], verify_only=args.verify, report=rows)
    save_manifest(m)
    t = Table(
        Col("doc_id", max_width=24),
        Col("media", max_width=9),
        Col("status", max_width=24),
        Col("sha256", width=12),
        Col("bytes", align="right", width=12),
        Col("extracted text", max_width=26),
        title="corpus manifest",
    )
    for doc_id, media, status, sha, nbytes, text in rows:
        t.add_row(doc_id, media, status, sha, nbytes, text,
                  style=None if status in ("ok", "handled by rag.chunk", "not included") else "yellow")
    t.print()
    ok(f"manifest rewritten -> {MANIFEST_PATH}")
    if args.ofac:
        from .ofac import fetch_ofac

        step("fetching the OFAC SDN list (sdn.csv + alt.csv)")
        res = fetch_ofac(config.OFAC_DIR, m["user_agent"])
        o = Table(Col("file", width=10), Col("ok", width=5, align="center"),
                  Col("bytes", align="right", width=12), Col("sha256", width=12), title="data/ofac")
        for name, r in res.items():
            o.add_row(name, "yes" if r["ok"] else "no", f"{r['bytes']:,}", r["sha256"][:12],
                      style=None if r["ok"] else "red")
        o.print()
    missing = [d["doc_id"] for d in m["documents"] if d.get("include") and d.get("media") in ("pdf", "html")
               and not (config.CORPUS_RAW / d["local_name"]).exists()]
    for doc_id in missing:
        warn(f"{doc_id}: still missing, needs a browser download")
    n_ok = sum(1 for r in rows if r[2] == "ok")
    summary("rag.fetch_corpus complete",
            {"documents seen": len(rows), "hashed + extracted": n_ok,
             "missing (browser download)": len(missing), "manifest": str(MANIFEST_PATH),
             "next": "python -m rag.chunk"},
            status="warn" if missing else "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
