"""etl/parse_closed_cases.py — template-parse the 5,565 closed-case notes into ClosedCase attributes
(contracts/schema.md ClosedCase: template_id, note_region, note_device, note_evidence_type, embed_text)
and the INVOLVES / ON_CARD / CONNECTED_TO / MATCHES edge rows.

    python -m etl.parse_closed_cases data/hhgoa.duckdb

Creates tables:
    closed_case_parsed   one row per ClosedCase (all attributes except the vector)
    closed_case_txn      (case_id, TransactionID)         -> INVOLVES
    closed_case_conn     (case_id, card_id)               -> CONNECTED_TO
ON_CARD comes from closed_case_parsed.card_id, MATCHES from closed_case_parsed.pattern (cleared -> 'none').

The six note templates (verified on all 5,565 rows):
    fraud_reported     4,656  "cardholder X reported unrecognized activity ... <pattern sentence> [device sentence]"
    cleared_travel       716  "model scored ... Cardholder confirmed travel to the billing region in question."
    cleared_new_phone    158  "... Cardholder confirmed the purchase from a new phone. Device added to profile."
    cleared_amount        26  "... Cardholder confirmed the purchase. Amount unusual ... consistent with their stated intent."
    undoc_ring             4  "... came from a Samsung SM-G935F ... behind an anonymous proxy ..."
    undoc_burst            5  "... four online purchases within forty minutes, each just under $500 ..."
embed_text = "pattern: X | outcome: Y | device: Z | region: R | " + note with case ids, customer/card ids,
transaction counts, dates, amounts and scores masked (<CASE>, <ID>, <N>, <DATE>, <AMT>, <SCORE>); browser/OS
versions are kept because they carry device signal.  Requires tables cc, txc (etl/features.py).
"""
from __future__ import annotations

import re
import shutil
import sys
import textwrap

import duckdb

from ops.console import Col, Table, detail, header, ok, rule, step, summary, warn

# The six note templates and their documented row counts (verified on all 5,565 rows; see the docstring).
EXPECTED_TEMPLATES = {"fraud_reported": 4_656, "cleared_travel": 716, "cleared_new_phone": 158,
                      "cleared_amount": 26, "undoc_ring": 4, "undoc_burst": 5}

RE_FRAUD = re.compile(
    r"^Case (CC-\d{4}): cardholder (C\d{5}) reported unrecognized activity on card (C\d{5}-K\d)\. "
    r"(\d+) transaction\(s\) between (\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2}) totaling \$([\d,]+\.\d{2}) "
    r"were confirmed fraudulent\. (?P<body>.*?) Card blocked and reissued\. Customer reimbursed\.$"
)
RE_DEVICE = re.compile(r"Online transactions came from a (?P<dev>.+?) on (?P<browser>.+?)\.$")
RE_CLEARED = re.compile(r"^Case (CC-\d{4}): model scored a \$([\d,]+\.\d{2}) transaction at (0\.\d{2})\. (?P<body>.*?) Alert cleared\.$")
RE_RING = re.compile(
    r"^Case (CC-\d{4}): cardholder (C\d{5}) reported (\d+) online purchase\(s\) they did not make\. "
    r"The purchases came from a (?P<dev>.+?) behind an anonymous proxy, a device never seen on this account\. "
    r"Two other cardholders reported the same device profile this month\. Pattern not matched to a documented typology\. "
    r"Card blocked and reissued\.$"
)
RE_BURST = re.compile(
    r"^Case (CC-\d{4}): cardholder (C\d{5}) reported four online purchases within forty minutes, each just under \$500, "
    r"none of which they made\. Amounts appear chosen to stay under a \$500 authorization threshold\. "
    r"Pattern not matched to a documented typology\. Card blocked and reissued\.$"
)
CLEARED_BODIES = {
    "Cardholder confirmed travel to the billing region in question.": ("cleared_travel", "customer_confirmed_travel"),
    "Cardholder confirmed the purchase from a new phone. Device added to profile.": ("cleared_new_phone", "customer_confirmed_new_phone"),
    "Cardholder confirmed the purchase. Amount unusual for this customer but consistent with their stated intent.": ("cleared_amount", "customer_confirmed_amount"),
}


