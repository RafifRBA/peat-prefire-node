# peat-prefire-node

A low-cost, solar-powered node that warns villages when peat is drying out, before a fire starts. It measures water-table depth, soil moisture and rain, checks its own readings, sets one of four risk levels (Normal, Watch, Alert, Danger) and sends the result over LoRaWAN.

This repo is the simulation behind the report *Pre-Fire Risk Warning for Peatlands*. Everything runs on synthetic data, and the model constants are assumptions, not site data.

## Files

| Path | What it does |
|---|---|
| `hydrology.py` | Generates synthetic depth, moisture and rain (10-minute steps) with noise, missing readings and optional sensor faults. Model constants are at the top of the file. |
| `node_logic.py` | Python version of the ESP32 logic: data checks, risk level, time to danger, adaptive sampling. Prints the E1 to E3 numbers. |
| `energy.py` | Battery use per day, adaptive vs fixed sampling. |
| `pipeline/` | `gateway.py` publishes uplinks to MQTT, `service.py` decodes them into SQLite and raises alerts, `charts.py` draws the result. |
| `data/` | Generated CSVs and plots for the four scenarios. |

The firmware runs in Wokwi (not in this repo): [wokwi.com/projects/478913967602614273](https://wokwi.com/projects/478913967602614273). LoRa itself is not simulated.

## Setup

Python 3.12 with numpy, matplotlib and paho-mqtt 2.x. For the pipeline you also need an MQTT broker such as Mosquitto on `localhost:1883`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install numpy matplotlib paho-mqtt
```

## Run

```bash
python hydrology.py --all --plot     # data/<scenario>.csv
python node_logic.py --all --plot    # data/<scenario>_node.csv and E1-E3 numbers
python energy.py                     # battery use and life
```

`--all` uses fixed seeds, so the CSVs in `data/` can be reproduced exactly. Scenarios: `wet_season` (60 days), `dry_spell` (90), `long_drought` (150) and `sensor_faults` (90, dry spell plus spikes, stuck values, dropouts and a drift).

Data pipeline, with the broker running:

```bash
cd pipeline
python service.py --idle 5                    # terminal 1
python gateway.py --scenario sensor_faults    # terminal 2
python charts.py                              # writes pipeline_chart.png
```

The gateway pauses 3 ms between messages; without that the broker drops some. `service.py` recreates `peat.db` on every start. To get alerts in Telegram, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (for example in a local `.env`, which is git-ignored); otherwise alerts only print.

## How the node decides

- **Data checks:** depth outside 0 to 150 cm or a jump over 15 cm is `SUSPECT`; 6 identical readings or 4 hours without data is `FAULT`; decisions use the median of the last 3 good readings.
- **Risk levels:** Watch at depth 25 cm or a predicted 40 cm crossing within 7 days; Alert at 35 cm or 2 days; Danger at 40 cm or moisture below 0.76 m³/m³. A level rises after 2 worse readings and falls after 3 better ones.
- **Sampling:** measure every 60 / 30 / 10 / 10 min and send every 6 h / 3 h / 1 h / 30 min for Normal / Watch / Alert / Danger. A level change or a new fault sends at once.
- **Time to danger:** a line through the daily minimum depth of the last 5 days.
- **Payload:** 8 bytes with level, flag, depth, moisture, days to danger and battery. See `encode()` in `pipeline/gateway.py`.

All thresholds are starting defaults; the 40 cm limit follows SIPALAGA but is contested.

## Results

| | wet_season | dry_spell | long_drought | sensor_faults |
|---|---|---|---|---|
| Energy saved vs fixed sampling | 81% | 46% | 13% | 43% |
| Watch fires before 40 cm (days) | n/a | 11.7 | 15.4 | 14.4 |

In `sensor_faults` the node caught 31 of the 31 faults it read, missed the slow drift, and never read 19 of the 45 spikes. It raised 1 false flag in 5,661 clean readings. The long drought saves little because the node sits in Danger 73% of the time. Battery life assumes one 18650 cell and no solar input, so it is a worst case.

## Limits

- Synthetic data only; there is no parameter sweep yet.
- The Python logic mirrors the Wokwi sketch. Both count the same measurements and uplinks on the full data, but they are separate code.
- Rain drives the water model but is not an input to the risk rule.
- The battery value in the pipeline is fixed at 3,900 mV, and the watchdog, low-battery mode and resend-after-outage are not simulated.
- The node gives advice to local groups. It is not an official warning system.
