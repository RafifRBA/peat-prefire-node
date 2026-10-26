"""
Draws the stored uplinks from SQLite: water-table depth with the risk level, and alerts.

Usage:  python charts.py [--db peat.db] [--out pipeline_chart.png]
Needs: matplotlib
"""
import argparse
import sqlite3

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLORS = ["tab:green", "gold", "darkorange", "tab:red"]
NAMES = ["Normal", "Watch", "Alert", "Danger"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="peat.db")
    ap.add_argument("--out", default="pipeline_chart.png")
    a = ap.parse_args()
    db = sqlite3.connect(a.db)
    rows = db.execute("SELECT sim_minutes, level, flag, depth_cm FROM uplinks ORDER BY step").fetchall()
    alerts = db.execute("SELECT sim_minutes, text FROM alerts ORDER BY step").fetchall()
    days = [r[0] / 1440 for r in rows]
    depth = [r[3] for r in rows]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(days, depth, color="gray", lw=0.8)
    for k in range(4):
        pts = [(d, x) for d, x, r in zip(days, depth, rows) if r[1] == k]
        if pts:
            ax.scatter(*zip(*pts), s=10, color=COLORS[k], label=NAMES[k], zorder=3)
    ax.axhline(40, color="tab:red", ls="--", lw=1)
    for m, _ in alerts:
        ax.axvline(m / 1440, color="black", alpha=0.25, lw=0.8)
    ax.invert_yaxis()
    ax.set_xlabel("day (synthetic data)")
    ax.set_ylabel("water-table depth (cm)")
    ax.set_title(f"Uplinks stored in SQLite ({len(rows)} messages, {len(alerts)} alerts as vertical lines)")
    ax.legend(loc="lower left", fontsize=8)
    fig.tight_layout()
    fig.savefig(a.out, dpi=150)
    print("saved", a.out)


if __name__ == "__main__":
    main()