def parse_note(note: str) -> dict:
    """Return template_id, note_device, note_evidence_type for one analyst note (raises on an unknown shape)."""
    note = note.strip()
    m = RE_FRAUD.match(note)
    if m:
        body = m.group("body")
        dev = ""
        dm = RE_DEVICE.search(body)
        if dm:
            dev = f"{dm.group('dev')} on {dm.group('browser')}"
        return {"template_id": "fraud_reported", "note_device": dev, "note_evidence_type": "cardholder_report"}
    m = RE_CLEARED.match(note)
    if m:
        tid, ev = CLEARED_BODIES[m.group("body")]
        return {"template_id": tid, "note_device": "", "note_evidence_type": ev}
    m = RE_RING.match(note)
    if m:
        return {"template_id": "undoc_ring", "note_device": m.group("dev") + " behind an anonymous proxy",
                "note_evidence_type": "cardholder_report_shared_device"}
    if RE_BURST.match(note):
        return {"template_id": "undoc_burst", "note_device": "", "note_evidence_type": "cardholder_report_threshold_burst"}
    raise ValueError(f"unrecognised note template: {note[:120]}")


def mask(note: str) -> str:
    s = note.strip()
    s = re.sub(r"\$[\d,]+\.\d{2}", "<AMT>", s)
    s = re.sub(r"\bat 0\.\d{2}\b", "at <SCORE>", s)
    s = re.sub(r"\bCC-\d{4}\b", "<CASE>", s)
    s = re.sub(r"\bC\d{5}(-K\d)?\b", "<ID>", s)
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<DATE>", s)
    s = re.sub(r"\b\d+ (transaction\(s\)|online purchase\(s\))", r"<N> \1", s)
    return s


def embed_text(pattern: str, outcome: str, note_device: str, note_region: str, note: str) -> str:
    return (f"pattern: {pattern} | outcome: {outcome} | device: {note_device or 'none'} | "
            f"region: {note_region or 'none'} | {mask(note)}")


