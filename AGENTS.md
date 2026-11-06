# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## Project purpose

Build an application that **monitors and analyzes EEG recordings from a Muse S Athena headband**.

Data path:

```
Muse S Athena ──BLE 5.3 (OpenMuse)──▶ this application
                                       ├─ ingest & buffer
                                       ├─ live monitoring (signal quality, band power)
                                       ├─ persistent recording storage
                                       └─ offline analysis & reports
```

**Decision:** the application connects to the headband directly over BLE using OpenMuse (see
`src/muse_monitor/check.py`). No phone app in the path. The architecture is specified in `spec/`.

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
- Receiving data from a phone app (HTTP/OSC). Revisit only if a concrete need appears.

## Device facts: Muse S Athena

These come from the OpenMuse and muse-rs projects, which reverse-engineered the protocol. Treat them as
**provisional** and check them against real data from the user's device.

| Signal | Details |
|---|---|
| Radio | BLE 5.3, 2.4 GHz |
| EEG | TP9, AF7, AF8, TP10 (+ FPz reference/DRL, + aux). 4-ch or 8-ch mode depending on preset. **256 Hz**, 14-bit |
| EEG channel order (8-ch) | TP9, AF7, AF8, TP10, FPz, AUX_R, AUX_L, AUX |
| EEG scaling (Athena) | `µV = (raw14 − 8192) × 0.0885` |
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

- GATT characteristics are in the `273e00xx-…` family. Athena multiplexes all sensors onto one characteristic
  (`273e0013`). Control/commands go through `273e0001`.
- Startup sequence (muse-rs): `v4 → s → h → p1045 → dc001 ×2 → d → L1`.
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
