"""
Backend service: listens to MQTT, decodes the 8-byte payload, stores it in SQLite
and prints (or sends to Telegram) an alert when the risk level rises or a fault starts.

Usage:
    python service.py [--db peat.db] [--idle 5]
Optional Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
Needs: pip install paho-mqtt
"""
import argparse
import json
import os
import queue
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

TOPIC = "peat/node1/uplink"
LEVELS = ["Normal", "Watch", "Alert", "Danger"]
FLAGS = ["OK", "MISSING", "SUSPECT", "FAULT"]


def decode(b):
    return dict(level=b[0] & 3, flag=b[0] >> 2,
                depth_cm=((b[1] << 8) | b[2]) / 10,
                moisture=((b[3] << 8) | b[4]) / 1000,
                days_to_danger=None if b[5] == 255 else b[5],
                battery_mv=(b[6] << 8) | b[7])


def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        try:
            urllib.request.urlopen(url, data, timeout=10)
        except Exception as e:
            print("telegram failed:", e)


outbox = queue.Queue()


def sender():
    # one worker sends alerts in order, so a slow Telegram call does not block MQTT
    while True:
        send_telegram(outbox.get())
        outbox.task_done()


def notify(text):
    print("ALERT:", text)
    outbox.put(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="peat.db")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--idle", type=float, default=0, help="stop after this many seconds without messages (0 = run forever)")
    a = ap.parse_args()

    if os.path.exists(a.db):
        os.remove(a.db)   # start fresh for each simulation run
    db = sqlite3.connect(a.db, check_same_thread=False)
    db.execute("""CREATE TABLE uplinks (step INT, sim_minutes INT, level INT, flag INT,
                  depth_cm REAL, moisture REAL, days_to_danger INT, battery_mv INT)""")
    db.execute("CREATE TABLE alerts (step INT, sim_minutes INT, text TEXT)")
    threading.Thread(target=sender, daemon=True).start()
    state = {"level": 0, "fault": False, "last": time.time(), "n": 0}

    def on_message(client, userdata, m):
        state["last"] = time.time()
        msg = json.loads(m.payload)
        p = decode(bytes.fromhex(msg["payload"]))
        db.execute("INSERT INTO uplinks VALUES (?,?,?,?,?,?,?,?)",
                   (msg["step"], msg["sim_minutes"], p["level"], p["flag"], p["depth_cm"],
                    p["moisture"], p["days_to_danger"], p["battery_mv"]))
        day = msg["sim_minutes"] / 1440
        text = None
        if p["level"] > state["level"] and p["level"] >= 2:
            text = (f"Day {day:.1f}: level {LEVELS[state['level']]} -> {LEVELS[p['level']]}, "
                    f"depth {p['depth_cm']:.1f} cm, days to 40 cm: {'n/a' if p['days_to_danger'] is None else p['days_to_danger']}")
        if p["flag"] == 3 and not state["fault"]:
            text = f"Day {day:.1f}: depth sensor fault, please check the node"
        state["fault"] = p["flag"] == 3
        state["level"] = p["level"]
        if text:
            db.execute("INSERT INTO alerts VALUES (?,?,?)", (msg["step"], msg["sim_minutes"], text))
            notify(text)
        if state["n"] % 50 == 0:
            db.commit()
        state["last"] = time.time()
        state["n"] += 1

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.on_message = on_message
    c.connect(a.host, a.port)
    c.subscribe(TOPIC, qos=1)
    c.loop_start()
    print("listening on", TOPIC)
    try:
        while True:
            time.sleep(0.5)
            if a.idle and state["n"] and time.time() - state["last"] > a.idle:
                break
    except KeyboardInterrupt:
        pass
    c.loop_stop()
    outbox.join()   # wait until all alerts are sent
    db.commit()
    print(f"stored {state['n']} uplinks in {a.db}")


if __name__ == "__main__":
    main()
