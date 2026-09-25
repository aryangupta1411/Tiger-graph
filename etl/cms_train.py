"""etl/cms_train.py — the case-memory scorer (CMS): LightGBM over the Vesta columns + identity +
risk_score + leakage-free card-history features, trained on the bank's own closed-case labels.

    python -m etl.cms_train data/hhgoa.duckdb --out data/models

Procedure (PLAN §4.1 #4, §4.5):
  1. holdout run: train Jul–Sep, early-stop on October   -> best_iter, October AUC / calibration table
  2. isotonic calibrator fit on the October out-of-fold predictions  -> data/models/isotonic.json
  3. month-wise out-of-fold cms_p for EVERY Jul–Oct row (train on the other three months, best_iter rounds)
     so the 310-case QA replay never sees a score that was trained on its own label
  4. final model on Jul–Oct (best_iter rounds) scores Nov–Dec        -> data/models/lgb_fraud.txt
  5. cms_p / cms_fold written into txn_feat (card_seq and prior_* already live there; export reads them)
Everything is seeded; a full run is ~2–3 minutes on an M-series Mac (one fit ≈ 15 s).
cms_p is the RAW model probability; the engine applies cal() = load_calibrator()(cms_p).
"""
from __future__ import annotations

import argparse
import json
import os
import time

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, roc_auc_score

from ops.console import Col, Table, fail, header, ok, prob, rule, step, summary, warn

# PLAN §4.1 #4: the October holdout AUC is the headline correctness claim for the scorer.
AUC_TARGET = 0.95

PARAMS = dict(objective="binary", learning_rate=0.05, num_leaves=127, min_data_in_leaf=100,
              feature_fraction=0.5, bagging_fraction=0.8, bagging_freq=1, lambda_l2=10,
              verbose=-1, num_threads=8, max_cat_to_onehot=4, cat_smooth=50, seed=42,
              bagging_seed=42, feature_fraction_seed=42, deterministic=True)
CAT_TX = ["ProductCD", "card4", "card6", "addr1", "addr2", "P_emaildomain", "R_emaildomain",
          "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "card2", "card3", "card5", "dist1", "dist2"]
ID_COLS = [f"id_{i:02d}" for i in range(1, 39)]
ID_NUM, ID_CAT = ID_COLS[:11], ID_COLS[11:] + ["DeviceType", "DeviceInfo"]
HIST = ["card_seq", "prior_in_region", "prior_on_dev", "prior_pem", "prior_pcd", "prior_chan", "prior_med_amt", "prior_max_amt"]
BINS = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0]


def load_frame(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, list[str], list[str]]:
    cols = [r[0] for r in con.execute("DESCRIBE tx_raw").fetchall()]
    vcd = [c for c in cols if c[0] in "VCD" and c[1:].isdigit()]
    num_expr = ", ".join(f"TRY_CAST(t.{c} AS FLOAT) AS {c}" for c in vcd + ["TransactionAmt", "risk_score"])
    id_num = ", ".join(f"TRY_CAST(i.{c} AS FLOAT) AS {c}" for c in ID_NUM)
    id_cat = ", ".join(f"i.{c}" for c in ID_CAT)
    hist = ", ".join(f"f.{c}" for c in HIST)
    df = con.execute(f"""
        SELECT t.TransactionID::BIGINT AS TransactionID, t.ts::TIMESTAMP AS ts, t.channel,
               {num_expr}, {", ".join("t." + c for c in CAT_TX)}, {id_num}, {id_cat},
               (i.TransactionID IS NOT NULL)::INT AS has_id, {hist}
        FROM tx_raw t
        LEFT JOIN idn i USING (TransactionID)
        JOIN txn_feat f ON f.TransactionID = t.TransactionID::BIGINT
    """).fetchdf()
    df["amt_ratio"] = np.where(df.prior_med_amt > 0, df.TransactionAmt / df.prior_med_amt.replace(0, np.nan), np.nan)
    for c, src in (("region_new", "prior_in_region"), ("dev_new", "prior_on_dev"), ("pem_new", "prior_pem"),
                   ("pcd_new", "prior_pcd"), ("chan_new", "prior_chan")):
        df[c] = (df[src] == 0).astype(int)
    df["hour"] = df.ts.dt.hour
    df["dow"] = df.ts.dt.dayofweek
    fraud_ids = set(int(x) for (s,) in con.execute("SELECT txn_ids FROM cc WHERE outcome = 'confirmed_fraud'").fetchall() for x in s.split("|"))
    df["y"] = df.TransactionID.isin(fraud_ids).astype(int)
    catcols = CAT_TX + ID_CAT + ["channel"]
    for c in catcols:
        df[c] = df[c].astype("category")
    feats = [c for c in df.columns if c not in ("TransactionID", "ts", "y")]
    return df, feats, catcols


