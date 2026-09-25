"""Shared helpers for the Streamlit UI.

Everything the pages show comes from three places, in this order of trust:
  1. runs/<run_id>/<case_id>/{answer.json, phases.jsonl, calls.jsonl, sar.md}   (contract 2D)
  2. ui/approvals.sqlite                                                        (ui/approvals_db.py)
  3. the graph, read through pyTigerGraph installed queries                      (contract 2A; live only)

RUN_MODE=mock (default when TG_HOST is unset) never opens a graph connection:
the graph page builds the subgraph from the answer file, the approvals page
records decisions locally only and says so.

phases.jsonl rows:  {"phase": "P3", "name": "scorecard", "at": "...", "payload": {...}}
calls.jsonl rows:   {"seq", "ts", "kind": "tool"|"llm", "phase", "name", "params", "latency_s",
                     "result_bytes", "ok"}  and for kind=llm  {"model", "input_tokens",
                     "output_tokens", "cache_read_tokens"}
Every reader below tolerates missing keys so a partially written run still renders.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Paths and mode                                                              #
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    return os.getenv(name, default)


def run_mode() -> str:
    """'live' only when RUN_MODE=live and TG_HOST is set; otherwise 'mock'."""
    mode = _env("RUN_MODE", "mock").lower()
    return "live" if mode == "live" and _env("TG_HOST") else "mock"


def read_only() -> bool:
    """DEPLOY_READONLY=1 (the Dockerfile default): a public deployment never spawns the agent and never
    writes to the graph, whatever RUN_MODE says. Graph reads and local approval decisions still work."""
    return _env("DEPLOY_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


def case_select(where: Any, pack_ids: list[str], key: str, preferred: str | None = None) -> str:
    """The Case selectbox of the queue, case and graph pages. Each page's widget owns its key and is
    seeded from st.session_state["case_id"] only while it has no value, so a pick sticks (an index=
    that changes with the pick rebuilds the widget and snaps it back to the previous case) and the
    last opened case still follows the analyst from page to page."""
    import streamlit as st

    if st.session_state.get(key) not in pack_ids:
        want = preferred or st.session_state.get("case_id") or "HHG-014"
        st.session_state[key] = want if want in pack_ids else pack_ids[0]
    case_id = where.selectbox("Case", pack_ids, key=key)
    st.session_state["case_id"] = case_id
    return case_id


def runs_root() -> Path:
    p = Path(_env("RUNS_DIR", str(ROOT / "runs")))
    return p if p.is_absolute() else ROOT / p


def cases_dir() -> Path:
    return ROOT / "cases"


def approvals_db_path() -> Path:
    return Path(_env("APPROVALS_DB", str(ROOT / "ui" / "approvals.sqlite")))


# --------------------------------------------------------------------------- #
# Case pack                                                                   #
# --------------------------------------------------------------------------- #
CASE_PACK_COLUMNS = ["case_id", "opened_at", "trigger_type", "flagged_txn_id", "card_id", "customer_id", "risk_score", "trigger_text"]

# The 20 exam cases, verbatim from dataset/README.md ("The 20 Cases"), so the
# queue renders even before data/raw/case_pack.csv exists on a fresh clone.
_CASE_PACK_FALLBACK_CSV = """case_id,opened_at,trigger_type,flagged_txn_id,card_id,customer_id,risk_score,trigger_text
HHG-001,2016-12-05 01:55:28,risk_score,3514030,C12382-K1,C12382,0.61,"Real-time model scored transaction 3514030 ($77.07, in billing region 444.0) at 0.61. Review and decide."
HHG-002,2016-11-22 23:27:07,risk_score,3478782,C11891-K1,C11891,0.79,"Real-time model scored transaction 3478782 ($292.36, online) at 0.79. Review and decide."
HHG-003,2016-12-10 15:01:21,customer_report,3530164,C08623-K2,C08623,,"Customer C08623 message: 'I never made this $49.00 purchase. Please check my card.' Refers to 3530164."
HHG-004,2016-12-29 07:53:54,customer_report,3583227,C08106-K1,C08106,,"Customer C08106 message: 'I never made this $128.33 purchase. Please check my card.' Refers to 3583227."
HHG-005,2016-12-08 03:38:37,risk_score,3523199,C02923-K1,C02923,0.54,"Real-time model scored transaction 3523199 ($100.07, online) at 0.54. Review and decide."
HHG-006,2016-11-22 02:30:00,customer_report,3476682,C07297-K1,C07297,,"Customer C07297 message: 'I never made this $482.12 purchase. Please check my card.' Refers to 3476682."
HHG-007,2016-12-05 03:46:14,risk_score,3514948,C09933-K2,C09933,0.87,"Real-time model scored transaction 3514948 ($111.92, in billing region 264.0) at 0.87. Review and decide."
HHG-008,2016-12-20 03:08:56,customer_report,3558054,C13171-K2,C13171,,"Customer C13171 message: 'I never made this $55.68 purchase. Please check my card.' Refers to 3558054."
HHG-009,2016-12-28 17:10:53,customer_report,3581141,C08299-K1,C08299,,"Customer C08299 message: 'I never made this $30.02 purchase. Please check my card.' Refers to 3581141."
HHG-010,2016-12-02 18:18:27,risk_score,3506725,C10434-K1,C10434,0.90,"Real-time model scored transaction 3506725 ($1,000.03, online) at 0.90. Review and decide."
HHG-011,2016-12-29 06:27:44,customer_report,3583368,C11923-K2,C11923,,"Customer C11923 message: 'I never made this $131.30 purchase. Please check my card.' Refers to 3583368."
HHG-012,2016-12-18 05:00:31,risk_score,3553342,C05876-K2,C05876,0.55,"Real-time model scored transaction 3553342 ($30.91, in billing region 494.0) at 0.55. Review and decide."
HHG-013,2016-12-09 05:39:29,risk_score,3526826,C07671-K2,C07671,0.76,"Real-time model scored transaction 3526826 ($35.66, online) at 0.76. Review and decide."
HHG-014,2016-11-22 20:11:00,analyst_request,3478561,C13487-K1,C13487,,"Analyst request: several cards this month show purchases from the same unusual device profile. Review transaction 3478561 on card C13487-K1 and look for related activity."
HHG-015,2016-11-17 19:03:36,risk_score,3464869,C03042-K1,C03042,0.77,"Real-time model scored transaction 3464869 ($599.94, online) at 0.77. Review and decide."
HHG-016,2016-12-12 01:39:08,customer_report,3534820,C09988-K1,C09988,,"Customer C09988 message: 'I never made this $59.67 purchase. Please check my card.' Refers to 3534820."
HHG-017,2016-11-12 00:46:24,risk_score,3450629,C04570-K1,C04570,0.57,"Real-time model scored transaction 3450629 ($100.09, online) at 0.57. Review and decide."
HHG-018,2016-11-27 14:41:26,customer_report,3491361,C02354-K2,C02354,,"Customer C02354 message: 'I never made this $39.08 purchase. Please check my card.' Refers to 3491361."
HHG-019,2016-12-01 22:28:53,risk_score,3503878,C07987-K2,C07987,0.90,"Real-time model scored transaction 3503878 ($99.92, online) at 0.90. Review and decide."
HHG-020,2016-12-03 12:04:26,risk_score,3509359,C12265-K2,C12265,0.52,"Real-time model scored transaction 3509359 ($125.08, online) at 0.52. Review and decide."
"""


def load_case_pack() -> list[dict[str, str]]:
    """Rows of case_pack.csv (data/raw first, then the embedded README copy), in opened_at order."""
    path = ROOT / "data" / "raw" / "case_pack.csv"
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    else:
        rows = list(csv.DictReader(io.StringIO(_CASE_PACK_FALLBACK_CSV)))
    return sorted(rows, key=lambda r: r["opened_at"])


def case_meta(case_id: str) -> dict[str, str]:
    for r in load_case_pack():
        if r["case_id"] == case_id:
            return r
    return {"case_id": case_id}


# --------------------------------------------------------------------------- #
# Runs                                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class CaseRun:
    run_id: str
    case_id: str
    dir: Path
    answer: dict | None = None
    phases: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    sar_md: str = ""

    @property
    def ok(self) -> bool:
        return self.answer is not None


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a row still being written
    return rows


def list_runs() -> list[str]:
    """Run ids, newest first (by manifest / directory mtime)."""
    root = runs_root()
    if not root.exists():
        return []
    runs = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    runs.sort(key=lambda p: (p / "run_manifest.json").stat().st_mtime if (p / "run_manifest.json").exists() else p.stat().st_mtime, reverse=True)
    return [p.name for p in runs]


def run_manifest(run_id: str) -> dict:
    p = runs_root() / run_id / "run_manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def list_cases_in_run(run_id: str) -> list[str]:
    d = runs_root() / run_id
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "answer.json").exists() or (p / "calls.jsonl").exists())


def load_case_run(run_id: str, case_id: str) -> CaseRun:
    d = runs_root() / run_id / case_id
    cr = CaseRun(run_id=run_id, case_id=case_id, dir=d)
    ap = d / "answer.json"
    if ap.exists():
        try:
            cr.answer = json.loads(ap.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cr.answer = None
    cr.phases = _read_jsonl(d / "phases.jsonl")
    cr.calls = _read_jsonl(d / "calls.jsonl")
    sp = d / "sar.md"
    cr.sar_md = sp.read_text(encoding="utf-8") if sp.exists() else ""
    return cr


def load_promoted_answer(case_id: str) -> dict | None:
    p = cases_dir() / f"{case_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def phase_payload(run: CaseRun, code: str) -> dict:
    """Payload of the last row for phase ``code`` (e.g. 'P3'); {} when absent."""
    for row in reversed(run.phases):
        if row.get("phase") == code:
            return row.get("payload") or {}
    return {}


# --------------------------------------------------------------------------- #
# Derived views used by the case page                                         #
# --------------------------------------------------------------------------- #
FAMILY_LABELS = {"history": "card history", "device": "device / shared origin", "memory": "case memory", "customer": "customer / analyst reply", "document": "policy document"}
FAMILY_ORDER = ["history", "device", "memory", "customer", "document"]

_RULE_RE = re.compile(r"\b(R(?:10|[1-9])|3a|3b|§\s?6|policy 6|section 6)\b", re.IGNORECASE)


def family_of_evidence(ev: dict) -> str:
    ref = (ev.get("ref") or "").lower()
    src = ev.get("source")
    if src == "customer":
        return "customer"
    if src == "document":
        return "document"
    if any(k in ref for k in ("prior_cases", "similar", "closed")):
        return "memory"
    if any(k in ref for k in ("device", "ring", "shared_origin", "email_neighbors", "burst")):
        return "device"
    return "history"


def families(run: CaseRun) -> tuple[list[str], list[str]]:
    """(families_fraud, families_legit) from the scorecard phase, else derived from the evidence list."""
    p3 = phase_payload(run, "P3")
    if p3.get("families_fraud") is not None or p3.get("families_legit") is not None:
        return sorted(p3.get("families_fraud") or []), sorted(p3.get("families_legit") or [])
    ev = (run.answer or {}).get("case", {}).get("evidence", [])
    fam = sorted({family_of_evidence(e) for e in ev if e.get("source") != "document"})
    return fam, []


def probability_pre_post(run: CaseRun) -> tuple[float | None, float | None]:
    """(pre-evidence, post-evidence) probability. pre = P4 assess (else P3 p_engine); post = P7 (else answer)."""
    ans = run.answer or {}
    post = ans.get("case", {}).get("fraud_probability")
    p7 = phase_payload(run, "P7")
    if p7.get("fraud_probability") is not None:
        post = p7["fraud_probability"]
    p4 = phase_payload(run, "P4")
    p3 = phase_payload(run, "P3")
    pre = p4.get("fraud_probability", p3.get("p_engine"))
    if pre is None:
        pre = post
    return (None if pre is None else float(pre)), (None if post is None else float(post))


def rule_chips(reason: str) -> list[str]:
    seen: list[str] = []
    for m in _RULE_RE.findall(reason or ""):
        tag = m.upper().replace(" ", "")
        tag = {"POLICY6": "§6", "SECTION6": "§6", "3A": "3a", "3B": "3b"}.get(tag, tag)
        if tag not in seen:
            seen.append(tag)
    return seen


ROUTE_COLORS = {"auto": "gray", "L1": "orange", "L2": "red"}
VERDICT_COLORS = {"fraud": "red", "legitimate": "green", "uncertain": "orange"}
STATUS_COLORS = {"open": "blue", "escalated": "orange", "closed_fraud": "red", "closed_legitimate": "green"}
SOURCE_COLORS = {"graph": "blue", "document": "violet", "customer": "orange", "external": "gray"}


def badge(text: str, color: str) -> str:
    """Streamlit markdown colour badge (``:color-badge[text]`` needs streamlit >= 1.40).
    Colours resolve through .streamlit/config.toml's palette, so "red" is always the
    same red everywhere in the app rather than Streamlit's stock per-color defaults."""
    return f":{color}-badge[{text}]"


