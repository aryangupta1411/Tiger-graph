"""Chunk the corpus into PolicyChunk rows.

    uv run python -m rag.chunk                       # all included manifest docs + README policy/patterns
    uv run python -m rag.chunk --readme-only         # just policy#* and pattern#* chunks
    uv run python -m rag.chunk --readme data/raw/README.md --text-dir data/corpus/text --out data/out

Outputs (data/out/):
  policy_chunks.jsonl   one object per chunk: {id, doc_id, section, page, page_end, kind, text, url,
                        sha256, n_tokens, about:[FraudPattern ids]}
  policy_chunk.csv      headerless: id,doc_id,section,page,kind,text            (LOAD -> PolicyChunk)
  document.csv          headerless: id,title,url,sha256,kind                    (LOAD -> Document)
  chunk_of.csv          headerless: chunk_id,doc_id                             (LOAD -> CHUNK_OF)
  about.csv             headerless: chunk_id,pattern_id                         (LOAD -> ABOUT)
  policy_chunk_embed.jsonl  {id, text} - the input for rag.embed (text = "section: <heading>\\n<body>")

NOT written here: data/out/fraud_pattern.csv. The FraudPattern vertex file is owned by the ETL
(etl/export_graph_csvs.py FRAUD_PATTERNS, TSV). The
`about` rows target those seven ids; chunk_readme() still returns the README pattern descriptions
for callers that want them, but nothing here writes them to disk.

Chunk sizes: heading-based sections, 500-800 token target (tokens ~= words * 1.3, Voyage's own count is
recorded by rag.embed), 12 % sentence-aligned overlap between consecutive chunks of one section.
Policy rules R1-R10 and sections 3a/3b/4/5/6/7 and the five README patterns are one chunk each
(ids policy#R1 ... pattern#card_testing), never split or merged.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ops.console import Col, Table, fail, header, ok, step, summary, warn

from . import config
from .fetch_corpus import load_manifest, sha256_of

TARGET_MIN, TARGET_MAX, OVERLAP = 500, 800, 0.12

RUNNING_HEADERS = [
    r"^\s*Financial Crimes Enforcement Network\s*$",
    r"^\s*Board of Governors of the Federal Reserve System\s*$",
    r"^\s*Federal Deposit Insurance Corporation\s*$",
    r"^\s*National Credit Union Administration\s*$",
    r"^\s*Office of the Comptroller of the Currency\s*$",
    r"^\s*\d{1,3}\s*$",  # bare page numbers
]

# Which FraudPattern a policy rule / README pattern chunk is ABOUT (drives the ABOUT edges and
# FraudPattern.rule_refs). Rules that apply to every pattern are mapped to none of them.
RULE_ABOUT = {
    "R1": ["card_not_present_fraud", "card_not_present_new_device", "out_of_region_use"],
    "R2": ["card_not_present_fraud", "card_not_present_new_device", "out_of_region_use", "account_takeover"],
    "R3": ["card_not_present_fraud", "card_not_present_new_device", "out_of_region_use", "account_takeover"],
    "R4": ["card_not_present_fraud", "card_not_present_new_device"],
    "R5": ["card_testing"],
    "R6": ["undocumented", "card_not_present_new_device", "account_takeover"],
    "R7": ["none"],
    "R8": [],
    "R9": ["undocumented"],
    "R10": ["account_takeover"],
    "3a": ["undocumented"],
    "3b": [],
    "4": [],
    "5": [],
    "6": [],
    "7": [],
    "0": [],
    "1-actions": [],
    "2-routing": [],
    "sar-format": [],
}
PATTERN_KEYS = {
    "1": "card_testing",
    "2": "card_not_present_fraud",
    "3": "card_not_present_new_device",
    "4": "out_of_region_use",
    "5": "account_takeover",
}


@dataclass
class Chunk:
    id: str
    doc_id: str
    section: str
    page: int
    page_end: int
    kind: str
    text: str
    url: str
    sha256: str
    n_tokens: int
    about: list[str] = field(default_factory=list)


def n_tokens(text: str) -> int:
    return int(round(len(text.split()) * 1.3))


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text.strip()) if s]


def slug(s: str, n: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n].rstrip("-") or "sec"


# ----------------------------------------------------------------------------- regulatory docs

def _clean_lines(text: str, extra_strip: list[str] | None = None) -> list[tuple[int, str]]:
    """Return (page, line) pairs with running headers removed; pages split on form feed."""
    pats = [re.compile(p) for p in RUNNING_HEADERS + (extra_strip or [])]
    out: list[tuple[int, str]] = []
    for pno, page in enumerate(text.split("\f"), start=1):
        for line in page.splitlines():
            if any(p.match(line) for p in pats):
                continue
            out.append((pno, line.rstrip()))
    return out


def sections_by_heading(lines: list[tuple[int, str]], heading_re: str | None, page_base: int = 1
                        ) -> list[tuple[str, int, int, str]]:
    """Split into (heading, page_start, page_end, body) using the doc's heading regex, or a generic
    heuristic (short capitalised line with no terminal period, followed by text)."""
    hre = re.compile(heading_re) if heading_re else None
    secs: list[tuple[str, int, int, list[str]]] = []
    cur_title, cur_start, cur_end, cur_body = "Preamble", page_base, page_base, []
    for i, (p, line) in enumerate(lines):
        page = p + page_base - 1
        is_heading = False
        s = line.strip()
        if s:
            if hre is not None:
                is_heading = bool(hre.match(s))
            else:
                nxt = lines[i + 1][1].strip() if i + 1 < len(lines) else ""
                is_heading = (4 <= len(s) <= 90 and len(s.split()) <= 12 and s[0].isupper()
                              and not s.endswith((".", ",", ";")) and nxt != "" and not s.isupper())
        if is_heading:
            if "".join(cur_body).strip():
                secs.append((cur_title, cur_start, cur_end, cur_body))
            cur_title, cur_start, cur_end, cur_body = s, page, page, []
        else:
            cur_body.append(line)
            cur_end = page
    if "".join(cur_body).strip():
        secs.append((cur_title, cur_start, cur_end, cur_body))
    return [(t, a, b, _dehyphenate("\n".join(body))) for t, a, b, body in secs]


def _dehyphenate(s: str) -> str:
    s = re.sub(r"(\w)-\n\s*(\w)", r"\1\2", s)        # line-break hyphenation
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def paragraphs(body: str) -> list[str]:
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", body)]
    return [p for p in paras if p]


def window_chunks(paras: list[str], tmin: int, tmax: int, overlap: float = OVERLAP) -> list[str]:
    """Greedy paragraph packing into [tmin, tmax] tokens with sentence-aligned overlap."""
    units: list[str] = []
    unit_max = tmax - int(tmax * overlap)   # leave room for the carried-over tail
    for p in paras:
        if n_tokens(p) > unit_max:  # a single huge paragraph: split by sentences
            buf: list[str] = []
            for s in sentences(p):
                if n_tokens(" ".join(buf + [s])) > unit_max and buf:
                    units.append(" ".join(buf))
                    buf = []
                buf.append(s)
            if buf:
                units.append(" ".join(buf))
        else:
            units.append(p)
    chunks: list[str] = []
    cur: list[str] = []
    for u in units:
        if cur and n_tokens(" ".join(cur + [u])) > tmax:
            chunks.append(" ".join(cur))
            # overlap: carry the tail sentences (~12 % of tokens) into the next chunk
            tail: list[str] = []
            budget = int(n_tokens(chunks[-1]) * overlap)
            for s in reversed(sentences(chunks[-1])):
                if n_tokens(" ".join(tail + [s])) > budget:
                    break
                tail.insert(0, s)
            cur = tail[:] if n_tokens(" ".join(tail + [u])) <= tmax else []
        cur.append(u)
    if cur:
        chunks.append(" ".join(cur))
    # merge a runt last chunk into the previous one when it is far below tmin
    if len(chunks) >= 2 and n_tokens(chunks[-1]) < tmin // 3 and n_tokens(chunks[-2] + " " + chunks[-1]) <= tmax * 1.25:
        chunks[-2:] = [chunks[-2] + " " + chunks[-1]]
    return chunks


def chunk_regulatory(doc: dict, text: str) -> list[Chunk]:
    kind = doc.get("kind", "regulation")
    tmin = int(doc.get("min_tokens", TARGET_MIN))
    tmax = int(doc.get("max_tokens", TARGET_MAX))
    page_base = (doc.get("pages") or [1])[0]
    lines = _clean_lines(text, doc.get("strip_patterns"))
    secs = sections_by_heading(lines, doc.get("heading_regex"), page_base)
    # merge sections that are too small to stand alone (below tmin/2) into the next one
    merged: list[tuple[str, int, int, str]] = []
    carry: tuple[str, int, int, str] | None = None
    for t, a, b, body in secs:
        if carry:
            t = carry[0] if carry[3] else t
            a = carry[1]
            body = (carry[3] + "\n\n" + body).strip()
            carry = None
        if n_tokens(body) < tmin // 2:
            carry = (t, a, b, body)
            continue
        merged.append((t, a, b, body))
    if carry:
        if merged:
            t, a, b, body = merged[-1]
            merged[-1] = (t, a, carry[2], body + "\n\n" + carry[3])
        else:
            merged.append(carry)
    out: list[Chunk] = []
    for t, a, b, body in merged:
        pieces = window_chunks(paragraphs(body), tmin, tmax)
        for i, piece in enumerate(pieces):
            cid = f"{doc['doc_id']}#p{a:02d}-{slug(t)}" + (f"-{i + 1}" if len(pieces) > 1 else "")
            out.append(Chunk(cid, doc["doc_id"], t, a, b, kind, piece, doc.get("url", ""),
                             doc.get("sha256", ""), n_tokens(piece), list(doc.get("about", []))))
    return out


# ----------------------------------------------------------------------------- README policy + patterns

def _section(md: str, start_re: str, end_re: str) -> str:
    m = re.search(start_re, md, re.M)
    if not m:
        return ""
    rest = md[m.end():]
    e = re.search(end_re, rest, re.M)
    return rest[: e.start()] if e else rest


def chunk_readme(md: str, readme_url: str = "README.md", sha: str = "") -> tuple[list[Chunk], dict[str, str]]:
    """Return (chunks, pattern_descriptions). Ids: policy#R1..R10, policy#0, policy#1-actions,
    policy#2-routing, policy#3a, policy#3b, policy#4..7, policy#sar-format, pattern#<id>."""
    chunks: list[Chunk] = []
    pol = _section(md, r"^# Fraud Policy\s*$", r"^# Answer Format\s*$")
    version_line = "Fraud Policy v1.0 (HHGOA dataset README)."

    def add(cid: str, section: str, text: str, kind: str, about: list[str]) -> None:
        text = text.strip()
        chunks.append(Chunk(cid, "fraud_policy" if kind == "policy" else "known_patterns", section, 0, 0,
                            kind, text, readme_url, sha, n_tokens(text), about))

    # numbered sections "### N. Title" / "### 3a. Title"
    sec_re = re.compile(r"^### (\d+[ab]?)\. (.+?)\s*$", re.M)
    heads = list(sec_re.finditer(pol))
    for i, h in enumerate(heads):
        num, title = h.group(1), h.group(2)
        body = pol[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(pol)].strip()
        if num == "3":  # rules: one chunk per rule
            for rm in re.finditer(r"^\*\*(R\d+)\. (.+?)\*\*\s*(.+?)(?=^\*\*R\d+\.|\Z)", body, re.M | re.S):
                rid, rtitle, rtext = rm.group(1), rm.group(2), " ".join(rm.group(3).split())
                add(f"policy#{rid}", f"3. Rules - {rid}. {rtitle}",
                    f"{version_line} Rule {rid}: {rtitle}\n{rtext}", "policy", RULE_ABOUT.get(rid, []))
            continue
        cid = {"1": "1-actions", "2": "2-routing"}.get(num, num)
        add(f"policy#{cid}", f"{num}. {title}", f"{version_line} Section {num}: {title}\n{body}",
            "policy", RULE_ABOUT.get(cid, []))
    # the SAR part of the Answer Format (what a narrative must contain) - grounding for sar.py
    ans = _section(md, r"^# Answer Format\s*$", r"^# The 20 Cases\s*$")
    sar_part = _section(ans, r"^#### Part 2: `sar`\s*$", r"^#### Part 3:")
    if sar_part:
        add("policy#sar-format", "Answer Format - Part 2: sar",
            "HHGOA answer format, Part 2 (the suspicious activity report fields).\n" + sar_part, "policy", [])
    # README patterns
    pat = _section(md, r"^## The five known fraud patterns\s*$", r"^## Regulatory references\s*$")
    descriptions: dict[str, str] = {}
    intro = " ".join(pat.split("**1.")[0].split())
    for pm in re.finditer(r"^\*\*(\d)\. (.+?)\*\*\s*(.+?)$", pat, re.M):
        pid = PATTERN_KEYS[pm.group(1)]
        text = f"Known fraud pattern {pm.group(1)} ({pid}): {pm.group(2)} {' '.join(pm.group(3).split())}"
        descriptions[pid] = " ".join(pm.group(3).split())
        add(f"pattern#{pid}", f"Known pattern {pm.group(1)}: {pm.group(2)}", text, "pattern", [pid])
    undoc = ("Undocumented patterns (undocumented): " + intro +
             " The answer format says: use `undocumented` when the evidence shows abuse that fits none of the five "
             "known patterns, and describe it in `pattern_description` (two or three sentences on what the pattern is, "
             "who it affects, and how you found it). Policy R9 applies: CREATE_CASE, FILE_REPORT and ESCALATE_TO_ANALYST "
             "when the activity shows coordinated or repeated abuse across customers; do not force it into a known category. "
             "The closed cases contain two such shapes: purchases from one unusual device profile behind an anonymous proxy "
             "shared by many cards in one month, and four online purchases within forty minutes each just under $500.")
    descriptions["undocumented"] = ("Activity that fits none of the five known patterns but shows coordinated or repeated "
                                    "abuse across customers; described in the agent's own words.")
    descriptions["none"] = "No fraud: the alert was cleared as legitimate."
    add("pattern#undocumented", "Undocumented patterns", undoc, "pattern", ["undocumented"])
    return chunks, descriptions