def fit(df, feats, catcols, rounds, valid=None):
    dtr = lgb.Dataset(df[feats], df.y, categorical_feature=catcols, free_raw_data=False)
    if valid is not None:
        dva = lgb.Dataset(valid[feats], valid.y, reference=dtr)
        return lgb.train(PARAMS, dtr, num_boost_round=rounds, valid_sets=[dva],
                         callbacks=[lgb.early_stopping(100, verbose=False)])
    return lgb.train(PARAMS, dtr, num_boost_round=rounds)


def calib_table(p, y):
    b = pd.cut(p, BINS, include_lowest=True)
    t = pd.DataFrame({"p": p, "y": y}).groupby(b, observed=True).agg(n=("y", "size"), fraud_rate=("y", "mean"), mean_p=("p", "mean"))
    return [{"bin": str(i), "n": int(r.n), "fraud_rate": round(float(r.fraud_rate), 4), "mean_p": round(float(r.mean_p), 4)} for i, r in t.iterrows()]


def save_calibrator(iso: IsotonicRegression, path: str) -> None:
    json.dump({"x": [float(v) for v in iso.X_thresholds_], "y": [float(v) for v in iso.y_thresholds_],
               "note": "isotonic regression fit on October out-of-fold raw cms_p; apply with numpy.interp(p, x, y)"},
              open(path, "w"), indent=1)


def load_calibrator(path: str):
    """Return cal(p) -> calibrated probability (numpy.interp over the isotonic thresholds)."""
    d = json.load(open(path))
    x, y = np.asarray(d["x"]), np.asarray(d["y"])
    return lambda p: float(np.interp(p, x, y))


def _bin_table(bins: list[dict], title: str) -> Table:
    """One calibration table: predicted-probability bin vs the fraud rate actually observed in it."""
    t = Table(
        Col("p bin", max_width=18),
        Col("rows", align="right", width=9),
        Col("mean p", align="right", width=8),
        Col("fraud rate", align="right", width=10),
        title=title,
    )
    for b in bins:
        t.add_row(b["bin"], f"{b['n']:,}", prob(b["mean_p"], digits=4), prob(b["fraud_rate"], digits=4))
    return t