def build(con: duckdb.DuckDBPyConnection) -> dict:
    con.execute("PRAGMA threads=8")
    rows = con.execute("SELECT case_id, outcome, pattern, analyst_notes FROM cc ORDER BY case_id").fetchall()
    # note_region: the most frequent addr1 among the case's in-person transactions ('' when none)
    region = dict(con.execute("""
        SELECT case_id, addr1 FROM (
          SELECT p.case_id, t.addr1, row_number() OVER (PARTITION BY p.case_id ORDER BY count(*) DESC, min(t.ts)) rk
          FROM pairs p JOIN txc t USING (TransactionID)
          WHERE p.src = 'closed' AND t.channel = 'in_person' AND t.addr1 <> '' GROUP BY 1, 2) WHERE rk = 1
    """).fetchall())
    parsed = []
    for case_id, outcome, pattern, note in rows:
        p = parse_note(note)
        nr = region.get(case_id, "")
        parsed.append((case_id, p["template_id"], nr, p["note_device"], p["note_evidence_type"],
                       embed_text(pattern, outcome, p["note_device"], nr, note)))
    con.execute("CREATE OR REPLACE TEMP TABLE _parsed(case_id VARCHAR, template_id VARCHAR, note_region VARCHAR, note_device VARCHAR, note_evidence_type VARCHAR, embed_text VARCHAR)")
    con.executemany("INSERT INTO _parsed VALUES (?, ?, ?, ?, ?, ?)", parsed)
    con.execute("""
        CREATE OR REPLACE TABLE closed_case_parsed AS
        SELECT c.case_id AS id, c.customer_id, c.card_id,
               c.opened_at::TIMESTAMP AS opened_at, c.closed_at::TIMESTAMP AS closed_at,
               c.outcome, c.pattern, coalesce(c.first_fraud_txn_id, '') AS first_fraud_txn_id,
               c.n_txns::INT AS n_txns, c.exposure_usd::DOUBLE AS exposure_usd, c.actions_taken,
               (c.report_filed = 'Yes') AS report_filed, c.analyst_notes,
               p.template_id, p.note_region, p.note_device, p.note_evidence_type, p.embed_text
        FROM cc c JOIN _parsed p USING (case_id) ORDER BY c.case_id
    """)
    con.execute("""
        CREATE OR REPLACE TABLE closed_case_txn AS
        SELECT DISTINCT case_id, TransactionID FROM pairs WHERE src = 'closed' ORDER BY 1, 2
    """)
    con.execute("""
        CREATE OR REPLACE TABLE closed_case_conn AS
        SELECT case_id, unnest(string_split(connected_card_ids, '|')) AS card_id
        FROM cc WHERE connected_card_ids IS NOT NULL AND connected_card_ids <> '' ORDER BY 1, 2
    """)
    # integrity: every referenced transaction / card exists
    missing_txn = con.execute("SELECT count(*) FROM closed_case_txn x LEFT JOIN txc t USING (TransactionID) WHERE t.TransactionID IS NULL").fetchone()[0]
    missing_card = con.execute("SELECT count(*) FROM closed_case_conn x LEFT JOIN cardmap m USING (card_id) WHERE m.card_id IS NULL").fetchone()[0]
    assert missing_txn == 0 and missing_card == 0, (missing_txn, missing_card)
    rep = {
        "templates": dict(con.execute("SELECT template_id, count(*) FROM closed_case_parsed GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "n_cases": con.execute("SELECT count(*) FROM closed_case_parsed").fetchone()[0],
        "involves": con.execute("SELECT count(*) FROM closed_case_txn").fetchone()[0],
        "connected_to": con.execute("SELECT count(*) FROM closed_case_conn").fetchone()[0],
        "with_device": con.execute("SELECT count(*) FROM closed_case_parsed WHERE note_device <> ''").fetchone()[0],
        "with_region": con.execute("SELECT count(*) FROM closed_case_parsed WHERE note_region <> ''").fetchone()[0],
        "sample": con.execute("SELECT id, embed_text FROM closed_case_parsed WHERE id IN ('CC-0001','CC-0003','CC-2649','CC-3748','CC-4160') ORDER BY id").fetchall(),
    }
    render_report(rep)
    return rep


# ----------------------------------------------------------------------------------------
# rendering — display only: `build()` returns exactly the dict it always did
# ----------------------------------------------------------------------------------------

def render_report(rep: dict) -> None:
    """Print the parser report as tables: template counts against the documented ones, then samples."""
    t = Table(
        Col("template", max_width=20),
        Col("expected", align="right", width=9),
        Col("actual", align="right", width=9),
        Col("result", width=6, align="center"),
        title="note templates (all 5,565 rows must parse)",
    )
    wrong = []
    for name in sorted(set(EXPECTED_TEMPLATES) | set(rep["templates"])):
        want, got = EXPECTED_TEMPLATES.get(name), rep["templates"].get(name, 0)
        good = want is None or want == got
        if not good:
            wrong.append(f"{name}: expected {want:,}, parsed {got:,}")
        t.add_row(name, f"{want:,}" if want is not None else "-", f"{got:,}",
                  "PASS" if want is not None and good else "-" if want is None else "FAIL",
                  style=None if good else "red")
    total_want = sum(EXPECTED_TEMPLATES.values())
    t.add_row("TOTAL", f"{total_want:,}", f"{rep['n_cases']:,}",
              "PASS" if rep["n_cases"] == total_want else "FAIL",
              style=None if rep["n_cases"] == total_want else "red")
    t.print()
    for w in wrong:
        warn(w)
    if not wrong and rep["n_cases"] == total_want:
        ok("every note matched its documented template")

    e = Table(Col("edge / attribute", max_width=30), Col("rows", align="right", width=10),
              title="edge rows and note attributes")
    e.add_row("INVOLVES (case -> txn)", f"{rep['involves']:,}")
    e.add_row("CONNECTED_TO (case -> card)", f"{rep['connected_to']:,}")
    e.add_row("cases with note_device", f"{rep['with_device']:,}")
    e.add_row("cases with note_region", f"{rep['with_region']:,}")
    e.print()

    rule("embed_text samples (masked; one per template family)")
    width = max(40, min(shutil.get_terminal_size(fallback=(100, 24)).columns, 100) - 5)
    for case_id, text in rep["sample"]:
        step(case_id)
        for line in textwrap.wrap(text, width=width) or [""]:
            detail(line)


if __name__ == "__main__":  # pragma: no cover
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hhgoa.duckdb"
    header("etl.parse_closed_cases",
           "template-parse 5,565 analyst notes -> closed_case_parsed / _txn / _conn",
           {"db": db, "templates": len(EXPECTED_TEMPLATES), "masking": "<CASE> <ID> <N> <DATE> <AMT> <SCORE>"})
    con = duckdb.connect(db)
    rep = build(con)
    summary("etl.parse_closed_cases complete",
            {"closed cases": f"{rep['n_cases']:,}", "INVOLVES": f"{rep['involves']:,}",
             "CONNECTED_TO": f"{rep['connected_to']:,}", "db": db})
