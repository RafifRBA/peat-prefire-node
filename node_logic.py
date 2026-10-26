"""
Plays a CSV from hydrology.py through the logic the ESP32 would run:
data checks, risk level, time to danger and adaptive sampling.
Prints the numbers used for experiments E1, E2 and E3.

Usage:
    python node_logic.py --all
    python node_logic.py --scenario dry_spell --plot
Needs numpy (and matplotlib for --plot).
"""
import argparse
import csv
import os

import numpy as np

STEP_MIN = 10
STEPS_PER_DAY = 144

# Risk levels: 0 Normal, 1 Watch, 2 Alert, 3 Danger
NAMES = ["Normal", "Watch", "Alert", "Danger"]
MEASURE_MIN = [60, 30, 10, 10]    # measure every (minutes)
SEND_MIN = [360, 180, 60, 30]     # send every (minutes)
D_WATCH, D_ALERT, D_DANGER = 25.0, 35.0, 40.0   # depth limits in cm
T_WATCH_DAYS, T_ALERT_DAYS = 7.0, 2.0           # predicted days to 40 cm
MOIST_CRIT = 0.76                 # soil moisture below this means Danger
TREND_DAYS = 5                    # days used to fit the trend
MIN_SLOPE = 0.3                   # cm per day; flatter than this counts as no trend
UP_COUNT, DOWN_COUNT = 2, 3       # readings needed to go up / down a level
HYST_CM = 2.0                     # a level falls only 2 cm below its limit
HYST_MOIST = 0.01                 # same idea for moisture

# Data checks
RANGE_CM = (0.0, 150.0)
MAX_JUMP_CM = 15.0                # max change from the last good reading
STUCK_N, STUCK_TOL = 6, 0.05      # 6 equal readings in a row = stuck
GAP_FAULT_MIN = 240               # no data for 4 hours = fault

# Baseline for comparison: measure every 10 min, send every hour
BASE_MEASURE_MIN = 10
BASE_SEND_MIN = 60


def load(path):
    rows = list(csv.DictReader(open(path)))
    f = lambda k: np.array([float(r[k]) if r[k] != "" else np.nan for r in rows])
    return dict(depth_true=f("depth_true_cm"), depth=f("depth_meas_cm"),
                moist=f("moisture_meas"), label=np.array([r["depth_fault"] for r in rows]))


def trend_tcross(good, i):
    """Days until depth reaches 40 cm, from a line through the daily minimum of the last 5 days."""
    day = i // STEPS_PER_DAY
    mins = {}
    for j, d in good:
        dj = j // STEPS_PER_DAY
        if day - TREND_DAYS < dj <= day:
            mins[dj] = min(mins.get(dj, 1e9), d)
    if len(mins) < 3:
        return None
    x = np.array(sorted(mins))
    y = np.array([mins[k] for k in x])
    slope = np.polyfit(x, y, 1)[0]
    if slope < MIN_SLOPE:
        return None
    return max(0.0, (D_DANGER - y[-1]) / slope)


def candidate(d, m, tc, level=0):
    # A level that is already on needs a small margin before it can turn off
    h = lambda k: HYST_CM if level >= k else 0.0
    if d >= D_DANGER - h(3) or (m is not None and m < MOIST_CRIT + (HYST_MOIST if level >= 3 else 0.0)):
        return 3
    if d >= D_ALERT - h(2) or (tc is not None and tc <= T_ALERT_DAYS):
        return 2
    if d >= D_WATCH - h(1) or (tc is not None and tc <= T_WATCH_DAYS):
        return 1
    return 0


def simulate(data):
    n = len(data["depth"])
    level, up, down = 0, 0, 0
    good, raw, mgood = [], [], []     # accepted depths, all raw depths, accepted moisture
    last_valid_i = 0
    fault_on = False
    log = []                          # one row per measurement
    uplinks = 0
    last_send = 0
    i = 0
    while i < n:
        x, m = data["depth"][i], data["moist"][i]
        flag = "OK"
        if np.isnan(x):
            flag = "MISSING"
            if (i - last_valid_i) * STEP_MIN >= GAP_FAULT_MIN:
                flag = "FAULT"
        else:
            last_valid_i = i
            raw.append(x)
            if len(raw) >= STUCK_N and max(raw[-STUCK_N:]) - min(raw[-STUCK_N:]) < STUCK_TOL:
                flag = "FAULT"
            elif not (RANGE_CM[0] <= x <= RANGE_CM[1]):
                flag = "SUSPECT"
            elif good and abs(x - good[-1][1]) > MAX_JUMP_CM:
                flag = "SUSPECT"
            if flag == "OK":
                good.append((i, x))
        if not np.isnan(m) and 0 <= m <= 1:
            mgood.append(m)

        sent = False
        if flag == "FAULT":
            if not fault_on:
                sent = True           # report the fault once
            fault_on = True
        else:
            if flag == "OK":
                fault_on = False
            if good and (flag == "OK" or fault_on is False):
                d = float(np.median([v for _, v in good[-3:]]))
                mm = float(np.median(mgood[-3:])) if mgood else None
                cand = candidate(d, mm, trend_tcross(good, i), level)
                if cand > level:
                    up, down = up + 1, 0
                    if up >= UP_COUNT:
                        level, up, sent = cand, 0, True
                elif cand < level:
                    down, up = down + 1, 0
                    if down >= DOWN_COUNT:
                        level, down, sent = level - 1, 0, True
                else:
                    up = down = 0

        if (i - last_send) * STEP_MIN >= SEND_MIN[level]:
            sent = True
        if sent:
            uplinks += 1
            last_send = i
        log.append((i, level, flag, sent))
        i += MEASURE_MIN[level] // STEP_MIN
    return log, uplinks


