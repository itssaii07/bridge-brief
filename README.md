# Infrastructure Inspection Brief Generator

Drafts an **evidence-linked bridge inspection brief** in which every generated
sentence is traceable to a specific source artifact, and detects **contradictions
between official condition records** that nobody currently checks for.

A human inspector reviews and signs off. The system never issues a safety clearance
or a maintenance order.

> ### Current status
>
> **No data is on disk and nothing in this repository has been run against real data.**
> Every module is complete, defensive and unit-tested, and every data-dependent path is
> ready to run and unrun. No mock, sample or placeholder data was created anywhere under
> `data/`. Every number this system reports is produced at run time or reported as `n/a`
> with the reason.
>
> To put the data in place and run it, follow **[HANDOFF.md](HANDOFF.md)**.

---

## What it does

Two published federal sources describe the same bridges in different vocabularies:

| Source | What it says |
|---|---|
| **NBI** (National Bridge Inventory) | one blunt 0–9 condition rating per component |
| **NBE** (National Bridge Elements) | how much of each element sits in condition states 1–4 |

They disagree on real bridges. The contradiction engine reduces both to a shared
good/fair/poor vocabulary and reports where they conflict, in either direction, with
a severity score, a separate confidence score, and the artifact IDs on both sides:

```
Structure 013450, 2023:
  NBI  deck rating = 7  ("good")          [NBI-013450-2023-deck]
  NBE  340 sq ft of deck in CS3 ("poor")  [NBE-013450-2023-12-cs3]
  → contradiction
```

Flags raised on the 2023 records are then checked against the 2025 records: did the
official rating subsequently drop? That is a real predictive result on real published
data with no constructed ground truth — and it is reported against a **control base
rate**, so that "the flags predict nothing" is a result the harness can and will state.

---

## Invariants

These are architectural, not stylistic, and each is enforced in code rather than by
convention:

1. **Real data only.** A missing input is reported missing, never invented.
2. **Every generated sentence carries artifact IDs.** The grounding gate
   (`src/generate/grounding.py`) is deterministic code: a sentence with no citation
   that resolves to a stored artifact is dropped before render and counted in
   `blocked_unsupported`, which the UI displays as a headline figure.
3. **Originals are immutable.** Nothing under `data/raw/` is ever written. All
   processing writes to `data/derived/`.
4. **No autonomous clearance.** There is no column, no endpoint and no code path that
   marks a structure safe, cleared or scheduled. Sign-off marks a *brief* as reviewed
   by a named human.
5. **Uncertainty is always visible.** Every finding carries a confidence value and one
   of three evidence tiers (`corroborated`, `single_source`, `conflicting`).
6. **Imagery provenance is always labelled.** Every image is `inspection_upload` or
   `reference_corpus`. Corpus rows have a NULL structure, so benchmark imagery cannot
   be attached to a bridge.

---

## Setup

Python 3.10 or newer. The core runtime is **the standard library only**: no web
framework, no database server, no build step, no CDN.

```bash
git clone <this repo>
cd bridge-brief
python -m pip install -e ".[dev]"        # pytest, for the test suite
python -m pip install -e ".[imagery]"    # Pillow: photographs, detection, the benchmark
python -m pip install -e ".[llm]"        # anthropic SDK: Claude vision and the LLM drafter
python -m pytest -q                      # 474 tests, none of which touch data/
```

On a machine with more than one Python, always use `python -m pip` so packages land in
the interpreter that runs the project.

### Configuration

All configuration is by environment variable. **Nothing is hardcoded and no secret is
committed.**

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(unset)* | Enables **Claude vision** on uploaded photographs, and the optional LLM drafter |
| `BRIDGE_BRIEF_VISION_MODEL` | `claude-opus-5` | Model that reads photographs |
| `BRIDGE_BRIEF_DRAFTER` | `template` | `template` (deterministic, default) or `llm` |
| `BRIDGE_BRIEF_MODEL` | `claude-sonnet-5` | Model id for the LLM drafter |
| `BRIDGE_BRIEF_DATA` | `./data` | Root of the data tree |

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # never commit this; .env is gitignored
```

Without a key everything still runs: photographs are read by the classical baseline
detector instead, and the app says so on every page where it matters. The grounding
gate is deterministic code applied identically either way. No model is trusted to cite
correctly; every citation is checked.

---

## The data

`data/` is gitignored in full. Download each source yourself and place it as below.

| Source | Where to get it | Place it at |
|---|---|---|
| **NBI 2023, 2025** | FHWA NBI ASCII / delimited downloads — <https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm> (pick the delimited file for each year, all states) | `data/raw/nbi/2023/`, `data/raw/nbi/2025/` |
| **NBE 2023, 2025** | FHWA bridge element data — <https://www.fhwa.dot.gov/bridge/nbi/elements.cfm> (one ZIP per state per year; AL, AZ, IA) | `data/raw/nbe/{year}/AL/`, `.../AZ/`, `.../IA/` |
| **CODEBRIM** | CODEBRIM dataset, original images and annotations — <https://zenodo.org/record/2620293> | `data/raw/codebrim/` |

Sizes are substantial: a single NBI year is roughly 624,000 records. **Never `Read` a
raw file** — process it in a script and print aggregates. After ingest, run
`python -m scripts.extract_samples` to pull a handful of genuine records into
`samples/` for inspection.

Layout:

```
data/                        # gitignored in full
├── raw/                     # immutable; never written to
│   ├── nbi/{year}/          # NBI delimited, single-quote text qualifier
│   ├── nbe/{year}/{state}/  # NBE element XML or ZIP
│   └── codebrim/            # benchmark imagery + annotations
├── uploads/{structure}/     # photos supplied for a specific inspection
└── derived/
    ├── assets.sqlite        # the working index
    └── tiles/
