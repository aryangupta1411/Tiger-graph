"""Graph view: runs the installed query `case_subgraph(c, as_of, hours)` (contract 2A)
through pyTigerGraph and renders ≤ 300 nodes/edges with streamlit-agraph
(falls back to pyvis). In mock mode the subgraph is built from the answer file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Graph", page_icon=common.PAGE_ICONS["graph"], layout="wide")
common.inject_css()
common.page_header("Case subgraph")

pack_ids = [m["case_id"] for m in common.load_case_pack()]
run_id = st.session_state.get("run_id") or (common.list_runs() or [None])[0]
c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
case_id = common.case_select(c1, pack_ids, key="graph_case")
hours = c2.number_input("hours before opened_at", 1, 24 * 30, 72)
renderer = c3.selectbox("Renderer", ["streamlit-agraph", "pyvis"])
meta = common.case_meta(case_id)
mode = common.run_mode()
c4.markdown(f"card `{meta.get('card_id','')}` · as_of `{meta.get('opened_at','')}`")

sub: dict | None = None
if mode == "live":
    try:
        conn = common.graph_conn()
        sub = common.run_installed(conn, "case_subgraph", {"c": meta.get("card_id", ""), "as_of": meta.get("opened_at", ""), "hours": int(hours)})
        st.caption(f"`case_subgraph` returned {len(sub.get('nodes', []))} nodes / {len(sub.get('edges', []))} edges")
    except Exception as exc:  # noqa: BLE001
        st.error(f"case_subgraph failed ({exc}); falling back to the answer-derived subgraph")
if sub is None or not sub.get("nodes"):
    ans = (common.load_case_run(run_id, case_id).answer if run_id else None) or common.load_promoted_answer(case_id)
    if not ans:
        st.warning("No answer file for this case yet; run the case (or `make mock-run`) to draw its subgraph.")
        st.stop()
    sub = common.subgraph_from_answer(ans, meta)
    st.caption(f"answer-derived subgraph: {len(sub['nodes'])} nodes / {len(sub['edges'])} edges (card, flagged + affected transactions, device profiles, connected cards, similar closed cases, the AgentCase vertex)")

FONT = "Inter, -apple-system, Helvetica, Arial, sans-serif"
MUTED = "#9aa3b2"
# the canvas paints its own labels, so it follows the app theme explicitly; an unknown theme (first load) gets
# the light palette on a light panel, which stays legible either way
DARK = st.context.theme.type == "dark"
INK, PAPER, SUBTLE, SELECT = ("#e6edf3", "#0d1117", "#8b949e", "#58a6ff") if DARK else ("#1a1a1a", "#ffffff", "#5b6472", "#0b3d91")
NODE_STYLE = {  # (vis shape, size); shape repeats the colour cue so the legend reads without colour
    "AgentCase": ("diamond", 28),
    "Card": ("dot", 24),
    "DeviceProfile": ("square", 20),
    "Customer": ("dot", 20),
    "EmailDomain": ("triangle", 17),
    "BillingRegion": ("triangle", 17),
    "ClosedCase": ("dot", 14),
    "Transaction": ("dot", 14),
}
SHAPE_GLYPH = {"dot": "●", "diamond": "◆", "square": "■", "triangle": "▲"}
EDGE_STYLE = {  # (colour, dashed): device links in red because they carry the ring signal; memory links dashed
    "OWNS": (MUTED, False),
    "MADE": ("#2563eb", False),
    "FROM_DEVICE": ("#c0392b", False),
    "SHARES_DEVICE": ("#c0392b", False),
    "PURCHASER_EMAIL": ("#7c3aed", False),
    "BILLED_IN": ("#0e7c86", False),
    "CASE_ON_CARD": ("#1e7e34", False),
    "CASE_INVOLVES": ("#1e7e34", False),
    "CASE_CONNECTED_TO": ("#1e7e34", False),
    "CASE_SIMILAR_TO": ("#5b6472", True),
    "ON_CARD": ("#5b6472", True),
}


def node_style(t: str) -> tuple[str, int]:
    return NODE_STYLE.get(t, ("dot", 16))


def edge_style(t: str) -> tuple[str, bool]:
    return EDGE_STYLE.get(t, (MUTED, False))


def short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def node_label(n: dict, detailed: bool) -> str:
    """Two short lines instead of one long one: the id, then what it is."""
    nid, desc = str(n["id"]), str(n.get("label") or "")
    if " | " in nid:  # device profiles are keyed by the full "model | os | browser | screen" string
        head, rest = nid.split(" | ", 1)
        tail = " · ".join(rest.split(" | ")[:2])
    else:
        head = nid
        tail = desc[len(nid):].strip(" ()") if desc.startswith(nid) else desc
    head = short(head, 22)
    return f"{head}\n{short(tail, 26)}" if detailed and tail else head


type_counts = {}
for n in sub["nodes"]:
    type_counts[common.vtype(n)] = type_counts.get(common.vtype(n), 0) + 1
types = sorted(type_counts)
f1, f2 = st.columns([5, 1], vertical_alignment="bottom")
keep = f1.multiselect("Vertex types", types, default=types)
edge_labels = f2.toggle("Edge labels", value=False, help="Relationship names always show when you hover an edge; switch this on to print them on every edge.")
nodes = [n for n in sub["nodes"] if common.vtype(n) in keep]
ids = {n["id"] for n in nodes}
seen_edges: set[tuple] = set()
edges = []
for e in sub["edges"]:
    k = (e.get("src"), e.get("dst"), common.etype(e))
    if k[0] in ids and k[1] in ids and k not in seen_edges:
        seen_edges.add(k)
        edges.append(e)
detailed = len(nodes) <= 60
dense = len(nodes) > 20  # vis zooms out to fit a big view, so it gets larger type and smaller cards
HEIGHT = 720 if dense else 620
node_by_id = {n["id"]: n for n in nodes}

legend_nodes = "".join(
    f"<span style='white-space:nowrap'><span style='color:{common.NODE_COLORS.get(t, MUTED)};font-size:15px'>{SHAPE_GLYPH[node_style(t)[0]]}</span>"
    f"&nbsp;{t} <span style='color:#8a93a3'>{type_counts[t]}</span></span>"
    for t in types if t in keep
)
edge_types = sorted({common.etype(e) for e in edges})
legend_edges = "".join(
    f"<span style='white-space:nowrap'><span style='display:inline-block;width:22px;border-top:2px {'dashed' if edge_style(t)[1] else 'solid'} {edge_style(t)[0]};vertical-align:middle'></span>"
    f"&nbsp;{t}</span>"
    for t in edge_types
)
row = f"display:flex;flex-wrap:wrap;gap:6px 20px;align-items:center;font-size:13px;color:{SUBTLE};margin:2px 0 6px"
st.markdown(f"<div style='{row}'>{legend_nodes}</div><div style='{row};margin-bottom:12px'>{legend_edges}</div>", unsafe_allow_html=True)

vis_nodes = []
for n in nodes:
    t = common.vtype(n)
    shape, size = node_style(t)
    if dense and t == "Card":
        size = 16
    c = common.NODE_COLORS.get(t, MUTED)
    vis_nodes.append({
        "id": n["id"],
        "label": node_label(n, detailed),
        "shape": shape,
        "size": size,
        "color": {"background": c, "border": PAPER, "highlight": {"background": c, "border": SELECT}, "hover": {"background": c, "border": SELECT}},
    })
vis_edges = []
for e in edges:
    t = common.etype(e)
    c, dashed = edge_style(t)
    ve = {"source": e["src"], "target": e["dst"], "title": t, "dashes": dashed, "color": {"color": c, "highlight": SELECT, "hover": c, "opacity": 0.9}}
    if edge_labels:
        ve["label"] = t
    vis_edges.append(ve)

VIS_OPTIONS = {
    "autoResize": True,
    "nodes": {
        "font": {"face": FONT, "size": 18 if dense else 13, "color": INK, "strokeWidth": 4, "strokeColor": PAPER},
        "borderWidth": 2,
        "borderWidthSelected": 3,
    },
    "edges": {
        "width": 1.4,
        "selectionWidth": 1.5,
        "hoverWidth": 0.8,
        "arrows": {"to": {"enabled": True, "scaleFactor": 0.45}},
        "smooth": {"enabled": True, "type": "continuous", "roundness": 0.2},
        "font": {"face": FONT, "size": 10, "color": SUBTLE, "strokeWidth": 3, "strokeColor": PAPER, "align": "middle"},
    },
    "physics": {
        "enabled": len(nodes) < 150,
        "solver": "barnesHut",
        "barnesHut": {"gravitationalConstant": -7000 if dense else -9000, "centralGravity": 0.3 if dense else 0.25, "springLength": 150 if dense else 170, "springConstant": 0.04 if dense else 0.035, "damping": 0.35, "avoidOverlap": 0.8 if dense else 0.5},
        "stabilization": {"enabled": True, "iterations": 600, "fit": True},
        "minVelocity": 0.75,
    },
    "layout": {"randomSeed": 7, "improvedLayout": True, "hierarchical": {"enabled": False}},
    "interaction": {"hover": True, "tooltipDelay": 120, "zoomView": True, "dragView": True, "navigationButtons": False, "keyboard": False},
}

g_col, d_col = st.columns([3, 1], gap="medium")
clicked = None
with g_col:
    st.markdown(f"<style>.st-key-graph_panel {{background: {PAPER};}}</style>", unsafe_allow_html=True)
    with st.container(border=True, key="graph_panel"):
        if renderer == "streamlit-agraph":
            try:
                from streamlit_agraph import Config, Edge, Node, agraph

                gnodes = []
                for vn in vis_nodes:
                    gn = Node(**vn)
                    gn.__dict__.pop("title", None)  # the component opens a node's title as a URL on double-click
                    gnodes.append(gn)
                gedges = [Edge(**{k: v for k, v in ve.items() if k != "color"}, color=ve["color"]) for ve in vis_edges]
                cfg = Config(directed=True, physics=len(nodes) < 150, hierarchical=False)
                cfg.__dict__.update(VIS_OPTIONS, width="100%", height=f"{HEIGHT}px")  # Config appends "px" to width, so set it directly
                cfg.__dict__.pop("groups", None)  # Config defaults groups=None, which vis rejects on every redraw
                clicked = agraph(nodes=gnodes, edges=gedges, config=cfg)
            except ImportError:
                st.warning("streamlit-agraph not installed; using pyvis")
                renderer = "pyvis"
        if renderer == "pyvis":
            import json

            from pyvis.network import Network

            net = Network(height=f"{HEIGHT}px", width="100%", directed=True, notebook=False, cdn_resources="in_line", bgcolor=PAPER, font_color=INK)
            for vn in vis_nodes:
                net.add_node(vn["id"], label=vn["label"], shape=vn["shape"], size=vn["size"], color=vn["color"], title=f"{common.vtype(node_by_id[vn['id']])}\n{vn['id']}")
            for ve in vis_edges:
                net.add_edge(ve["source"], ve["target"], title=ve["title"], dashes=ve["dashes"], color=ve["color"], **({"label": ve["label"]} if "label" in ve else {}))
            net.set_options(json.dumps(VIS_OPTIONS))
            html = (  # the page already draws the border, so drop pyvis's own two
                net.generate_html()
                .replace("border: 1px solid lightgray;", "border: none;")
                .replace('<div class="card" style="width: 100%">', '<div class="card" style="width: 100%; border: none">')
            )
            st.iframe(html, height=HEIGHT + 20)

with d_col:
    st.markdown("**Selected node**")
    if clicked and clicked in node_by_id:
        n = node_by_id[clicked]
        t = common.vtype(n)
        st.markdown(
            f"<span style='color:{common.NODE_COLORS.get(t, MUTED)};font-size:15px'>{SHAPE_GLYPH[node_style(t)[0]]}</span>&nbsp;<b>{t}</b>",
            unsafe_allow_html=True,
        )
        st.code(str(n["id"]), language=None, wrap_lines=True)
        if n.get("label") and str(n["label"]) != str(n["id"]):
            st.caption(str(n["label"]))
        links = []
        for e in edges:
            if clicked in (e["src"], e["dst"]):
                other = e["dst"] if e["src"] == clicked else e["src"]
                arrow = "→" if e["src"] == clicked else "←"
                links.append(f"{arrow} `{common.etype(e)}` {short(str(other).split(' | ')[0], 30)} :gray[({common.vtype(node_by_id.get(other, {}))})]")
        st.markdown(f"**Connections** ({len(links)})")
        st.markdown("\n".join(f"- {x}" for x in links[:25]) + (f"\n- … and {len(links) - 25} more" if len(links) > 25 else "") if links else "_none in this view_")
    elif renderer == "pyvis":
        st.caption("Hover a node for its id. Switch the renderer to streamlit-agraph to inspect a node's connections here.")
    else:
        st.caption("Click a node to see its full id, type and every connection. Drag to pan, scroll to zoom, hover an edge for its relationship.")
    st.divider()
    st.caption(f"{len(nodes)} nodes · {len(edges)} edges in view")

with st.expander("Nodes / edges"):
    st.dataframe(nodes, width="stretch", hide_index=True)
    st.dataframe(edges, width="stretch", hide_index=True)
