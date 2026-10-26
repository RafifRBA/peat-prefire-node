"""
Makes synthetic peatland data: water-table depth, soil moisture and rain at
10-minute steps, then adds sensor noise, missing readings and optional faults.

All data is synthetic and the model constants are assumptions, not site data.

Usage:
    python hydrology.py --all               # all 4 scenarios, saved in ./data
    python hydrology.py --scenario dry_spell --days 90 --seed 1
    python hydrology.py --all --plot        # also save PNG plots (needs matplotlib)

Needs numpy.
"""
import argparse
import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

STEP_MIN = 10
STEPS_PER_DAY = 24 * 60 // STEP_MIN  # 144

# Water model (assumed values)
S_Y = 0.35            # specific yield
H_CANAL = -60.0       # canal water level, cm relative to the peat surface
K_DRAIN = 0.004       # drainage coefficient, per day
ET_MIN_FACTOR = 0.3   # evapotranspiration never falls below this share
ET_DEPTH_SCALE = 200.0  # cm; evapotranspiration shrinks as the water table gets deeper

# Soil moisture (assumed values)
THETA_SAT = 0.85        # m3/m3 when saturated
THETA_MIN = 0.30        # m3/m3, lowest value
THETA_DRY_RATE = 0.00291  # m3/m3 lost per dry day (taken from a peat study)
THETA_RAIN_GAIN = 0.02  # m3/m3 gained per 10 mm of rain
THETA_DEPTH_SLOPE = 0.0010  # the moisture ceiling drops by this much per cm of depth

# Sensor model
NOISE_DEPTH_CM = 0.3
NOISE_MOIST = 0.005
MISSING_RATE = 0.10   # SIPALAGA hourly availability is 76-93% (IPB study)

T0 = datetime(2026, 6, 1)  # start date is arbitrary


@dataclass
class Scenario:
    name: str
    days: int
    p_wet_start: float    # chance that a day is wet at the start
    p_wet_end: float      # chance that a day is wet at the end (changes steadily)
    rain_mean_mm: float   # mean rain on a wet day
    et_mm_day: float      # evapotranspiration per day
    h0_cm: float          # start water-table height (0 = surface, negative = below it)
    faults: bool = False


SCENARIOS = {
    "wet_season":    Scenario("wet_season",    60, 0.60, 0.60, 15.0, 3.0,  -5.0),
    "dry_spell":     Scenario("dry_spell",     90, 0.50, 0.03, 12.0, 4.5, -10.0),
    "long_drought":  Scenario("long_drought", 150, 0.30, 0.02, 10.0, 4.5, -15.0),
    "sensor_faults": Scenario("sensor_faults", 90, 0.50, 0.03, 12.0, 4.5, -10.0, True),
}


def make_rain(sc, rng):
    """Rain per step in mm. Each wet day gets one shower of 1-4 hours."""
    n = sc.days * STEPS_PER_DAY
    rain = np.zeros(n)
    for d in range(sc.days):
        frac = d / max(1, sc.days - 1)
        p_wet = sc.p_wet_start + (sc.p_wet_end - sc.p_wet_start) * frac
        if rng.random() < p_wet:
            total = rng.exponential(sc.rain_mean_mm)
            start = int(rng.integers(0, STEPS_PER_DAY - 24))
            length = int(rng.integers(6, 25))
            weights = rng.random(length)
            weights /= weights.sum()
            idx = d * STEPS_PER_DAY + start
            rain[idx:idx + length] += total * weights
    return rain


def et_pattern():
    """Share of daily evapotranspiration in each step (daylight only)."""
    hours = np.arange(STEPS_PER_DAY) * STEP_MIN / 60
    p = np.where((hours >= 6) & (hours < 18), np.sin(np.pi * (hours - 6) / 12), 0.0)
    return p / p.sum()


def run_water(sc, rain):
    """Water balance: h(t+1) = min(0, h + (rain - ET - drainage) / S_Y), in cm."""
    n = len(rain)
    pattern = et_pattern()
    day_rain = rain.reshape(-1, STEPS_PER_DAY).sum(axis=1)
    h = np.empty(n)
    theta = np.empty(n)
    h_now = sc.h0_cm
    th = 0.80
    for i in range(n):
        d = i // STEPS_PER_DAY
        depth = -h_now
        et_factor = max(ET_MIN_FACTOR, 1.0 - depth / ET_DEPTH_SCALE)
        et_cm = sc.et_mm_day * pattern[i % STEPS_PER_DAY] * et_factor / 10.0
        rain_cm = rain[i] / 10.0
        drain_cm = max(0.0, K_DRAIN * (h_now - H_CANAL)) / STEPS_PER_DAY
        h_now = min(0.0, h_now + (rain_cm - et_cm - drain_cm) / S_Y)

        th += THETA_RAIN_GAIN * rain_cm
        if day_rain[d] < 0.1:  # dry day
            th -= THETA_DRY_RATE / STEPS_PER_DAY
        upper = min(THETA_SAT, THETA_SAT - THETA_DEPTH_SLOPE * (-h_now))
        th = min(max(th, THETA_MIN), upper)

        h[i] = h_now
        theta[i] = th
    return h, theta


