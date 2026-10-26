"""
Energy estimate for the node: adaptive sampling vs fixed sampling.

Reads data/<scenario>_node.csv (made by node_logic.py) and works out how much
battery the node uses per day.

Usage:  python energy.py [--data-dir data]
Needs only the standard library.
"""
import argparse
import csv
import math
import os

# Currents come from datasheets where one exists, otherwise they are my own estimate.
# Supply is 3.3 V.

# ESP32 datasheet, "Power Consumption by Power Modes":
I_DEEP_SLEEP_MA = 0.010   # deep sleep, RTC timer + RTC memory
I_MCU_MA = 31.0           # CPU on, radios off, 80 MHz (upper end of the 20-31 mA range)

# SX1276 datasheet (Semtech), power consumption tables:
I_LORA_SLEEP_MA = 0.0002  # sleep
I_LORA_TX_MA = 28.0       # LoRa transmit, +13 dBm
I_LORA_RX_MA = 10.3       # LoRa receive, 125 kHz

# Capacitive soil moisture sensor (product page): 5 mA at 3.3-5.5 V
I_MOIST_MA = 5.0

# Pressure sensor: Novus WL420 style 4-20 mA loop sensor, powered from 12-36 V.
# Worst case: 20 mA at 12 V, fed from a boost converter. 85% efficiency is my guess.
BOOST_EFF = 0.85
I_PRESSURE_MA = 12.0 * 20.0 / (3.3 * BOOST_EFF)   # about 85 mA at 3.3 V

# My own estimates (not in datasheets):
T_MEASURE_S = 2.0         # ESP32 awake for one measurement
T_SENSOR_S = 1.5          # sensors powered per measurement (warm-up + reading)
T_RX_S = 0.3              # two LoRaWAN receive windows
T_UPLINK_MCU_S = 1.5      # ESP32 awake around one uplink
BATTERY_MAH = 3000 * 0.8  # one 18650 cell, 80% usable

# LoRaWAN airtime: 13 bytes protocol overhead + 8 bytes of our data
SF, BW_HZ, PAYLOAD_B = 10, 125000, 21

# Baseline for comparison: measure every 10 min, send every hour
BASE_MEASURE_MIN, BASE_SEND_MIN = 10, 60

I_SLEEP_MA = I_DEEP_SLEEP_MA + I_LORA_SLEEP_MA


def airtime_s(sf, bw, payload):
    # Semtech's airtime formula (explicit header, CRC on, coding rate 4/5)
    tsym = (2 ** sf) / bw
    de = 1 if tsym > 0.016 else 0
    n = 8 + max(math.ceil((8 * payload - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * 5, 0)
    return (12.25 + n) * tsym


def mah_per_measure():
    return (I_MCU_MA * T_MEASURE_S + (I_MOIST_MA + I_PRESSURE_MA) * T_SENSOR_S) / 3600


def mah_per_uplink(sf=SF):
    air = airtime_s(sf, BW_HZ, PAYLOAD_B)
    return (I_LORA_TX_MA * air + I_LORA_RX_MA * T_RX_S + I_MCU_MA * T_UPLINK_MCU_S) / 3600


def mah_sleep(days):
    return I_SLEEP_MA * 24 * days


def load_counts(path):
    n = u = 0
    for r in csv.DictReader(open(path)):
        n += 1
        u += int(r["uplink"])
    return n, u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    a = ap.parse_args()
    days = {"wet_season": 60, "dry_spell": 90, "long_drought": 150, "sensor_faults": 90}

    print(f"LoRa airtime SF{SF}: {airtime_s(SF, BW_HZ, PAYLOAD_B) * 1000:.0f} ms per uplink "
          f"(SF7: {airtime_s(7, BW_HZ, PAYLOAD_B) * 1000:.0f} ms)")
    print(f"Per measurement: {mah_per_measure() * 1000:.1f} uAh;  per uplink: {mah_per_uplink() * 1000:.1f} uAh;  "
          f"sleep: {I_SLEEP_MA * 24:.2f} mAh/day\n")
    print(f"{'scenario':14s} {'adaptive mAh/d':>15s} {'fixed mAh/d':>12s} {'saving':>7s} "
          f"{'life adaptive':>14s} {'life fixed':>11s}")
    for nm, d in days.items():
        path = os.path.join(a.data_dir, f"{nm}_node.csv")
        if not os.path.exists(path):
            print(f"{nm}: run node_logic.py first")
            continue
        n, u = load_counts(path)
        ad = (n * mah_per_measure() + u * mah_per_uplink() + mah_sleep(d)) / d
        bm = d * 24 * 60 / BASE_MEASURE_MIN
        bu = d * 24 * 60 / BASE_SEND_MIN
        fx = (bm * mah_per_measure() + bu * mah_per_uplink() + mah_sleep(d)) / d
        print(f"{nm:14s} {ad:15.2f} {fx:12.2f} {100 * (1 - ad / fx):6.0f}% "
              f"{BATTERY_MAH / ad:12.0f} d {BATTERY_MAH / fx:9.0f} d")

    print("\nEffect of spreading factor (dry_spell):")
    path = os.path.join(a.data_dir, "dry_spell_node.csv")
    if os.path.exists(path):
        n, u = load_counts(path)
        for sf in (7, 9, 10, 12):
            ad = (n * mah_per_measure() + u * mah_per_uplink(sf) + mah_sleep(90)) / 90
            print(f"  SF{sf:2d}: {ad:.2f} mAh/day -> {BATTERY_MAH / ad:.0f} days on battery alone")
    print("\nThe node also has a solar panel, so battery-only life is the worst case.")


if __name__ == "__main__":
    main()
