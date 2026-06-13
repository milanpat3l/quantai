"""
model.py
========
ML signal-layer scaffolding (roadmap step 5). DELIBERATELY conservative:

* Features are built only from information available at day t (PCR levels +
  short changes), the target is the *next* day's price direction — no lookahead.
* Validation is strict **walk-forward** (expanding window, chronological). Never
  a random shuffle: with time series that leaks the future and inflates scores.
* It refuses to pretend: below a minimum sample count it reports "insufficient
  data" and compares against the majority-class baseline, so a model that only
  learned the base rate can't masquerade as signal.

This is the gate's downstream consumer — only meaningful once the daily logger
has accumulated enough history AND analysis.py shows a real lead-lag edge.

Usage
-----
    python model.py --underlying "NSE_INDEX|Nifty 50" --min-train 40
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

import config
config.load_env()
import plot_pcr

PCR_COLS = ["pcr", "pcr_m", "pcr_m_atm", "vol_pcr", "vol_pcr_atm"]


def build_features(df: pd.DataFrame, price_col: str = "fut") -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Return (X, y, dates). y = 1 if next-day price return > 0 else 0."""
    df = df.sort_values("date").reset_index(drop=True).copy()
    if price_col not in df or df[price_col].notna().sum() < 3:
        price_col = "spot"
    cols = [c for c in PCR_COLS if c in df.columns]

    feats = {}
    for c in cols:
        feats[c] = df[c]
        feats[f"{c}_chg"] = df[c].diff()          # day-over-day change
    X = pd.DataFrame(feats)

    fwd_ret = df[price_col].pct_change().shift(-1)  # return from t -> t+1
    y = (fwd_ret > 0).astype("Int64")

    keep = X.notna().all(axis=1) & y.notna()
    return X.loc[keep].reset_index(drop=True), y.loc[keep].astype(int).reset_index(drop=True), df["date"].loc[keep].reset_index(drop=True)


def walk_forward(X: pd.DataFrame, y: pd.Series, min_train: int = 40) -> dict:
    """Expanding-window walk-forward; predict each day from a model trained only
    on prior days. Returns metrics vs the majority-class baseline."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    n = len(X)
    if n < min_train + 5:
        return {"status": "insufficient", "n": n, "need": min_train + 5}

    preds, actuals = [], []
    for i in range(min_train, n):
        Xtr, ytr = X.iloc[:i], y.iloc[:i]
        if ytr.nunique() < 2:           # need both classes to fit
            continue
        clf = make_pipeline(StandardScaler(),
                            LogisticRegression(max_iter=1000, C=0.5))
        clf.fit(Xtr, ytr)
        preds.append(int(clf.predict(X.iloc[[i]])[0]))
        actuals.append(int(y.iloc[i]))

    if not preds:
        return {"status": "insufficient", "n": n, "need": min_train + 5}

    preds, actuals = np.array(preds), np.array(actuals)
    acc = float((preds == actuals).mean())
    majority = float(max(actuals.mean(), 1 - actuals.mean()))  # always-up / always-down
    return {"status": "ok", "n": n, "tested": len(preds), "accuracy": acc,
            "baseline": majority, "edge": acc - majority}


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward PCR -> next-day direction baseline")
    p.add_argument("--underlying", default="NSE_INDEX|Nifty 50")
    p.add_argument("--min-train", type=int, default=40,
                   help="minimum training days before the first prediction")
    p.add_argument("--price", default="fut", choices=["fut", "spot"])
    args = p.parse_args()

    df = plot_pcr.load(args.underlying)
    X, y, dates = build_features(df, args.price)
    res = walk_forward(X, y, args.min_train)

    if res["status"] == "insufficient":
        print(f"Insufficient data: have {res['n']} usable rows, need ~{res['need']}.")
        print("Keep the daily logger running — this is scaffolding, not a usable model yet.")
        return

    print(f"Walk-forward over {res['tested']} days (train>={args.min_train}):")
    print(f"  accuracy       : {res['accuracy']:.3f}")
    print(f"  majority baseline: {res['baseline']:.3f}")
    print(f"  edge over base : {res['edge']:+.3f}")
    if res["tested"] < 60 or abs(res["edge"]) < 0.03:
        print("\n⚠ Not a tradeable signal: too few samples and/or no edge over the "
              "base rate. Confirm a lead-lag edge (analysis.py) before trusting this.")


if __name__ == "__main__":
    main()