def episodes(flags_per_step):
    """Start and end of each run of True."""
    out, s = [], None
    for k, v in enumerate(flags_per_step):
        if v and s is None:
            s = k
        if not v and s is not None:
            out.append((s, k))
            s = None
    if s is not None:
        out.append((s, len(flags_per_step)))
    return out


def lvl_at_steps(log, n):
    """Level at every 10-minute step (keeps the last value until the next measurement)."""
    out = np.zeros(n, dtype=int)
    for k, (i, l, _, _) in enumerate(log):
        j = log[k + 1][0] if k + 1 < len(log) else n
        out[i:j] = l
    return out


def report(name, data, log, uplinks):
    n = len(data["depth"])
    days = n / STEPS_PER_DAY
    per_step = lvl_at_steps(log, n)
    print(f"\n=== {name} ({days:.0f} days) ===")

    # E1: how much work the node does
    base_m, base_u = n // (BASE_MEASURE_MIN // STEP_MIN), int(days * 24 * 60 / BASE_SEND_MIN)
    print(f"E1 measurements: adaptive {len(log)} vs fixed {base_m} "
          f"({100 * (1 - len(log) / base_m):.0f}% fewer); "
          f"uplinks: adaptive {uplinks} vs fixed {base_u} ({100 * (1 - uplinks / base_u):.0f}% fewer)")
    print("   time in level (% of days): " +
          ", ".join(f"{NAMES[k]} {100 * np.mean(per_step == k):.0f}%" for k in range(4)))

    # E2: how early each level turns on before the true depth reaches 40 cm
    cross = np.where(data["depth_true"] >= D_DANGER)[0]
    if len(cross):
        tc = cross[0]
        for lab, k in (("Watch", 1), ("Alert", 2), ("Danger", 3)):
            ep = [e for e in episodes(per_step >= k) if e[1] > tc]
            if ep:
                s = ep[0][0]
                print(f"E2 {lab} on at day {s / STEPS_PER_DAY:.1f}; true 40 cm at day "
                      f"{tc / STEPS_PER_DAY:.1f}; lead {(tc - s) / STEPS_PER_DAY:+.1f} days "
                      f"(negative = late)")
            else:
                print(f"E2 {lab}: never on after 40 cm")
    else:
        print(f"E2 true depth never reaches 40 cm. Steps at Watch or higher: "
              f"{100 * np.mean(per_step >= 1):.1f}%, Alert or higher: {100 * np.mean(per_step >= 2):.1f}%")

    # E3: fault detection (only for scenarios with faults)
    lab = data["label"]
    if np.isin(lab, ["spike", "stuck", "dropout", "drift"]).any():
        flagged = {r[0]: r[2] for r in log}
        print("E3 fault detection (events with at least one reading the node took):")
        for typ in ("spike", "stuck", "dropout", "drift"):
            ev = episodes(lab == typ)
            ok = (lambda k: True) if typ == "dropout" else (lambda k: not np.isnan(data["depth"][k]))
            sampled = [(s, e) for s, e in ev if any(k in flagged and ok(k) for k in range(s, e))]
            hit = [(s, e) for s, e in sampled
                   if any(flagged.get(k) in ("SUSPECT", "FAULT") for k in range(s, e))]
            print(f"   {typ:8s} events {len(ev):3d}, sampled {len(sampled):3d}, detected {len(hit):3d}")
        clean = [r for r in log if lab[r[0]] == "none" and not np.isnan(data["depth"][r[0]])]
        fa = sum(1 for r in clean if r[2] in ("SUSPECT", "FAULT"))
        print(f"   false flags on clean readings: {fa} of {len(clean)}")
        wrong = (per_step >= 2) & (data["depth_true"] < D_ALERT - 5)
        print(f"   steps at Alert/Danger while true depth < {D_ALERT - 5:.0f} cm: {wrong.sum()} "
              f"({'none caused by faults' if wrong.sum() == 0 else 'check these'})")


def plot(path, name, data, log):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed, skipping the plot")
        return
    n = len(data["depth"])
    days = np.arange(n) / STEPS_PER_DAY
    lv = lvl_at_steps(log, n)
    fig, ax = plt.subplots(2, 1, figsize=(9, 5.5), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax[0].plot(days, data["depth_true"], color="tab:blue", label="true depth")
    ax[0].axhline(D_DANGER, color="tab:red", ls="--", lw=1, label="40 cm")
    ax[0].invert_yaxis()
    ax[0].set_ylabel("water-table depth (cm)")
    ax[0].legend(loc="lower left", fontsize=8)
    ax[1].step(days, lv, where="post", color="tab:orange")
    ax[1].set_yticks(range(4))
    ax[1].set_yticklabels(NAMES)
    ax[1].set_xlabel("day (synthetic data)")
    fig.suptitle(f"Node risk level: {name}")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--plot", action="store_true")
    a = ap.parse_args()
    names = ["wet_season", "dry_spell", "long_drought", "sensor_faults"] if a.all else [a.scenario]
    if not names[0]:
        ap.error("use --scenario NAME or --all")
    for nm in names:
        data = load(os.path.join(a.data_dir, f"{nm}.csv"))
        log, up = simulate(data)
        report(nm, data, log, up)
        with open(os.path.join(a.data_dir, f"{nm}_node.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["step", "level", "flag", "uplink"])
            for r in log:
                w.writerow([r[0], NAMES[r[1]], r[2], int(r[3])])
        if a.plot:
            plot(os.path.join(a.data_dir, f"{nm}_node.png"), nm, data, log)


if __name__ == "__main__":
    main()