```

---

## Running it

### The web app

```bash
python -m src.ui.server          # then open http://127.0.0.1:8765
```

| Page | What it is for |
|---|---|
| **Overview** `/` | Every GAI40 deliverable with the live figure behind it |
| **New inspection** `/inspect` | Pick a structure, drop in photographs, generate a draft brief |
| **Report** `/report/<brief>` | Photographs with region overlays; each sentence traced to its source; approve, edit or reject; sign off; publish |
| **Sign-off queue** `/queue` | Briefs awaiting a named reviewer, ordered by urgency |
| **Evaluation** `/metrics` | The full metrics table, with the reason beside every `n/a` |
| **Records** `/structures` | The federal record explorer |

**What to upload.** Photographs (JPEG, PNG, WebP, TIFF or BMP; up to 40 MB each, 24 per
inspection) **of the structure you select**. A photograph is only ever attached to a
bridge that exists in the federal inventory, and it is labelled `inspection_upload`, so
a photo of a different bridge would be mislabelled evidence. Search by structure number
(`AL012757`), road (`US 80`) or feature crossed (`Cahaba River`). Structures in Alabama,
Arizona and Iowa also carry element data, so their federal records are cross-checked
for contradictions; elsewhere the brief rests on the inventory rating and the photos.

Each upload is stored byte-for-byte under a name derived from its SHA-256, so the
original is always available from the report and no two photographs can overwrite each
other. Nothing is published until a named reviewer signs the brief off; once signed or
rejected it is frozen.

The server binds to 127.0.0.1 and has no authentication. Reviewer names are typed, not
verified. Do not expose it.

### The pipeline from the command line

Full order, with what each step prints, is in **[HANDOFF.md](HANDOFF.md)**. In brief:

```bash
# 1-2  ingest and see what you have
python -m src.ingest.nbi --year 2023 --year 2025
python -m src.ingest.nbe --year 2023 --year 2025
python -m src.catalog --record-missing

# 3-4  the core analysis
python -m src.analysis.contradictions --year 2023
python -m src.analysis.validate --json reports/validation.json

# 5-6  imagery (needs Pillow)
python -m src.ingest.uploads --structure AL012757 --dir ~/photos --detect
python -m src.ingest.dacl10k
python -m src.eval.codebrim_benchmark --corpus dacl10k --json reports/detector_dacl10k.json

# 7-8  brief and review
python -m src.generate.brief --structure AL012757 --year 2023 --print
python -m src.ui.server

# 9    the metrics table
python -m src.eval.sentence_review --export reviews/sentences.csv   # human fidelity sample
python -m src.eval.run_all --benchmark reports/detector_dacl10k.json --json reports/metrics.json

# Publishing is gated: this refuses any brief a named human has not signed off.
python -m src.generate.export --brief BRIEF-AL012757-2023-v1 --out reports/brief.md
```

Every command reports clearly and exits non-zero when its input is missing. None of
them will invent anything to fill a gap.

---

## Layout

```
src/
├── schema.sql              authoritative schema; changes go here first
├── ids.py                  artifact ID system + structure-number normalisation
├── db.py                   connection, migrations, DataUnavailable
├── store.py                evidence store; every write registers its artifact
├── catalog.py              coverage table and per-state breakdown
├── ingest/                 nbi.py, nbe.py, uploads.py, codebrim.py, element_map.py
├── analysis/               findings.py, contradictions.py, validate.py
├── detect/                 base.py (interface), tiling.py, baseline.py
├── generate/               grounding.py (the gate), drafters.py, brief.py
├── eval/                   codebrim_benchmark.py, metrics.py, run_all.py
└── ui/                     review.py, views.py, server.py
tests/                      286 tests, all on inline fixtures
```

Module boundaries are one-directional: **ingest never imports from generation.**

---

## Documents

* **[ASSUMPTIONS.md](ASSUMPTIONS.md)** — every decision made that the specification did
  not settle, every format guess, and the single change point for each.
* **[HANDOFF.md](HANDOFF.md)** — exactly what to do when the data arrives.

## Scope

Bridge NDE sensor data (GPR, electrical resistivity) is published only for a small set
of FHWA research structures, and inspection narratives are restricted in most states.
This project is therefore scoped to the two evidence streams available at scale —
imagery and official condition records. The evidence model accepts additional streams
without redesign: the artifact ID scheme already defines and parses the reserved
`NDE-{struct}-{method}-{cell}` form.
