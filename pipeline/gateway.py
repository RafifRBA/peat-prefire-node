"""
Gateway stand-in: replays the uplinks the node would send and publishes them to MQTT.
Real LoRa is not simulated; this plays the role of the LoRaWAN gateway + network server.

Uses data/<scenario>.csv and data/<scenario>_node.csv (made by hydrology.py and node_logic.py).

Usage:
    python gateway.py --scenario sensor_faults [--host localhost] [--delay 0.003]
Needs: pip install paho-mqtt
"""
import argparse
import csv
import json
import os
import sys
import time

import numpy as np
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import node_logic as nl

TOPIC = "peat/node1/uplink"
BATTERY_MV = 3900


def encode(level, flag, depth, moist, days):
    """8-byte payload, same layout as the firmware."""
    d = int(round(depth * 10))
    m = int(round(moist * 1000))
    t = 255 if days is None else min(254, int(days))
    return bytes([level | (flag << 2), d >> 8, d & 255, m >> 8, m & 255, t,
                  BATTERY_MV >> 8, BATTERY_MV & 255])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--data-dir", default="../data")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--delay", type=float, default=0.003, help="seconds between messages (a small pause keeps the broker queue from overflowing)")
    a = ap.parse_args()

    raw = nl.load(os.path.join(a.data_dir, f"{a.scenario}.csv"))
    node = list(csv.DictReader(open(os.path.join(a.data_dir, f"{a.scenario}_node.csv"))))

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(a.host, a.port)
    c.loop_start()

    good, mgood = [], []
    sent = 0
    infos = []
    for r in node:
        i = int(r["step"])
        x, m = raw["depth"][i], raw["moist"][i]
        if r["flag"] == "OK" and not np.isnan(x):
            good.append((i, x))
        if not np.isnan(m) and 0 <= m <= 1:
            mgood.append(m)
        if r["uplink"] != "1" or not good:
            continue
        depth = float(np.median([v for _, v in good[-3:]]))
        moist = float(np.median(mgood[-3:])) if mgood else 0.0
        days = nl.trend_tcross(good, i)
        level = nl.NAMES.index(r["level"])
        flag = ["OK", "MISSING", "SUSPECT", "FAULT"].index(r["flag"])
        msg = {"step": i, "sim_minutes": i * nl.STEP_MIN,
               "payload": encode(level, flag, depth, moist, days).hex()}
        infos.append(c.publish(TOPIC, json.dumps(msg), qos=1))
        sent += 1
        if a.delay:
            time.sleep(a.delay)
    for inf in infos:
        inf.wait_for_publish()
    c.loop_stop()
    c.disconnect()
    print(f"published {sent} uplinks to {TOPIC}")


if __name__ == "__main__":
    main()
