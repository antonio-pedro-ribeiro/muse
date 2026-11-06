# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## Project purpose

Build an application that **monitors and analyzes EEG recordings from a Muse S Athena headband**.

Data path (verified 2026-11-06 on the development PC):

```
Muse S Athena ──BLE 5.3 (OpenMuse)──▶ this application
                                       ├─ ingest & buffer
                                       ├─ live monitoring (signal quality, band power)
                                       ├─ persistent recording storage
                                       └─ offline analysis & reports
```

**Decision:** the application connects to the headband directly over BLE using OpenMuse (see
`src/muse_monitor/check.py`). No phone app in the path. The architecture is specified in `spec/`.
**Alternative (2026-11-06):** the `mindmonitor` source receives the stream relayed by the Mind Monitor phone app
(OSC over UDP, default port 5000) for when the phone holds the Bluetooth link. Direct BLE stays the default: OSC
has no packet counters or device timestamps, so timing is rebuilt and gaps are only inferred.

### Goals

1. **Ingest**: receive the BLE stream reliably (reconnects, gaps, out-of-order or duplicate packets).
2. **Monitor live**: show raw traces, per-channel signal quality (contact/noise), band powers (delta, theta, alpha,
   beta, gamma), battery and connection state.
3. **Record**: store every session losslessly with timestamps and device/preset metadata, in an open format
   (CSV/Parquet for tabular data; EDF+ or BIDS-EEG for interoperability).
4. **Analyze**: offline processing of stored sessions: filtering, artifact handling (blinks, jaw clench,
   motion via IMU), spectral analysis, trends across sessions.

### Non-goals (for now)

- Medical diagnosis. This is a consumer device and the outputs are not clinical. Do not word UI text or reports as
  diagnoses.
- Writing our own BLE stack. Use OpenMuse for connection and packet decoding.
- Receiving data from phone apps other than Mind Monitor, or over HTTP.

## Repository layout and commands

- Python 3.13 project managed with **uv** (`pyproject.toml`, `uv.lock`). OpenMuse is installed from its GitHub
  repository (see `[tool.uv.sources]`).
- `src/muse_monitor/check.py`: the `muse-check` connectivity CLI.

```
uv sync                                   # install dependencies
uv run muse-check scan                    # list Muse headbands in range
uv run muse-check test                    # connect, stream 10 s, report rates/loss/battery/EEG contact
uv run muse-check test --duration 30 --save raw.txt   # also save raw packets (OpenMuse text format)
uv run OpenMuse stream --address <MAC>    # publish LSL streams
uv run muse run --calibrate --duration 10m   # live session: dashboard + recording
uv run muse run --source mindmonitor -o eeg_offset_uv=800   # via the Mind Monitor app (OSC, UDP 5000)
uv sync --extra models && uv run muse models pull cbramod   # model weights (M5)
uv run muse train-probe ID ID ID --label-from calibration   # probe vs band power
```

`muse-check test` exits 0 (PASS) when EEG arrives at ≥90% of 256 Hz with ≤2% of subpackets missing.

## Connecting from Windows: lessons learned

- **Do not pair the headband in Windows Settings.** When it was paired, every connection dropped right after
  connecting ("Unreachable" on GATT access, then "Not connected"), in our tool and in `OpenMuse record` alike.
  Removing the device in Settings → Bluetooth & devices fixed it; bleak connects without pairing.
- The headband only advertises (and so is only found by a scan) when nothing else is connected to it. Close the
  Muse phone app first.
- Subscribing to the control characteristic (`273e0001`, firmware/battery info) can fail on Windows. Treat it as
  optional; only the data characteristics (`273e0013`, `273e0014`) are required.
- The first subpacket of each BLE packet has no `subpkt_index` (`None`). It fills the next slot in the sequence;
  don't count it as a lost packet.
- Observed on this PC (preset p1041, Intel adapter, about 50–60 dBm RSSI): EEG 8 ch at 256 Hz, optics 16 ch at
  ~64 Hz, motion at ~51 Hz, about 1–2% of subpackets missing, connect time 1.5–2.5 s. Firmware 3.1.19,
  hardware 01.1.

## Device facts: Muse S Athena

These come from the OpenMuse and muse-rs projects, which reverse-engineered the protocol. Treat them as
**provisional** and check them against real data from the user's device.