def render_metrics(metrics: dict, elapsed: float | None = None) -> None:
    """Print the metrics dict as tables. Display only — data/models/cms_metrics.json is unchanged."""
    h = metrics["oct_holdout"]
    auc_ok = h["auc"] >= AUC_TARGET
    rule("October holdout (train Jul-Sep, early-stop on October)")
    t = Table(
        Col("metric", max_width=30),
        Col("value", align="right", width=8),
        Col("target", align="right", width=8),
        Col("result", width=6, align="center"),
        title="scorer quality",
    )
    t.add_row("ROC AUC", prob(h["auc"], digits=4), f">= {AUC_TARGET:.2f}",
              "PASS" if auc_ok else "FAIL", style=None if auc_ok else "red")
    t.add_row("average precision", prob(h["ap"], digits=4), "-", "-")
    t.add_row("base fraud rate", prob(h["base_rate"], digits=4), "-", "-")
    t.add_row("bank risk_score AUC (baseline)", prob(h["risk_score_auc"], digits=4), "-", "-")
    t.add_row("best_iteration", f"{metrics['best_iteration']:,}", "-", "-")
    t.print()
    if auc_ok:
        ok(f"October holdout AUC {h['auc']:.4f} >= target {AUC_TARGET:.2f} "
           f"(bank risk_score baseline {h['risk_score_auc']:.4f})")
    else:
        fail(f"October holdout AUC {h['auc']:.4f} is below the {AUC_TARGET:.2f} target")

    _bin_table(h["raw_bins"], "calibration, raw model probability").print()
    if h.get("cal_bins"):
        _bin_table(h["cal_bins"], "calibration after isotonic regression").print()

    rule("out-of-fold months (each month scored by a model that never saw it)")
    m = Table(Col("month", width=9), Col("AUC", align="right", width=8), title="month-wise OOF AUC")
    for month, auc in metrics["oof_month_auc"].items():
        m.add_row(month, prob(auc, digits=4) if auc is not None else "n/a")
    m.print()

    nd = metrics["novdec"]
    n = Table(Col("measure", max_width=28), Col("value", align="right", width=12),
              title="Nov-Dec scored by the final model")
    n.add_row("transactions", f"{nd['n']:,}")
    n.add_row("p > 0.5", f"{nd['p_gt_0.5']:,}")
    n.add_row("p > 0.3", f"{nd['p_gt_0.3']:,}")
    n.add_row("expected frauds (sum p)", f"{nd['sum_p']:,.1f}")
    n.print()

    rule("top features by gain")
    f = Table(Col("#", align="right", width=3), Col("feature", max_width=24),
              Col("gain", align="right", width=14), title="LightGBM feature importance")
    for i, (name, gain) in enumerate(metrics["top_features"], start=1):
        f.add_row(i, name, f"{float(gain):,.0f}")
    f.print()

    rule("the 20 case-pack flagged transactions")
    c = Table(
        Col("case", width=8),
        Col("card", width=10),
        Col("txn", align="right", width=10),
        Col("bank risk", align="right", width=9),
        Col("cms_p raw", align="right", width=9),
        Col("cms_p calibrated", align="right", width=16),
        title="cms_p vs the bank's own risk_score",
    )
    for r in metrics["case_pack"]:
        c.add_row(r["case"], r["card"], r["txn"], prob(r["risk"], digits=2),
                  prob(r["cms_p"], digits=4), prob(r["cal_p"], digits=3))
    c.print()
    if elapsed is not None:
        ok(f"training finished in {elapsed:.0f}s")


