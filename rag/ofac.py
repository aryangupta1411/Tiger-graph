"""OFAC SDN screening tool (deterministic; never vector retrieval).

    from rag.ofac import ofac_screen
    ofac_screen("C13487")  -> {"query": "C13487", "exact": False, "best_score": 0, "matches": [],
                               "list_version": {...}, "ref": "ofac_screen(sdn.csv 2026-09-19)"}
    uv run python -m rag.ofac "AEROCARIBBEAN AIRLINES"      # CLI
    uv run python -m rag.fetch_corpus --ofac                 # download sdn.csv + alt.csv into data/ofac/

Sources (verified 2026-09-19, HTTP 200 with a browser UA; the legacy dat_spec.txt layout page is gone (404),
so the column layout below was read off the files themselves):
  https://www.treasury.gov/ofac/downloads/sdn.csv  5,695,725 B, 19,393 rows, no header, 12 fields:
      ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type, Tonnage, GRT, Vess_flag, Vess_owner, Remarks
  https://www.treasury.gov/ofac/downloads/alt.csv  1,064,228 B, 20,210 rows, no header, 5 fields:
      ent_num, alt_num, alt_type (aka | fka | nka), alt_name, alt_remarks
  The literal `-0-` means null.
Matching: names are upper-cased and stripped of punctuation; exact match first, then rapidfuzz
token_set_ratio >= 90 (research/gap4 recommendation). Very short or purely numeric queries (dataset ids
such as C13487) cannot fuzzy-match a name and return an empty, honest "no match".
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import config

SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
ALT_URL = "https://www.treasury.gov/ofac/downloads/alt.csv"
THRESHOLD = 90
_NORM = re.compile(r"[^A-Z0-9 ]+")


def normalise(name: str) -> str:
    return " ".join(_NORM.sub(" ", (name or "").upper()).split())


def _null(v: str) -> str:
    v = (v or "").strip()
    return "" if v == "-0-" else v


class OfacIndex:
    def __init__(self, sdn_path: Path, alt_path: Path | None = None):
        self.sdn_path, self.alt_path = Path(sdn_path), Path(alt_path) if alt_path else None
        self.entries: dict[int, dict] = {}
        self.names: list[tuple[str, int, str]] = []   # (normalised name, ent_num, source)
        with self.sdn_path.open(newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) < 4 or not row[0].strip().isdigit():
                    continue
                ent = int(row[0])
                self.entries[ent] = {"ent_num": ent, "name": _null(row[1]), "type": _null(row[2]),
                                     "program": _null(row[3]), "remarks": _null(row[11]) if len(row) > 11 else ""}
                self.names.append((normalise(row[1]), ent, "sdn"))
        if self.alt_path and self.alt_path.exists():
            with self.alt_path.open(newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.reader(f):
                    if len(row) < 4 or not row[0].strip().isdigit():
                        continue
                    self.names.append((normalise(row[3]), int(row[0]), f"alt:{_null(row[2]) or 'aka'}"))
        self._exact: dict[str, list[int]] = {}
        for i, (n, _, _) in enumerate(self.names):
            self._exact.setdefault(n, []).append(i)
        self.version = {
            "sdn_sha256": _sha(self.sdn_path), "alt_sha256": _sha(self.alt_path) if self.alt_path and self.alt_path.exists() else "",
            "sdn_rows": len(self.entries), "name_rows": len(self.names),
            "fetched_at": datetime.fromtimestamp(self.sdn_path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d"),
        }

    def screen(self, name: str, threshold: int = THRESHOLD, limit: int = 5) -> dict:
        q = normalise(name)
        ref = f"ofac_screen(sdn.csv {self.version['fetched_at']})"
        base = {"query": name, "normalised": q, "exact": False, "best_score": 0, "matches": [],
                "threshold": threshold, "list_version": self.version, "ref": ref}
        if len(q) < 4 or not re.search(r"[A-Z]{3}", q):
            base["note"] = "query too short or non-alphabetic for name matching; no match"
            return base
        hits: dict[int, dict] = {}
        for i in self._exact.get(q, []):
            n, ent, src = self.names[i]
            hits[ent] = {**self.entries[ent], "matched_name": n, "score": 100, "source": src}
        if hits:
            base["exact"] = True
        from rapidfuzz import fuzz, process

        for n, score, i in process.extract(q, [x[0] for x in self.names], scorer=fuzz.token_set_ratio,
                                           limit=limit * 4, score_cutoff=threshold):
            _, ent, src = self.names[i]
            if ent not in hits or hits[ent]["score"] < score:
                hits[ent] = {**self.entries[ent], "matched_name": n, "score": int(score), "source": src}
        matches = sorted(hits.values(), key=lambda h: (-h["score"], h["name"]))[:limit]
        base["matches"] = matches
        base["best_score"] = matches[0]["score"] if matches else 0
        return base


def _sha(p: Path | None) -> str:
    if not p or not p.exists():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


_INDEX: OfacIndex | None = None


def get_index() -> OfacIndex:
    global _INDEX
    if _INDEX is None:
        sdn = config.OFAC_DIR / "sdn.csv"
        alt = config.OFAC_DIR / "alt.csv"
        if not sdn.exists():
            raise FileNotFoundError(f"{sdn} missing: run `python -m rag.fetch_corpus --ofac`")
        _INDEX = OfacIndex(sdn, alt if alt.exists() else None)
    return _INDEX


def ofac_screen(name: str) -> dict:
    """agent/tools_local.ofac_screen(name) delegates here. Evidence item: source=external, ref=result['ref']."""
    return get_index().screen(name)


def fetch_ofac(dest: Path, user_agent: str) -> dict:
    from .fetch_corpus import download, sha256_of

    dest.mkdir(parents=True, exist_ok=True)
    out = {}
    for url, name in ((SDN_URL, "sdn.csv"), (ALT_URL, "alt.csv")):
        p = dest / name
        ok = download(url, p, user_agent)
        out[name] = {"url": url, "ok": ok, "sha256": sha256_of(p) if ok else "", "bytes": p.stat().st_size if ok else 0,
                     "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (dest / "manifest.json").write_text(json.dumps(out, indent=2))
    return out


def main(argv: list[str] | None = None) -> int:
    from ops.console import Col, Table, detail, header, ok, step, summary, warn

    argv = argv if argv is not None else sys.argv[1:]
    names = list(argv) or ["C13487"]
    idx = get_index()
    v = idx.version
    header("rag.ofac",
           "deterministic OFAC SDN screening (exact match, then rapidfuzz token_set_ratio)",
           {"sdn.csv": str(idx.sdn_path), "alt.csv": str(idx.alt_path) if idx.alt_path else "(not loaded)",
            "entities": f"{v['sdn_rows']:,}", "names indexed": f"{v['name_rows']:,}",
            "list fetched": v["fetched_at"], "sdn sha256": v["sdn_sha256"][:16],
            "threshold": THRESHOLD, "queries": len(names)})
    hits = 0
    for name in names:
        r = ofac_screen(name)
        step(f"screening {name!r}")
        detail(f"normalised {r['normalised']!r} | exact={r['exact']} | best score {r['best_score']} "
               f"| threshold {r['threshold']} | ref {r['ref']}")
        if r.get("note"):
            warn(r["note"])
        t = Table(
            Col("score", align="right", width=6),
            Col("ent_num", align="right", width=8),
            Col("name", max_width=36),
            Col("type", max_width=10),
            Col("program", max_width=14),
            Col("matched via", max_width=12),
            title=f"matches for {name!r}",
        )
        for m in r["matches"]:
            t.add_row(m["score"], m["ent_num"], m["name"], m["type"], m["program"], m["source"],
                      style="red" if m["score"] >= 100 else "yellow")
        if r["matches"]:
            t.print()
            hits += 1
            warn(f"{name}: {len(r['matches'])} SDN match(es), best score {r['best_score']}")
        else:
            ok(f"{name}: no OFAC SDN match (screened against {v['name_rows']:,} names)")
    summary("rag.ofac complete",
            {"queries": len(names), "with matches": hits, "clean": len(names) - hits,
             "list version": f"sdn.csv {v['fetched_at']} ({v['sdn_rows']:,} entities)"},
            status="warn" if hits else "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
