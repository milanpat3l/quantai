"""
model.py
========
ML signal-layer for the OI-PCR model (roadmap step 5). Conservative by design:

* Features use only information available at day t (PCR levels + short changes);
  the target is the *next* day's price direction — no lookahead.
* Validation is strict **walk-forward** (expanding window, chronological). A
  random shuffle would leak the future and inflate scores — never used.
* Models: logistic (linear baseline), decision tree, random forest. Tree models
  especially MEMORISE tiny datasets, so we report accuracy against the
  majority-class baseline and refuse to emit a "signal" until there are enough
  out-of-sample test days AND a real edge over that baseline.

Only trustworthy once the daily logger has months of history and analysis.py
shows a genuine lead-lag edge.

Usage
-----
    python model.py --underlying "NSE_INDEX|Nifty 50" --model forest --min-train 40
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

import config
config.load_env()
import plot_pcr

PCR_COLS = ["pcr", "pcr_m", "pcr_m_atm", "vol_pcr", "vol_pcr_atm"]
MODELS = ("logistic", "tree", "forest")
MIN_CREDIBLE_TESTS = 60     # out-of-sample days before a "signal" is shown
MIN_EDGE = 0.03             # accuracy must beat the base rate by this much


def build_features(df: pd.DataFrame, price_col: str = "fut"):
    """Return (X, y, dates). X keeps the most recent row (whose y is unknown)
    so we can predict the next session; y is NaN there."""
    df = df.sort_values("date").reset_index(drop=True).copy()
    if price_col not in df or df[price_col].notna().sum() < 3:
        price_col = "spot"
    cols = [c for c in PCR_COLS if c in df.columns]

    feats = {}
    for c in cols:
        feats[c] = df[c]
        feats[f"{c}_chg"] = df[c].diff()
    X = pd.DataFrame(feats)
    y = (df[price_col].pct_change().shift(-1) > 0).astype("float")  # NaN on last row
    y[df[price_col].pct_change().shift(-1).isna()] = np.nan

    valid = X.notna().all(axis=1)
    return (X.loc[valid].reset_index(drop=True),
            y.loc[valid].reset_index(drop=True),
            df["date"].loc[valid].reset_index(drop=True))


def _make_model(name: str):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if name == "logistic":
        from sklearn.linear_model import LogisticRegression
        return make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, C=0.5))
    if name == "tree":
        from sklearn.tree import DecisionTreeClassifier
        return DecisionTreeClassifier(max_depth=3, min_samples_leaf=5, random_state=0)
    if name == "forest":
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(n_estimators=200, max_depth=4,
                                      min_samples_leaf=5, random_state=0, n_jobs=-1)
    raise ValueError(f"unknown model '{name}'")


def walk_forward(X: pd.DataFrame, y: pd.Series, min_train: int = 40,
                 model: str = "logistic") -> dict:
    """Expanding-window walk-forward over rows with a known label."""
    known = y.notna()
    Xk, yk = X.loc[known].reset_index(drop=True), y.loc[known].astype(int).reset_index(drop=True)
    n = len(Xk)
    if n < min_train + 5:
        return {"status": "insufficient", "n": n, "need": min_train + 5}

    preds, actuals = [], []
    for i in range(min_train, n):
        if yk.iloc[:i].nunique() < 2:
            continue
        clf = _make_model(model)
        clf.fit(Xk.iloc[:i], yk.iloc[:i])
        preds.append(int(clf.predict(Xk.iloc[[i]])[0]))
        actuals.append(int(yk.iloc[i]))
    if not preds:
        return {"status": "insufficient", "n": n, "need": min_train + 5}

    preds, actuals = np.array(preds), np.array(actuals)
    acc = float((preds == actuals).mean())
    base = float(max(actuals.mean(), 1 - actuals.mean()))
    return {"status": "ok", "model": model, "n": n, "tested": len(preds),
            "accuracy": acc, "baseline": base, "edge": acc - base}


def latest_call(X: pd.DataFrame, y: pd.Series, model: str = "logistic",
                min_train: int = 40) -> dict:
    """Train on all labelled history, predict the next session's direction.
    Only meant to be acted on when walk_forward shows a credible edge."""
    known = y.notna()
    Xk, yk = X.loc[known], y.loc[known].astype(int)
    if known.sum() < min_train or yk.nunique() < 2:
        return {"status": "insufficient"}
    clf = _make_model(model)
    clf.fit(Xk, yk)
    x_last = X.iloc[[-1]]                       # most recent features (y unknown)
    pred = int(clf.predict(x_last)[0])
    prob = None
    if hasattr(clf, "predict_proba"):
        prob = float(clf.predict_proba(x_last)[0][pred])
    return {"status": "ok", "direction": "UP" if pred == 1 else "DOWN", "prob": prob}


def credible(res: dict) -> bool:
    return (res.get("status") == "ok" and res.get("tested", 0) >= MIN_CREDIBLE_TESTS
            and res.get("edge", 0) >= MIN_EDGE)


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward PCR -> next-day direction")
    p.add_argument("--underlying", default="NSE_INDEX|Nifty 50")
    p.add_argument("--model", default="logistic", choices=MODELS)
    p.add_argument("--min-train", type=int, default=40)
    p.add_argument("--price", default="fut", choices=["fut", "spot"])
    args = p.parse_args()

    df = plot_pcr.load(args.underlying)
    X, y, _ = build_features(df, args.price)
    res = walk_forward(X, y, args.min_train, args.model)

    if res["status"] == "insufficient":
        print(f"Insufficient data: {res['n']} labelled rows, need ~{res['need']}.")
        print("Keep the daily logger running — scaffolding, not a usable model yet.")
        return

    print(f"[{res['model']}] walk-forward over {res['tested']} days "
          f"(train>={args.min_train}):")
    print(f"  accuracy        : {res['accuracy']:.3f}")
    print(f"  majority baseline: {res['baseline']:.3f}")
    print(f"  edge over base  : {res['edge']:+.3f}")
    if credible(res):
        call = latest_call(X, y, args.model, args.min_train)
        if call["status"] == "ok":
            pr = f" (p={call['prob']:.2f})" if call["prob"] is not None else ""
            print(f"  next-session call: {call['direction']}{pr}")
    else:
        print("\n⚠ Not a tradeable signal yet: too few out-of-sample days and/or "
              "no edge over the base rate. This is expected until months of data "
              "accumulate and the lead-lag check (analysis.py) shows an edge.")


if __name__ == "__main__":
    main()