# ----------------------------------------------------------------------------- outputs

def write_outputs(chunks: list[Chunk], docs: list[dict], out_dir: Path) -> None:
    """policy_chunks.jsonl, policy_chunk_embed.jsonl, policy_chunk.csv, document.csv, chunk_of.csv, about.csv.
    fraud_pattern.csv is deliberately NOT written (ETL-owned, see the module docstring)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "policy_chunks.jsonl").open("w") as f:
        for c in chunks:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    with (out_dir / "policy_chunk_embed.jsonl").open("w") as f:
        for c in chunks:
            f.write(json.dumps({"id": c.id, "text": f"section: {c.section}\n{c.text}"}, ensure_ascii=False) + "\n")
    q = dict(quoting=csv.QUOTE_ALL, lineterminator="\n")
    with (out_dir / "policy_chunk.csv").open("w", newline="") as f:
        w = csv.writer(f, **q)
        for c in chunks:
            w.writerow([c.id, c.doc_id, c.section, c.page, c.kind, c.text.replace("\n", " ")])
    with (out_dir / "document.csv").open("w", newline="") as f:
        w = csv.writer(f, **q)
        seen = set()
        for d in docs:
            if d.get("include") and d["doc_id"] not in seen:
                seen.add(d["doc_id"])
                w.writerow([d["doc_id"], d.get("title", ""), d.get("url", ""), d.get("sha256", ""), d.get("kind", "")])
    with (out_dir / "chunk_of.csv").open("w", newline="") as f:
        w = csv.writer(f, **q)
        for c in chunks:
            w.writerow([c.id, c.doc_id])
    with (out_dir / "about.csv").open("w", newline="") as f:
        w = csv.writer(f, **q)
        for c in chunks:
            for p in c.about:
                assert p in config.PATTERNS, f"ABOUT edge to unknown FraudPattern {p!r}"
                w.writerow([c.id, p])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--readme", type=Path, default=config.README_PATH)
    ap.add_argument("--text-dir", type=Path, default=config.CORPUS_TEXT)
    ap.add_argument("--out", type=Path, default=config.OUT_DIR)
    ap.add_argument("--manifest", type=Path, default=config.CORPUS_DIR / "manifest.json")
    ap.add_argument("--readme-only", action="store_true")
    args = ap.parse_args(argv)

    header("rag.chunk",
           "README policy/patterns + regulatory corpus -> PolicyChunk rows",
           {"readme": str(args.readme), "text dir": str(args.text_dir), "manifest": str(args.manifest),
            "out": str(args.out), "scope": "README only" if args.readme_only else "README + regulations",
            "target tokens": f"{TARGET_MIN}-{TARGET_MAX}", "overlap": f"{OVERLAP:.0%}"})

    manifest = load_manifest(args.manifest)
    docs = manifest["documents"]
    md = args.readme.read_text()
    readme_sha = sha256_of(args.readme)
    for d in docs:
        if d.get("media") == "markdown":
            d["sha256"], d["sha256_status"] = readme_sha, "verified"
    step(f"chunking {args.readme.name} (one chunk per rule R1-R10, per section and per known pattern)")
    chunks, _descriptions = chunk_readme(md, "README.md#fraud-policy", readme_sha)   # descriptions: ETL-owned
    ok(f"{len(chunks)} README chunks (sha256 {readme_sha[:12]})")

    t = Table(
        Col("doc_id", max_width=24),
        Col("kind", max_width=12),
        Col("chunks", align="right", width=7),
        Col("tokens min", align="right", width=10),
        Col("median", align="right", width=7),
        Col("max", align="right", width=6),
        Col("status", max_width=26),
        title="corpus documents",
    )
    missing = []
    if not args.readme_only:
        for d in docs:
            if not d.get("include") or d.get("media") == "markdown":
                continue
            if d.get("media") == "inline":
                text = d["text"].strip()
                chunks.append(Chunk(f"{d['doc_id']}#p00-note", d["doc_id"], d["title"], 0, 0, d["kind"], text,
                                    d.get("url", ""), "", n_tokens(text), []))
                t.add_row(d["doc_id"], d.get("kind", ""), 1, n_tokens(text), n_tokens(text), n_tokens(text),
                          "inline note")
                continue
            txt = args.text_dir / f"{d['doc_id']}.txt"
            if not txt.exists():
                missing.append(d["doc_id"])
                t.add_row(d["doc_id"], d.get("kind", ""), 0, "-", "-", "-",
                          "no extracted text; skipped", style="yellow")
                continue
            got = chunk_regulatory(d, txt.read_text(errors="replace"))
            toks = sorted(c.n_tokens for c in got)
            t.add_row(d["doc_id"], d.get("kind", ""), len(got),
                      toks[0] if toks else "-", toks[len(toks) // 2] if toks else "-", toks[-1] if toks else "-",
                      "ok" if toks else "no chunks produced", style=None if toks else "yellow")
            chunks.extend(got)
    t.print()
    for doc_id in missing:
        warn(f"{doc_id}: no extracted text in {args.text_dir} - run `python -m rag.fetch_corpus`")

    write_outputs(chunks, docs, args.out)
    by_kind: dict[str, int] = {}
    for c in chunks:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    k = Table(Col("kind", max_width=14), Col("chunks", align="right", width=8),
              Col("tokens total", align="right", width=13), title="chunks written by kind")
    for kind in sorted(by_kind):
        k.add_row(kind, f"{by_kind[kind]:,}", f"{sum(c.n_tokens for c in chunks if c.kind == kind):,}")
    k.add_row("TOTAL", f"{len(chunks):,}", f"{sum(c.n_tokens for c in chunks):,}")
    k.print()
    if not chunks:
        fail("no chunks were produced")
    else:
        ok(f"{len(chunks):,} chunks -> {args.out}/policy_chunks.jsonl (+ policy_chunk.csv, document.csv, "
           "chunk_of.csv, about.csv, policy_chunk_embed.jsonl)")
    summary("rag.chunk complete",
            {"chunks": f"{len(chunks):,}", "documents chunked": len(t) - len(missing),
             "documents missing text": len(missing), "out": str(args.out),
             "next": "python -m rag.embed --input policy_chunk_embed.jsonl --out vec_PolicyChunk.psv"},
            status="warn" if missing else "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
