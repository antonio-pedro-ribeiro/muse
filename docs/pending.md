# Pending: completing M5 (models)

All of M5's code is built and tested: the `cbramod` model plugin, `muse models pull` and
`muse train-probe`. What's missing is the **acceptance check on real data**:

> A probe trained on CBraMod embeddings must beat the same classifier trained on band power, scored
> only on sessions it never saw (session-held-out cross-validation). Otherwise the model plugin is
> marked "not recommended".

Either outcome completes M5. The aim is an honest answer, not a win for the model.

## 1. Prepare (once)

```
uv sync --extra models
uv run muse models pull cbramod
uv run muse check            # with the headband on: should end with PASS
```

Before each session: close the Muse phone app, don't pair the headband in Windows, and moisten the
sensors.

## 2. Choose the tasks

Record two comparisons:

| Comparison | Labels | Why |
|---|---|---|
| Eyes open vs. eyes closed | from calibration | A sanity check. Band power will almost certainly win, because closing your eyes simply raises alpha. |
| Relaxing vs. mental arithmetic (count back from 1000 in steps of 7) | from markers | The real test: a difference that isn't purely spectral. Music vs. silence also works. |

## 3. Record the sessions

- **At least 3 sessions, ideally 5, on different days.** Each held-out session is one test, so with
  fewer the result is mostly noise.
- **About 15 minutes each**, all with the same block layout.

Use the web interface, because it supports labelled markers:

```
uv run muse serve            # open http://127.0.0.1:8765
```

For each session:

1. On **Live**, keep the default analyzers, give it a label (for example `m5 day 1`) and press
   **Start session**.
2. Wait until **Signal quality** shows most electrodes as good. Adjust the headband if not.
3. Press **Calibrate (2 min)** and follow the prompts: 60 s eyes open, then 60 s eyes closed.
4. Then alternate blocks of **2 minutes each**, eyes open, sitting still. Type the label and press
   **Add marker** at the start of every block:

   | Block | 1 | 2 | 3 | 4 | 5 | 6 |
   |---|---|---|---|---|---|---|
   | Marker | `relax` | `arithmetic` | `relax` | `arithmetic` | `relax` | `arithmetic` |

   Alternating matters: if each session were "all relax, then all arithmetic", a classifier could
   learn slow drift over time instead of the task.
5. Add a `-` marker for any interruption. Everything from a `-` marker to the next marker is
   ignored.
6. Press **Stop**.

> Use `relax`, not `rest`: `train-probe` treats `rest`, like `-`, as unlabelled.

Keep blinking normal and avoid jaw clenching during arithmetic. Both produce large non-brain
signals that a classifier could pick up instead.

## 4. Train and compare

```
uv run muse sessions list --json                  # copy the session ids

uv run muse train-probe ID1 ID2 ID3 --label-from calibration --out eyes.npz
uv run muse train-probe ID1 ID2 ID3 --label-from markers     --out task.npz
```

Each run prints one row per held-out session, with the balanced accuracy of the CBraMod probe and
of band power, plus chance level (0.50 for two labels). Add `--json` to save the full results.

## 5. Decide

The built-in verdict compares only the means. Use this stricter rule:

- **Recommended for this task** if the model's mean is at least **0.05 higher** than band power's
  **and** it wins in **most** held-out sessions.
- **Not recommended** otherwise, including when both are near chance. In that case the task may
  simply not be detectable with this headband.

Expected result: band power wins eyes open/closed, and the task comparison decides.

## 6. Record the outcome

Update the M5 row in [spec/roadmap.md](../spec/roadmap.md) with:

- the task(s), number of sessions and dates;
- per-session scores for both methods, and the means;
- the conclusion: "recommended for <task>" or "not recommended: band power is enough".

Don't commit the recordings: they're personal data and `recordings/` is excluded from git.

## Optional improvements before recording

Two small changes would make the comparison fairer. They're **not implemented yet**:

1. **Skip bad-signal windows in `train-probe`.** It currently uses every labelled window, including
   ones with poor contact, which adds noise and can hide a real difference.
2. **Make the verdict stricter in the tool**: report how many held-out sessions each method wins, and
   apply the 0.05 margin from step 5 automatically.

## While you're recording

The same sessions can close two other open items:

- **M1:** confirm the signal quality shows good contact with the headband worn.
- **M4:** during one live session in the browser, note CPU use (Task Manager) and whether the charts
  stay smooth.