def inject_faults(depth, label, rng):
    """Add spikes, stuck values, dropouts, and a drift to the depth sensor."""
    n = len(depth)
    busy = np.zeros(n, dtype=bool)
    margin = STEPS_PER_DAY // 4

    def place(length):
        for _ in range(200):
            s = int(rng.integers(0, n - length))
            if not busy[max(0, s - margin): s + length + margin].any():
                busy[s: s + length] = True
                return s
        return None

    length = 20 * STEPS_PER_DAY  # slow drift, up to +10 cm over 20 days
    s = place(length)
    if s is not None:
        depth[s: s + length] += np.linspace(0, 10.0, length)
        label[s: s + length] = "drift"

    for _ in range(n // (2 * STEPS_PER_DAY)):  # about one spike every 2 days
        s = place(1)
        if s is not None:
            depth[s] += rng.choice([-1, 1]) * rng.uniform(30, 80)
            label[s] = "spike"

    for _ in range(3):  # stuck value for 12-36 hours
        length = int(rng.integers(72, 217))
        s = place(length)
        if s is not None:
            depth[s: s + length] = depth[s]
            label[s: s + length] = "stuck"

    for _ in range(2):  # no data for 6-24 hours
        length = int(rng.integers(36, 145))
        s = place(length)
        if s is not None:
            depth[s: s + length] = np.nan
            label[s: s + length] = "dropout"

    return depth, label


def sensor_readings(depth_true, theta_true, rng, faults):
    n = len(depth_true)
    depth = depth_true + rng.normal(0, NOISE_DEPTH_CM, n)
    moist = theta_true + rng.normal(0, NOISE_MOIST, n)
    label = np.array(["none"] * n, dtype=object)
    if faults:
        depth, label = inject_faults(depth, label, rng)

    # readings that go missing at random (not counted as faults)
    miss_d = rng.random(n) < MISSING_RATE
    miss_m = rng.random(n) < MISSING_RATE
    label[miss_d & (label == "none")] = "missing"
    depth[miss_d] = np.nan
    moist[miss_m] = np.nan
    return np.round(depth, 1), np.round(moist, 3), label


def write_csv(path, rain, h, theta, depth_m, moist_m, label):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["step", "time", "rain_mm", "depth_true_cm", "moisture_true",
                    "depth_meas_cm", "moisture_meas", "depth_fault"])
        for i in range(len(rain)):
            t = T0 + timedelta(minutes=STEP_MIN * i)
            w.writerow([
                i, t.isoformat(timespec="minutes"), f"{rain[i]:.3f}",
                f"{-h[i]:.2f}", f"{theta[i]:.4f}",
                "" if np.isnan(depth_m[i]) else f"{depth_m[i]:.1f}",
                "" if np.isnan(moist_m[i]) else f"{moist_m[i]:.3f}",
                label[i],
            ])


def plot(path, sc, rain, depth_true, theta, depth_m):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed; skipping the plot")
        return
    days = np.arange(len(depth_true)) / STEPS_PER_DAY
    fig, ax = plt.subplots(3, 1, figsize=(9, 7), sharex=True)
    ax[0].plot(days, depth_m, ".", ms=1, color="tab:gray", label="measured")
    ax[0].plot(days, depth_true, color="tab:blue", label="true")
    ax[0].axhline(40, color="tab:red", ls="--", lw=1, label="40 cm")
    ax[0].invert_yaxis()
    ax[0].set_ylabel("water-table depth (cm)")
    ax[0].legend(loc="lower left", fontsize=8)
    ax[1].plot(days, theta, color="tab:green")
    ax[1].set_ylabel("soil moisture (m3/m3)")
    daily_rain = rain.reshape(-1, STEPS_PER_DAY).sum(axis=1)
    ax[2].bar(np.arange(len(daily_rain)) + 0.5, daily_rain, width=1.0, color="tab:blue")
    ax[2].set_ylabel("rain (mm/day)")
    ax[2].set_xlabel("day (synthetic data)")
    fig.suptitle(f"Scenario: {sc.name} (synthetic)")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def run(name, days, seed, out_dir, do_plot):
    sc = SCENARIOS[name]
    if days:
        sc = Scenario(**{**sc.__dict__, "days": days})
    rng = np.random.default_rng(seed)
    rain = make_rain(sc, rng)
    h, theta = run_water(sc, rain)
    depth_true = -h
    depth_m, moist_m, label = sensor_readings(depth_true, theta, rng, sc.faults)

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"{name}.csv")
    write_csv(csv_path, rain, h, theta, depth_m, moist_m, label)
    if do_plot:
        plot(os.path.join(out_dir, f"{name}.png"), sc, rain, depth_true, theta, depth_m)

    crossed = np.where(depth_true >= 40.0)[0]
    first = f"day {crossed[0] / STEPS_PER_DAY:.1f}" if len(crossed) else "never"
    print(f"{name:14s} days={sc.days:3d} seed={seed}  max depth={depth_true.max():5.1f} cm  "
          f"reaches 40 cm: {first}  missing depth={np.isnan(depth_m).mean():.0%}  -> {csv_path}")


def main():
    ap = argparse.ArgumentParser(description="Synthetic peat-hydrology generator")
    ap.add_argument("--scenario", choices=SCENARIOS.keys())
    ap.add_argument("--all", action="store_true", help="make every scenario")
    ap.add_argument("--days", type=int, default=0, help="override the number of days")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args()

    if args.all:
        for i, name in enumerate(SCENARIOS):
            run(name, args.days, args.seed + i, args.out_dir, args.plot)
    elif args.scenario:
        run(args.scenario, args.days, args.seed, args.out_dir, args.plot)
    else:
        ap.error("use --scenario NAME or --all")


if __name__ == "__main__":
    main()