| Signal | Details |
|---|---|
| Radio | BLE 5.3, 2.4 GHz |
| EEG | TP9, AF7, AF8, TP10 (+ FPz reference/DRL, + aux). 4-ch or 8-ch mode depending on preset. **256 Hz**, 14-bit |
| EEG channel order (8-ch) | TP9, AF7, AF8, TP10, FPz, AUX_R, AUX_L, AUX |
| EEG scaling (Athena) | `µV = (raw14 − 8192) × 1450/16383` (≈ 0.0885 µV/LSB, range ±725 µV). OpenMuse returns `raw14 × 1450/16383` (0–1450 µV); the `openmuse` source (v1.1+) subtracts the midpoint. Recordings made with v1.0 carry a +725 µV offset (no `offset_uv` in the EEG stream meta) |
| Optics (fNIRS + PPG) | 5-optode bilateral frontal; wavelengths IR 850 nm, near-IR 730 nm, red 660 nm (+ ambient). 4/8/16 channels by preset. **64 Hz**, 20-bit |
| Accelerometer | 3-axis, **52 Hz**, 16-bit, ±2 g (0.0000610352 g/LSB) |
| Gyroscope | 3-axis, **52 Hz**, 16-bit, ±250 dps (−0.0074768 °/s/LSB, sign flipped compared with classic Muse) |
| DRL/REF | 32 Hz |
| Battery | about 0.2–1 Hz, depending on firmware |

### Presets (selected by the streaming app, not by us)

| Preset | EEG | Optics | Notes |
|---|---|---|---|
| p20 / p21 / p50 / p51 / p60 / p61 | 4 ch | none | EEG-only / classic-style |
| p1035 | 4 ch | 4 ch | low red LED |
| p1034 / p1043 | 8 ch | 8 ch | |
| p1041 / p1042 / p1045 | 8 ch | 16 ch | all channels; OpenMuse default p1041, muse-rs uses p1045 |
| p1046 / p4129 | 8 ch | 4 ch | |

**Implication for this codebase:** never hard-code a channel count. Read channel names, counts and sample rates from
the stream metadata (or infer them from the payload) and store them with each recording.

### BLE layer (handled by OpenMuse; reference)

- GATT characteristics are in the `273e00xx-4c4d-454d-96be-f03bac821358` family. Athena sends sensor data on
  `273e0013` and `273e0014`. Control/commands go through `273e0001`.
- Startup sequence (OpenMuse): `v6 → s → h → <preset> → s → dc001 ×2 → L1 → s`. Stop with `h`.
- Packets contain tagged subpackets: `0x11`/`0x12` EEG 4/8 ch, `0x34`/`0x35`/`0x36` optics 4/8/16 ch, `0x47` IMU,
  `0x53` DRL/REF, `0x88`/`0x98` battery.
- Athena packets carry no reliable per-sample timestamps. Timestamps are assigned on the receiving side, so expect
  clock jitter and reconstruct times from the sample rate and packet counters where possible.

Save short real captures under `samples/` (anonymized) and build parsers and tests from them.

## Engineering conventions

- **Language/stack**: Python 3.13 + uv. Microkernel with source/analyzer/sink plugins; see `spec/`. Built-in ML
  model: CBraMod (via braindecode, `[models]` extra).
- **Keep raw data**: always store the unmodified incoming samples. Filtering and derived features are separate,
  reproducible steps. Never overwrite raw data.
- **Units and metadata**: store µV for EEG and declare the units of every other stream. Each recording stores the
  device ID, preset, channel list, sample rates, firmware (if known), start time (UTC) and app version.
- **Time**: use UTC internally. Use monotonic clocks to measure gaps. Detect dropped samples and log them instead of
  hiding them.
- **Signal processing defaults** (adjust as needed): 1–40 Hz band-pass and 50/60 Hz notch for EEG display. Use
  Welch PSD for band power. Use IMU motion and frontal-channel amplitude thresholds to flag artifacts.
- **Testing**: parsers get unit tests against fixture captures. Analysis functions get tests on synthetic signals
  with a known spectrum (e.g. a 10 Hz sine should show up as alpha).
- **Privacy**: EEG is sensitive personal/biometric data. Don't commit real recordings unless they're explicitly
  anonymized and the user approves. Keep `data/` and `recordings/` in `.gitignore`. Don't send data to third-party
  services without explicit consent.

## References

- OpenMuse (Python; Athena BLE decode, record, LSL streaming): https://github.com/DominiqueMakowski/OpenMuse
- muse-rs (Rust; Athena packet/tag tables, startup sequence): https://docs.rs/crate/muse-rs/latest
- elata-muse-proto (Rust; Athena decoding): https://docs.rs/crate/elata-muse-proto/latest
- muse-lsl (Python; classic Muse plus Athena support in newer versions): https://github.com/alexandrebarachant/muse-lsl
- Mind Monitor (iOS/Android; OSC streaming, CSV recording): https://mind-monitor.com/
- Muse S Athena announcement: https://choosemuse.com/blogs/news/the-future-of-mental-fitness-starts-now-meet-muse-s-athena
