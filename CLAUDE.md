# Infrastructure Inspection Brief Generator

## What this is

A tool that takes the evidence available for a bridge — photographs supplied for the
inspection, and the official condition records for that structure — and drafts an
**evidence-linked inspection brief** in which every generated statement is traceable
to a specific source artifact. A human inspector reviews and signs off. The system
never issues a safety clearance or maintenance order on its own.

Its most distinctive capability: it detects **contradictions between official records**
that no one currently looks for.

Original problem statement (background — NOT the build instruction):

> **GAI40 — Generative AI Infrastructure Inspection Brief Generator**
> Inspectors often combine drone images, handwritten notes, and sensor readings
> manually before producing a defect report. Develop a tool that drafts an
> evidence-linked inspection brief with image regions, extracted observations,
> confidence levels, and a human sign-off queue.
>
> *Constraints:* Keep original sources available, mark uncertainty clearly, and
> require human review before publication, external sharing, or high-stakes use.
>
> *Expected solution:* Identify missing evidence, measure source-link accuracy and
> missed-defect rate, preserve the original artefacts, never provide autonomous
> safety clearance or maintenance orders. Evaluation should measure factual and
> source fidelity, unsupported-content rate, user correction effort, and edge cases.

### Scope decision (state this openly in the writeup)

Bridge NDE sensor data (GPR, electrical resistivity) is published only for a small set
of FHWA research structures, and inspection narratives are restricted in most states —
Pennsylvania withholds them, Texas requires open-records requests, several classify
them as security-sensitive. We therefore scoped to the two evidence streams available
at scale: **imagery** and **official condition records**. The evidence model accepts
additional streams without redesign; the artifact ID scheme already defines NDE types.

---

## Non-negotiable invariants

Architectural, not stylistic. Do not weaken them for convenience.

1. **Real data only.** No mock records, no sample generators, no placeholder images,
   no `faker`, no synthesized observations. If a needed input is missing, the correct
   behavior is to report it missing — never to invent it.

2. **Every generated sentence carries artifact IDs.** A sentence with zero artifact
   IDs is dropped before render and counted in `blocked_unsupported`. This counter is
   displayed in the UI. It is a feature, not a debug metric.

3. **Originals are immutable.** Nothing in `data/raw/` is ever modified, overwritten
   or deleted. All processing writes to `data/derived/`.

4. **No autonomous clearance.** Drafts and recommendations only. No code path may mark
   a structure as safe, cleared, or scheduled for maintenance without recorded human
   sign-off.

5. **Uncertainty is always visible.** Every finding carries a confidence value and an
   evidence tier. Never present a single-source finding as if corroborated.

6. **Imagery provenance is always labelled.** Every image in a brief is tagged either
   `inspection_upload` (supplied for this structure) or `reference_corpus` (public
   benchmark imagery, not of this structure). The UI shows this. Never let corpus
   imagery read as evidence about a specific bridge.

---

## Data

> ### Data status — on disk and ingested
>
> NBI 2023 and 2025 and the NBE 2023/2025 extracts for AL, AZ and IA are on local
> disk and have been ingested: 621,581 and 624,193 NBI rows with 0 rejected, and
> 66,597 / 55,905 NBE element records with 0 rejected. **10,661 structures have
> both sources in 2023** — that is the population the contradiction engine runs on.
>
> **CODEBRIM is not usable as published.** The archive's MD5 matches Zenodo and nothing
> in it is encrypted: it is a classic ZIP written past the 4 GiB limit without ZIP64
> records, so every stored offset has wrapped modulo 2³². All 1,057 annotations and 839
> of 1,700 images still read; the rest do not. Re-downloading cannot help and no password
> is involved — recover with `7z x` or `zip -FF`, or use one of the alternatives in
> ASSUMPTIONS.md H6b. Milestone 6 reports `n/a` with the reason rather than a number.
> `scripts/check_dataset_archive.py` vets an archive from its index before download.
>
> The rule that nothing is ever created under `data/` still stands, permanently: a
> missing input is reported missing. Unit-test fixtures live in `tests/`, never under
> `data/`, and never reach a brief. Real record excerpts are committed in `samples/`.

### What we have

| Source | Coverage | Role | Status |
|---|---|---|---|
| NBI 2023, 2025 | All states, 621,581 / 624,193 records | Structure inventory + official 0–9 condition ratings | ingested, 0 rejected |
| NBE 2023, 2025 | Alabama, Arizona, Iowa | Element-level condition state quantities (CS1–CS4) | ingested, 0 rejected |
| CODEBRIM original images | 1,590 annotated images | Detector benchmark only — never demo evidence for a named bridge | **unavailable — encrypted archive** |

### Never read raw data into context

Files under `data/raw/` are too large for the context window — a single NBI year is
~624k records. **Do not `Read` them.** Process in scripts, print aggregate summaries.
For record shape, read `samples/` — 2–3 real records per source, committed.

### Layout

```
data/                      # gitignored in full
├── raw/                   # immutable, never modified
│   ├── nbi/{year}/        # NBI ASCII, comma-delimited
│   ├── nbe/{year}/{state}/  # NBE element XML
│   └── codebrim/          # benchmark imagery + annotations
├── uploads/{structure}/   # photos supplied for a specific inspection
└── derived/
    ├── assets.sqlite      # the working index — query this
    └── tiles/
```

