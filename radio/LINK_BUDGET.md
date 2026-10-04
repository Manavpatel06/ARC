# ARC node radio: band, link budget, BOM (C11)

**One line for the slide:** an ARC node is a 915 MHz ISM radio (same band FLARM uses in the US), 22 dBm, 2 dBi antennas, with **≥ 22 dB of link margin at 3 miles** and a **prototype parts cost of about $136**, versus about $1,450 for a PowerFLARM Flex and $2,199 for a certified skyBeacon ADS-B Out.

## Band

| | Choice | Why |
|---|---|---|
| Prototype / product band | **902–928 MHz ISM** (US), FCC Part 15.247 | Unlicensed; **the same band FLARM uses in the USA** (902.2–927.8 MHz; 868 MHz in Europe). Cheap transceivers exist. |
| Power limit | 1 W conducted with digital modulation (≥ 500 kHz 6 dB bandwidth), or FHSS with ≥ 50 channels; reduce power for antenna gain above 6 dBi; PSD ≤ 8 dBm / 3 kHz | 47 CFR 15.247. We use **22 dBm (160 mW)**, about 6 dB under the limit. |
| Modulation | **GFSK 250 kb/s** (frequency-hopped) for the slotted 1 Hz STATE; LoRa SF7/500 kHz as a long-range fallback | 120-byte signed STATE = **4.1 ms on air** at 250 kb/s; it fits the 30 ms slot with room for 40-slot frames (~39 aircraft at 1 Hz). LoRa SF7/500 takes 47 ms per packet: more range, a fifth of the capacity. |
| Certified path (later) | 978 MHz UAT / 1090 ES equipment | Aviation spectrum, TSO and FAA approval. ARC's protocol and trust logic stay; only the bearer changes. Hobby ISM emulation is **not** an aviation communications approval. |

## Link budget (free space, 915 MHz)

| | GFSK 250 kb/s | LoRa SF7 / 500 kHz |
|---|---|---|
| TX power | +22 dBm | +22 dBm |
| Antennas (both ends) | +2 dBi + 2 dBi | +2 dBi + 2 dBi |
| Cables / connectors (both ends) | −2 dB | −2 dB |
| Path loss at **3 mi (4.83 km)** | −105.4 dB | −105.4 dB |
| **Received at 3 mi** | **−81.4 dBm** | **−81.4 dBm** |
| Receiver sensitivity (SX1262 datasheet) | −104 dBm | −117 dBm |
| **Margin at 3 mi** | **22.6 dB** | **35.6 dB** |
| Margin at 6 mi | 16.6 dB | 29.6 dB |
| Max range, 10 dB fade margin | 20.7 km (12.9 mi) | 92.5 km (57.5 mi) |
| Max range, 20 dB fade margin | 6.5 km (4.1 mi) | 29.2 km (18.2 mi) |

Free-space loss is the best case between aircraft in the air. The margin pays for airframe shadowing (a wing between the antennas), multipath and fading. **With a 20 dB fade allowance GFSK still reaches 4.1 mi, so 3 mi holds.** The emulated channel in `radio/rf.py` uses the same model (915 MHz, 20 dBm, free space, ±3 dB RSSI noise).

## Prototype BOM (one node, retail single-unit prices, Oct 2026)

| Part | Example | Price |
|---|---|---|
| Sub-GHz transceiver | Ebyte E22-900M22S (Semtech SX1262, 22 dBm, LoRa + GFSK) | $5.98 |
| Processor | ESP32-S3-DevKitC-1 (runs node + radio stack) | $15.95 |
| GNSS | SparkFun MAX-M10S breakout (1.5 m, 10 Hz) | $45.95 |
| Baro altitude | Adafruit BMP390 (±0.25 m relative) | $10.95 |
| 915 MHz antenna | SparkFun ½-wave dipole, 2 dBi | $21.95 |
| GNSS antenna | active patch, SMA *(estimate)* | ~$15 |
| Power + enclosure | USB-C / 18650 holder, 3D-printed case *(estimate)* | ~$20 |
| **Total** | | **≈ $136** |

At volume the GNSS and antenna are the big savings: a bare GNSS module plus a printed antenna replaces about $60 of breakout boards. Radio power draw is small: 118 mA at +22 dBm for about 4 ms per second, 5 mA receiving.

**Reference products:** PowerFLARM Flex (FLARM + ADS-B in, 868/915 MHz): **$1,450**. uAvionix skyBeacon (978 MHz UAT ADS-B Out only, certified): **$2,199**.

## Honest words for Q&A
- "Prototype parts cost," not "product price": no certification, enclosure tooling or support in the $136.
- The link budget is free-space plus margin, **not measured in flight**. The ESP32-C6 "over the air" test (C10) would be the first measured number.
- 915 MHz ISM is shared spectrum, which is why ARC signs every packet, slots its transmissions and checks RF consistency.

Sources: FLARM FAQ (bands) flarm.com/en/support/faq · 47 CFR 15.247 (govinfo.gov) · Semtech SX1261/2 datasheet (sensitivity, TX current) · Ebyte E22-900M22S · Adafruit ESP32-S3-DevKitC-1, BMP390 · SparkFun MAX-M10S, 915 MHz dipole · Cumulus Soaring (PowerFLARM Flex) · Sporty's (skyBeacon).