def route_badge(route: str) -> str:
    return badge(route, ROUTE_COLORS.get(route, "gray"))


def source_badge(source: str) -> str:
    return badge(source or "—", SOURCE_COLORS.get(source, "gray"))


# --------------------------------------------------------------------------- #
# Page chrome — one look shared by every page: a single injected                #
# stylesheet plus a consistent header component, instead of six pages each   #
# improvising their own st.title() + emoji.                                  #
# --------------------------------------------------------------------------- #
PAGE_ICONS = {
    "app": ":material/verified_user:",
    "queue": ":material/inbox:",
    "case": ":material/search:",
    "approvals": ":material/task_alt:",
    "graph": ":material/hub:",
    "runs": ":material/monitoring:",
}


def inject_css() -> None:
    """Small stylesheet on top of the config.toml theme: tighten default
    Streamlit spacing, give containers a touch more polish, and drop the
    "Made with Streamlit" footer. Safe to call once per page load."""
    import streamlit as st

    st.markdown(
        """
        <style>
        /* tighten the default top margin so the header sits closer to the sidebar */
        .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px; }
        footer { visibility: hidden; }
        /* main-menu (⋮) cleanup: keep Print + the theme toggle, drop the
           screen-recorder item and the "Made with Streamlit vX.Y.Z" footer.
           toolbarMode="viewer" bundles print/record/theme-toggle together
           with no config flag to hide just one, so this is CSS by necessity. */
        [data-testid="stMainMenuItem-recordScreencast"] { display: none !important; }
        [data-testid="stMainMenuPopover"] > div > div:not([data-testid]) { display: none !important; }
        /* bordered containers (st.container(border=True)) read as cards, not boxes */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 10px !important;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }
        /* metric labels: allow wrapping instead of Streamlit's default single-line ellipsis.
           the actual truncation lives on the nested stMarkdownContainer (fixed px width,
           nowrap, text-overflow:ellipsis) and its <p>, not on stMetricLabel itself. */
        [data-testid="stMetricLabel"],
        [data-testid="stMetricLabel"] > div,
        [data-testid="stMetricLabel"] [data-testid="stMarkdownContainer"],
        [data-testid="stMetricLabel"] [data-testid="stMarkdownContainer"] p {
            white-space: normal !important;
            overflow: visible !important;
            text-overflow: clip !important;
            width: 100% !important;
            max-width: 100% !important;
            line-height: 1.25 !important;
        }
        /* every other widget label (checkbox, selectbox, text_input, ...) uses the same
           fixed-width nowrap+ellipsis pattern as stMetricLabel; fix it once, globally. */
        [data-testid="stWidgetLabel"],
        [data-testid="stWidgetLabel"] [data-testid="stMarkdownContainer"],
        [data-testid="stWidgetLabel"] [data-testid="stMarkdownContainer"] p {
            white-space: normal !important;
            overflow: visible !important;
            text-overflow: clip !important;
            width: 100% !important;
            max-width: 100% !important;
            line-height: 1.25 !important;
        }
        div[data-testid="stMetricValue"],
        div[data-testid="stMetricValue"] > div {
            font-weight: 700;
            overflow: visible !important;
            white-space: normal !important;
            text-overflow: clip !important;
            width: 100% !important;
            max-width: 100% !important;
            font-size: 1.3rem !important;
            line-height: 1.3 !important;
            word-break: break-word;
        }
        /* sidebar nav: a touch more breathing room between page links */
        div[data-testid="stSidebarNav"] a { border-radius: 8px; }
        h1, h2, h3 { letter-spacing: -0.01em; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "", icon: str = "") -> None:
    """Consistent page header: one H2 with an optional muted subtitle line,
    used by every sub-page instead of st.title() + an emoji glued to the text."""
    import streamlit as st

    prefix = f"{icon} " if icon else ""
    st.markdown(f"## {prefix}{title}")
    if subtitle:
        st.caption(subtitle)


def actions_diff(initial: list[dict], final: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """(added, removed, kept) action names between the initial and final recommendation."""
    a = [x.get("action") for x in initial]
    b = [x.get("action") for x in final]
    return [x for x in b if x not in a], [x for x in a if x not in b], [x for x in b if x in a]


def counterfactual(run: CaseRun) -> str:
    """What would have happened on the other branch (voi.py / simulator.py), else what_changed."""
    p6 = phase_payload(run, "P6")
    cf = p6.get("counterfactual") or ""
    if not cf:
        sim = p6.get("simulated") or {}
        cf = sim.get("counterfactual") or ""
    if not cf:
        cf = (run.answer or {}).get("next_best_actions", {}).get("what_changed", "")
    return "" if cf == "nothing" else cf


def voi_branches(run: CaseRun) -> dict:
    p6 = phase_payload(run, "P6")
    voi = p6.get("voi") or {}
    return voi.get("branches") or {}


# --------------------------------------------------------------------------- #
# SAR validator badges - delegated to rag.validate_sar (the single validator,   #
# the single validator); no local copy of the rules.                          #
# --------------------------------------------------------------------------- #
SAR_CHECK_LABELS = [
    ("V00", "sar.file agrees with FILE_REPORT in final; reason cites a rule; negative SAR fields empty"),
    ("V01", "narrative is one paragraph"),
    ("V02", "6-12 sentences"),
    ("V03", "at least one ISO date and one amount"),
    ("V04", "activity_dates both appear in the narrative"),
    ("V05", "activity_dates = [first, last]"),
    ("V06", "total_amount_usd == exposure_usd and written in the narrative"),
    ("V07", "subjects present, include customer + card, named in narrative, resolve"),
    ("V08", "every id in the narrative resolves"),
    ("V09", "no 'see attached' / markdown"),
    ("V10", "internal case id named"),
    ("V11", "OFAC result and prior-report status stated"),
    ("V12", "no seeding tell (':00 seconds')"),
    ("V13", "no tables (a '|' inside a device profile is fine)"),
    ("V14", "a pending block is not described as done"),
    ("V15", "no gendered pronouns"),
    ("V16", "no time-zone claim"),
    ("V17", "every named id / device is a subject"),
    ("V18", "prior-report claim agrees with the evidence"),
]


def sar_checks(answer: dict, meta: dict | None = None, resolver: Any | None = None) -> list[tuple[str, bool, str]]:
    """[(check, ok, detail)] for the SAR tab, one row per rag.validate_sar code V00-V18 (V19 needs the as-of baseline and runs only in P8), computed from the answer
    alone (plus the case pack's customer/card ids in `meta`). Ids resolve against the answer's own lists unless a
    `resolver` (rag.validate_sar.DuckResolver over data/ids.duckdb) is given; the run's P8 validator payload is
    merged in by the page. When sar.file is false only V00 applies."""
    from rag.validate_sar import validate_answer_sar

    meta = meta or {}
    try:
        errs = validate_answer_sar(answer, customer_id=meta.get("customer_id", ""), card_id=meta.get("card_id", ""), resolver=resolver)
    except Exception as exc:  # noqa: BLE001 - a half-written answer must still render
        return [("rag.validate_sar ran", False, str(exc)[:160])]
    filed = bool((answer.get("sar") or {}).get("file"))
    out: list[tuple[str, bool, str]] = []
    for code, label in SAR_CHECK_LABELS:
        if code != "V00" and not filed:
            continue
        hits = [e for e in errs if e.startswith(code)]
        out.append((label, not hits, "; ".join(h[len(code):].strip() for h in hits)))
    return out


# --------------------------------------------------------------------------- #
# Graph access (live) and answer-derived subgraph (mock)                      #
# --------------------------------------------------------------------------- #
def graph_conn():
    """Sync pyTigerGraph connection with retry, or None in mock mode. Cached per Streamlit process."""
    if run_mode() != "live":
        return None
    import streamlit as st

    @st.cache_resource(show_spinner="Connecting to TigerGraph (waking the workspace if needed)…")
    def _conn():
        from ops.ensure_awake import tg_connection, warm_up

        c = tg_connection()
        warm_up(c)
        return c

    return _conn()


def run_installed(conn, name: str, params: dict, timeout_ms: int = 60_000) -> dict:
    """Run an installed query and merge its PRINT objects into one dict (contract 2A keys)."""
    from ops.ensure_awake import run_query

    res = run_query(conn, name, params, timeout_ms=timeout_ms)
    merged: dict[str, Any] = {}
    for obj in res or []:
        if isinstance(obj, dict):
            merged.update(obj)
    return merged


# Coordinated with .streamlit/config.toml's chartCategoricalColors so the graph
# page's node palette reads as part of the same design system, not a clip-art set.
NODE_COLORS = {
    "Card": "#b8860b",
    "Transaction": "#2563eb",
    "DeviceProfile": "#c0392b",
    "EmailDomain": "#7c3aed",
    "BillingRegion": "#0e7c86",
    "ClosedCase": "#5b6472",
    "AgentCase": "#1e7e34",
    "Customer": "#0b3d91",
}


def vtype(node: dict) -> str:
    """Vertex type of a case_subgraph node: the installed query prints `vtype` (`type` is reserved in GSQL); the
    answer-derived subgraph and older fixtures use `type`."""
    return str(node.get("vtype") or node.get("type") or "")


def etype(edge: dict) -> str:
    """Edge type of a case_subgraph edge (`etype`, falling back to `type`)."""
    return str(edge.get("etype") or edge.get("type") or "")


def subgraph_from_answer(answer: dict, meta: dict) -> dict:
    """A case_subgraph-shaped {nodes[{id, vtype, label}], edges[{src, dst, etype}]} built from the answer file
    alone (mock / fallback) - the same keys the installed query prints (read them through vtype() / etype())."""
    case = answer.get("case", {})
    card = meta.get("card_id", "")
    cust = meta.get("customer_id", "")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(nid: str, ntype: str, label: str | None = None):
        if nid and nid not in nodes:
            nodes[nid] = {"id": nid, "vtype": ntype, "label": label or nid}

    add(cust, "Customer")
    add(card, "Card")
    if cust and card:
        edges.append({"src": cust, "dst": card, "etype": "OWNS"})
    gid = case.get("graph_case_id") or f"AC-{answer.get('case_id','')}"
    add(gid, "AgentCase", f"{gid} ({case.get('verdict','')})")
    if card:
        edges.append({"src": gid, "dst": card, "etype": "CASE_ON_CARD"})
    for t in case.get("affected_txn_ids", []):
        add(t, "Transaction")
        edges.append({"src": card, "dst": t, "etype": "MADE"})
        edges.append({"src": gid, "dst": t, "etype": "CASE_INVOLVES"})
    flagged = meta.get("flagged_txn_id", "")
    if flagged and flagged not in nodes:
        add(flagged, "Transaction", f"{flagged} (flagged)")
        edges.append({"src": card, "dst": flagged, "etype": "MADE"})
    for d in case.get("connected_device_profiles", []):
        add(d, "DeviceProfile", d.split(" | ")[0])
        for t in case.get("affected_txn_ids", []):
            edges.append({"src": t, "dst": d, "etype": "FROM_DEVICE"})
        for c in case.get("connected_card_ids", []):
            add(c, "Card")
            edges.append({"src": c, "dst": d, "etype": "SHARES_DEVICE"})
    if not case.get("connected_device_profiles"):
        for c in case.get("connected_card_ids", []):
            add(c, "Card")
            edges.append({"src": gid, "dst": c, "etype": "CASE_CONNECTED_TO"})
    for cc in case.get("similar_prior_cases", []):
        add(cc, "ClosedCase")
        edges.append({"src": gid, "dst": cc, "etype": "CASE_SIMILAR_TO"})
    return {"nodes": list(nodes.values()), "edges": edges}


# --------------------------------------------------------------------------- #
# Small formatting helpers                                                    #
# --------------------------------------------------------------------------- #
def link_to(page_file: str, label: str, icon: str | None = None) -> None:
    """st.page_link that degrades to a caption when the page cannot be resolved
    (a page executed on its own, e.g. under streamlit.testing.AppTest)."""
    import streamlit as st

    try:
        st.page_link(page_file, label=label, icon=icon)
    except Exception:  # noqa: BLE001 - StreamlitPageNotFoundError and friends
        st.caption(f"→ {label} (open from the sidebar)")



def money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{100*float(x):.0f}%"


def tool_stats(run: CaseRun) -> dict:
    """Per-case totals from calls.jsonl, falling back to the answer's own counters."""
    tools = [c for c in run.calls if c.get("kind", "tool") == "tool"]
    llms = [c for c in run.calls if c.get("kind") == "llm"]
    ans = run.answer or {}
    tokens = sum(int(c.get("input_tokens", 0)) + int(c.get("output_tokens", 0)) for c in llms) or int(ans.get("tokens", 0) or 0)
    return {
        "tool_calls": len(tools) or int(ans.get("tool_calls", 0) or 0),
        "llm_calls": len(llms),
        "tokens": tokens,
        "latency_s": float(ans.get("latency_s", 0) or sum(float(c.get("latency_s", 0) or 0) for c in run.calls)),
        "tool_latency_s": round(sum(float(c.get("latency_s", 0) or 0) for c in tools), 2),
        "llm_latency_s": round(sum(float(c.get("latency_s", 0) or 0) for c in llms), 2),
    }