### Join key

The **NBI structure number** joins NBI, NBE and uploads. Normalize on ingest — states
pad and format it inconsistently.

---

## Artifact ID system

Every addressable unit of evidence gets a stable ID. These are what generated
sentences cite and what the UI resolves back to source.

| Source | Format | Example |
|---|---|---|
| NBI rating | `NBI-{struct}-{year}-{component}` | `NBI-013450-2023-deck` |
| NBE element state | `NBE-{struct}-{year}-{elem}-cs{n}` | `NBE-013450-2023-12-cs3` |
| Uploaded photo | `IMG-{struct}-{photo}` | `IMG-013450-p03` |
| Photo region | `IMG-{struct}-{photo}-r{n}` | `IMG-013450-p03-r2` |
| Reference image | `REF-{corpus}-{id}` | `REF-codebrim-00412` |
| NDE cell *(reserved)* | `NDE-{struct}-{method}-{cell}` | not yet populated |

IDs are deterministic from source coordinates — never from iteration order or
timestamps. The reserved NDE form is intentional: it demonstrates the evidence model
extends to sensor streams when they become available.

> **Deviation found when the real data arrived: `{struct}` is state-qualified.**
> The examples above write the bare structure number, but NBI item 8 is unique only
> *within a state* — 40,374 numbers in the 2023 file are claimed by more than one
> state, and `000002` by six. The bare form cannot identify a bridge nationally, so
> `{struct}` is the postal state abbreviation followed by the normalised number:
> `NBI-AL013450-2023-deck`, not `NBI-013450-2023-deck`. Rationale and measurements
> are in ASSUMPTIONS.md B5.

---

## The contradiction engine — the core analytical output

NBI records one blunt 0–9 score per component. NBE records how much of each element
sits in condition states 1–4. **These disagree on real bridges**, and nobody is
checking. Example:

```
Structure 013450, 2023:
  NBI  deck rating = 7  ("good")          [NBI-013450-2023-deck]
  NBE  340 sq ft of deck in CS3 ("poor")  [NBE-013450-2023-12-cs3]
  → contradiction
```

A real one, found in the published records and confirmed by the 2025 release:

```
Structure AL012757, 2023:
  NBI  deck rating = 7 ("good")                      [NBI-AL012757-2023-deck]
  NBE  4,078 of 7,255 units of deck in CS3/CS4 — 56% [NBE-AL012757-2023-12-cs3]
  → contradiction, severity 1.00
  → the 2025 NBI release rates the same deck 6
```

Validation is built in: flag contradictions in the **2023** data, then check the
**2025** data to see whether the official rating subsequently dropped. That is a real
predictive result on real published records, with no constructed ground truth.

### Evidence tiers

Every finding gets exactly one tier:

- **`corroborated`** — two or more independent sources agree
- **`single_source`** — only one source observes it; reduced confidence
- **`conflicting`** — sources disagree

**Surface conflicts most prominently.** They occur naturally in the real record. Do
not manufacture them and do not suppress them.

---

## Milestones

Build in order. Stop at the end of each and report before continuing.

| # | Scope | Gate to pass |
|---|---|---|
| 1 | Ingest NBI + NBE, coverage table | Corpus size known |
| 2 | Artifact ID system + evidence store | Every unit addressable and resolvable |
| 3 | Contradiction engine | Real NBI-vs-NBE conflicts found in 2023 |
| 4 | 2025 validation pass | Did flagged bridges get downgraded? |
| 5 | Photo upload + detector | Regions detected on uploaded photos |
| 6 | CODEBRIM benchmark | Precision/recall reported |
| 7 | Brief generation with grounding gate | `blocked_unsupported` counter working |
| 8 | Review UI + sign-off trail | Click sentence → evidence highlights |
| 9 | Eval harness | Metrics table reproducible end to end |

Milestones 3 and 4 are the project's centre of gravity. If the schedule slips, cut
scope from 5–6, never from 3–4.

---

## Evaluation

All metrics computed against real published data. No synthetic ground truth.

| Metric | Ground truth source |
|---|---|
| Contradiction precision | Manual review of sampled flags |
| Predictive alignment | 2025 NBI rating change on 2023-flagged bridges |
| Defect detection P/R | CODEBRIM annotations |
| Source-link accuracy | Manual verification of sampled sentences |
| Missing-evidence recall | Structures genuinely lacking NBE records |
| Unsupported-content rate | `blocked_unsupported` / total generated sentences |
| Correction effort | Edits per finding during review |

---

## Working agreements

- Python. Ingest, analysis and generation in separate modules with clean boundaries —
  the ingest layer must not import from the generation layer.
- Schema changes go in `src/schema.sql` first, then migrations. Never mutate
  `assets.sqlite` structure ad hoc.
- Long-running ingest prints progress and is resumable. Assume interruption.
- When a source is unavailable or a field missing, log it and continue. Never fill a
  gap with a default that could be mistaken for an observation.
- Small, verifiable steps. After each milestone, show what was built and what the
  numbers look like before moving on.