def train_all(db: str, out: str) -> dict:
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    con = duckdb.connect(db)
    con.execute("PRAGMA threads=8")
    step("loading the feature frame (Vesta V/C/D + identity + leakage-free card history)")
    df, feats, catcols = load_frame(con)
    ok(f"frame {df.shape[0]:,} rows x {df.shape[1]:,} columns in {time.time() - t0:.0f}s "
       f"({len(feats):,} features, {len(catcols)} categorical)")
    month = df.ts.dt.month
    tr, va, te = df[month <= 9], df[month == 10], df[month >= 11]
    # 1. holdout run
    step(f"holdout fit: train {len(tr):,} rows (Jul-Sep), early-stop on {len(va):,} October rows")
    m0 = fit(tr, feats, catcols, 2000, valid=va)
    best = int(m0.best_iteration)
    p_oct = m0.predict(va[feats], num_iteration=best)
    metrics = {"best_iteration": best,
               "oct_holdout": {"auc": round(roc_auc_score(va.y, p_oct), 4), "ap": round(average_precision_score(va.y, p_oct), 4),
                               "base_rate": round(float(va.y.mean()), 4), "risk_score_auc": round(roc_auc_score(va.y, va.risk_score.fillna(0)), 4),
                               "raw_bins": calib_table(p_oct, va.y.values)}}
    ok(f"holdout best_iter={best} October AUC={metrics['oct_holdout']['auc']:.4f} ({time.time() - t0:.0f}s)")
    if metrics["oct_holdout"]["auc"] < AUC_TARGET:
        warn(f"October AUC is below the {AUC_TARGET:.2f} target")
    # 2. isotonic calibrator on October OOF
    step("fitting the isotonic calibrator on the October out-of-fold predictions")
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(p_oct, va.y.values)
    save_calibrator(iso, os.path.join(out, "isotonic.json"))
    ok(f"calibrator -> {os.path.join(out, 'isotonic.json')}")
    metrics["oct_holdout"]["cal_bins"] = calib_table(iso.predict(p_oct), va.y.values)
    # 3. month-wise out-of-fold for Jul–Oct
    julOct = df[month <= 10]
    oof = pd.Series(np.nan, index=julOct.index)
    fold_auc = {}
    step("month-wise out-of-fold scoring of Jul-Oct (4 fits; no row is scored by a model trained on it)")
    for m in (7, 8, 9, 10):
        trn, tst = julOct[julOct.ts.dt.month != m], julOct[julOct.ts.dt.month == m]
        mm = fit(trn, feats, catcols, best)
        pm = mm.predict(tst[feats])
        oof.loc[tst.index] = pm
        fold_auc[f"2016-{m:02d}"] = round(roc_auc_score(tst.y, pm), 4) if tst.y.nunique() > 1 else None
        auc = fold_auc[f"2016-{m:02d}"]
        ok(f"oof 2016-{m:02d}: {len(tst):,} rows auc={auc if auc is not None else 'n/a'} ({time.time() - t0:.0f}s)")
    metrics["oof_month_auc"] = fold_auc
    # 4. final model on Jul–Oct -> Nov–Dec
    step("final fit on Jul-Oct, scoring Nov-Dec")
    final = fit(julOct, feats, catcols, best)
    p_te = final.predict(te[feats])
    final.save_model(os.path.join(out, "lgb_fraud.txt"))
    imp = pd.Series(final.feature_importance("gain"), index=feats).sort_values(ascending=False)
    metrics["top_features"] = [(k, round(float(v))) for k, v in imp.head(30).items()]
    metrics["novdec"] = {"n": int(len(te)), "p_gt_0.5": int((p_te > 0.5).sum()), "p_gt_0.3": int((p_te > 0.3).sum()), "sum_p": round(float(p_te.sum()), 1)}
    # 5. write cms_p into txn_feat
    scores = pd.concat([
        pd.DataFrame({"TransactionID": julOct.TransactionID.values, "cms_p": oof.values,
                      "cms_fold": [f"oof-2016-{m:02d}" for m in julOct.ts.dt.month]}),
        pd.DataFrame({"TransactionID": te.TransactionID.values, "cms_p": p_te, "cms_fold": "final"}),
    ])
    step("writing cms_p / cms_fold into txn_feat")
    con.register("_scores", scores)
    con.execute("UPDATE txn_feat SET cms_p = s.cms_p, cms_fold = s.cms_fold FROM _scores s WHERE txn_feat.TransactionID = s.TransactionID")
    con.unregister("_scores")
    n_null = con.execute("SELECT count(*) FROM txn_feat WHERE cms_p IS NULL").fetchone()[0]
    if n_null:
        fail(f"{n_null:,} txn_feat rows still have cms_p NULL")
    else:
        ok(f"every txn_feat row scored ({len(scores):,} rows written)")
    assert n_null == 0
    # case-pack flagged transactions (raw and calibrated)
    cal = load_calibrator(os.path.join(out, "isotonic.json"))
    rows = con.execute("""
        SELECT cp.case_id, cp.card_id, cp.flagged_txn_id, cp.risk_score, f.cms_p
        FROM cp JOIN txn_feat f ON f.TransactionID = cp.flagged_txn_id::BIGINT ORDER BY cp.case_id""").fetchall()
    metrics["case_pack"] = [{"case": r[0], "card": r[1], "txn": r[2], "risk": r[3], "cms_p": round(r[4], 4), "cal_p": round(cal(r[4]), 4)} for r in rows]
    json.dump(metrics, open(os.path.join(out, "cms_metrics.json"), "w"), indent=1, default=str)
    json.dump({"features": feats, "categorical": catcols}, open(os.path.join(out, "cms_features.json"), "w"), indent=1)
    render_metrics(metrics, elapsed=time.time() - t0)
    con.close()
    return metrics


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/hhgoa.duckdb")
    ap.add_argument("--out", default="data/models")
    a = ap.parse_args()
    header("etl.cms_train",
           "LightGBM case-memory scorer on the bank's own closed-case labels (seeded, deterministic)",
           {"db": a.db, "out": a.out, "learning rate": PARAMS["learning_rate"],
            "num_leaves": PARAMS["num_leaves"], "seed": PARAMS["seed"],
            "holdout": "October 2016", "AUC target": f">= {AUC_TARGET:.2f}"})
    m = train_all(a.db, a.out)
    summary("etl.cms_train complete",
            {"October AUC": f"{m['oct_holdout']['auc']:.4f}  (target >= {AUC_TARGET:.2f})",
             "best_iteration": m["best_iteration"],
             "model": os.path.join(a.out, "lgb_fraud.txt"),
             "calibrator": os.path.join(a.out, "isotonic.json"),
             "metrics": os.path.join(a.out, "cms_metrics.json")},
            status="ok" if m["oct_holdout"]["auc"] >= AUC_TARGET else "fail")
