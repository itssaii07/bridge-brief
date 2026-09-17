# Bridge Brief — project status

Snapshot of where this project stands: what the problem is, what has been built, and
what still has to happen. The short version is that **all nine milestones are
implemented and 300 tests pass, and everything that remains is blocked on the datasets
not yet being on disk.**

---

## The problem statement (GAI40)

> **Generative AI Infrastructure Inspection Brief Generator**
> Inspectors often combine drone images, handwritten notes, and sensor readings
> manually before producing a defect report. Develop a tool that drafts an
> evidence-linked inspection brief with image regions, extracted observations,
> confidence levels, and a human sign-off queue.
>
> *Constraints:* Keep original sources available, mark uncertainty clearly, and require
> human review before publication, external sharing, or high-stakes use.
>
> *Expected solution:* Identify missing evidence, measure source-link accuracy and
> missed-defect rate, preserve the original artefacts, never provide autonomous safety
> clearance or maintenance orders. Evaluation should measure factual and source
> fidelity, unsupported-content rate, user correction effort, and edge cases.

### Scope decision, stated openly

Bridge NDE sensor data (GPR, electrical resistivity) is published only for a small set
of FHWA research structures, and inspection narratives are restricted in most states —
Pennsylvania withholds them, Texas requires open-records requests, several classify them
as security-sensitive. The project is therefore scoped to the two evidence streams
available at scale: **imagery** and **official condition records**.

The evidence model accepts further streams without redesign. The artifact ID scheme
already defines, constructs and parses the reserved `NDE-{struct}-{method}-{cell}` form,
so adding a sensor stream is an ingest module, not an architecture change.

### The distinctive capability

Two federal sources describe the same bridges in different vocabularies:

| Source | What it says |
|---|---|
| **NBI** (National Bridge Inventory) | one blunt 0–9 condition rating per component |
| **NBE** (National Bridge Elements) | how much of each element sits in condition states 1–4 |

They disagree on real bridges, and nobody is checking. That is the core analytical
output:

```
Structure 013450, 2023:
  NBI  deck rating = 7  ("good")          [NBI-013450-2023-deck]
  NBE  340 sq ft of deck in CS3 ("poor")  [NBE-013450-2023-12-cs3]
  → contradiction
```

Flags raised on the 2023 records are then checked against 2025: did the official rating
subsequently drop? A real predictive result on real published data with no constructed
ground truth — reported against a **control base rate**, so "the flags predict nothing"
is a result the harness can and will state.

---

## The six invariants, and where each is enforced

These are architectural, not stylistic. Each is enforced in code rather than by
convention, and `tests/test_invariants.py` guards them at the repository level.

| # | Invariant | Enforced by |
|---|---|---|
| 1 | **Real data only** | Nothing under `data/` was ever created. A missing input raises `DataUnavailable` with the path and the fix. A test fails if any file is committed under `data/` or a mock generator appears in `src/` |
| 2 | **Every sentence carries artifact IDs** | `src/generate/grounding.py` — deterministic code. Citations are validated *and* resolved against the `artifacts` table; zero survivors means dropped, logged and counted in `blocked_unsupported` |
| 3 | **Originals are immutable** | Nothing writes to `data/raw/`. Uploads are copied byte-for-byte, never re-encoded. NBE ZIPs are read in place |
| 4 | **No autonomous clearance** | No such column, endpoint or function exists. Tests assert the schema has no clearance column and that the UI's entire control set is `{approve, edit, reject, sign off, reject brief}` |
| 5 | **Uncertainty is always visible** | `confidence` and `evidence_tier` are required fields on `Finding`; severity and confidence are scored and stored separately |
| 6 | **Imagery provenance is labelled** | `images.provenance` is `NOT NULL` with a `CHECK`. Corpus rows have a `NULL` structure, so benchmark imagery is structurally incapable of being attached to a bridge |

---

## What is done

All nine milestones, one commit each, 300 tests passing. No test touches `data/`.

| # | Scope | State |
|---|---|---|
| 1 | Schema, artifact ID system, scaffolding | Done, runnable |
| 2 | NBI + NBE ingest, coverage catalogue | Done, **unrun** |
| 3 | **Contradiction engine** | Done, **unrun** |
| 4 | **2025 validation pass** | Done, **unrun** |
| 5 | Photo upload + detector | Done, **unrun** |
| 6 | CODEBRIM benchmark | Done, **unrun** |
| 7 | Brief generation + grounding gate | Done, runnable |
| 8 | Review UI + sign-off trail | Done, runnable |
| 9 | Eval harness | Done, runnable |

### Milestone detail

**1 — Foundation.** `src/schema.sql` is the authoritative schema (structures, ratings,
elements, artifacts, findings, briefs, review/sign-off trail, `ingest_log`,
`rejected_rows`, `missing_evidence`, `blocked_sentences`). `src/ids.py` gives every unit
of evidence a deterministic ID with a matching parser — round-trip tested, including the
reserved `NDE-` form. One shared structure-number normaliser is used everywhere, so the
NBI/NBE/uploads join cannot silently drift.

**2 — Ingest.** NBI resolves columns **by header name, never by position**; a missing
required column stops the run and lists the headers actually present. The NBE parser is
namespace-agnostic, handles both published condition-state layouts, and confines the
entire format assumption to one function. `src/catalog.py` reports coverage with the
per-state breakdown for AL, AZ and IA, plus rejected rows by reason.

**3 — Contradiction engine.** Reduces NBI ratings and NBE condition states to a shared
good/fair/poor vocabulary and flags disagreement in **both** directions. Severity and
confidence are scored separately, with the confidence caveats stated on the finding.
Every threshold is a named constant and is recorded on each finding, so a later re-tune
is auditable. It reproduces the worked example from the specification exactly — which
required an absolute-quantity rule alongside the fraction rule, since 340 sq ft of an
8,000 sq ft deck is only 4.3%.

**4 — Validation pass.** Direction-aware: an NBI-optimistic flag is confirmed by a
*downgrade*, an NBI-pessimistic flag by an *upgrade*. Unevaluable structures are
excluded and counted, never scored as misses. A control group of unflagged components
gives the base rate, so `predictive_lift` — not `predictive_alignment` — is the headline.
Contradiction precision is never self-scored: the harness exports a deterministic sample
for a human to label and reads the labels back.

**5–6 — Imagery.** A swappable `DefectDetector` interface with a classical edge-energy
baseline behind it. It is **not a trained model**, and the benchmark report says so: its
class-aware score will be zero by construction because it localises without classifying.
Region artifact IDs derive from geometric ordering, so they are stable across runs.

**7 — Grounding gate.** Deterministic code that does not trust the drafter. Citations
are taken from the declared list *and* from the sentence text; each is parsed and then
resolved against the `artifacts` table. A well-formed but non-existent ID cannot launder
a sentence. The default drafter is template-based and needs no API key or network; an
LLM drafter sits behind the same interface and is gated identically.

**8 — Review UI.** Stdlib `http.server`, no framework, no build step. Click a sentence
and the artifacts it cites highlight. `blocked_unsupported` is a headline counter, not a
buried metric. The sign-off trail is append-only — an edit keeps the original text, which
is what the correction-effort metric reads. Renders an honest empty state against an
empty database, which is the state it will first be run in.

**9 — Eval harness.** The full metrics table. A metric that cannot be computed reads
`n/a` **with the reason and the command that would fill it** — a gap and a measured zero
are never confused.

---

## What still needs to happen

**Nothing is blocked on code. Everything is blocked on the datasets.** `data/` does not
exist in this repository, and no mock, sample, placeholder or example file was ever
created under it — that was the single most important rule of the build. Running any
ingest command right now reports the absence, exits `2`, and leaves nothing behind.

### 1. Add the datasets

```
data/raw/nbi/2023/        NBI 2023 delimited .txt/.csv, all states (~624k records)
data/raw/nbi/2025/        NBI 2025, same
data/raw/nbe/2023/AL/     Alabama 2023 element data (.zip — leave it zipped)
data/raw/nbe/2023/AZ/     Arizona 2023
data/raw/nbe/2023/IA/     Iowa 2023
data/raw/nbe/2025/AL/     and the same three for 2025
data/raw/nbe/2025/AZ/
data/raw/nbe/2025/IA/
data/raw/codebrim/        CODEBRIM original images + annotation files
```

| Source | Where to get it |
|---|---|
| NBI 2023, 2025 | <https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm> — the delimited file for each year, all states |
| NBE 2023, 2025 | <https://www.fhwa.dot.gov/bridge/nbi/elements.cfm> — one ZIP per state per year (AL, AZ, IA) |
| CODEBRIM | <https://zenodo.org/record/2620293> — original images and annotations |

Two things that matter:

* **The state subdirectory name is where the NBE parser gets the state from.** Getting
  `AL` / `AZ` / `IA` right is not cosmetic.
* **Leave the NBE ZIPs zipped.** They are read in place, because `data/raw/` is treated
  as immutable and nothing will ever unpack into it. If the NBI download arrives as a
  ZIP, unpack that one into its year directory.

`data/` is gitignored in full. Nothing from it is ever committed.

### 2. Then, in order

1. **Ingest and check the join.** `python -m src.catalog --record-missing`. The number
   that matters is *structures with both sources* — that is the population the engine can
   run on. Zero there, with both sources ingesting cleanly, means the join key is not
   matching.
2. **Commit real record shapes.** `python -m scripts.extract_samples --year 2023` pulls a
   handful of genuine records into `samples/` for review. That directory is deliberately
   empty right now — inventing a "representative" record would have been the worst kind
   of fabrication, since it would then be read as the authority on record shape.
3. **Tune the contradiction thresholds.** They are set from judgement, not from real
   distributions. Too tight flags nothing; too loose flags everything. Both are tuning
   problems, not bugs, and the whole surface is one constants block at the top of
   `src/analysis/contradictions.py`. `MIN_TOTAL_QTY` first.
4. **Get contradiction precision.** Requires a human labelling a sample —
   `--export-review`, fill the verdict column, `--read-review`.
5. **Delete the "⚠️ CURRENT STATUS — the data is NOT on disk yet" block from
   `CLAUDE.md`** once the coverage table looks right. Leaving it would make the next
   reader think there is still nothing to run.

`HANDOFF.md` walks all of this step by step — what to run, what output to expect at each
step, how to read the result, and exactly what to send back when a step fails.

---

## Top three things most likely to need fixing on real data

1. **The NBE XML parser.** Written against the published structure without ever seeing a
   real file. It is namespace-agnostic and handles both condition-state layouts, but the
   tag names are educated guesses. The fix is confined to
   `src/ingest/nbe.py::extract_elements` and the tag-candidate lists above it;
   `HANDOFF.md` step 3 says exactly which 4,000 bytes to send. If it fails it fails
   **loudly** — an unparsed file must never masquerade as a structure with no elements,
   because that would corrupt the missing-evidence metric.
2. **`MIN_TOTAL_QTY` and the band thresholds.** Judgement, not data. This is the single
   threshold most in need of tuning against real distributions.
3. **`ABS_DETERIORATED_QTY = 250`.** The rule that makes the documented example work, but
   it compares an absolute quantity against whatever units the source publishes, so it is
   only meaningful for area-scaled elements. A real weakness of the rule, flagged rather
   than hidden.

Every decision the specification did not settle is in `ASSUMPTIONS.md`, sections A–L,
with the single change point named for each format guess.

---

## Running it

```bash
python -m pip install -e ".[dev,imagery]"   # needs Python 3.10 or newer
python -m pytest -q                      # 300 passed — none touch data/

python -m src.ingest.nbi --year 2023 --year 2025
python -m src.ingest.nbe --year 2023 --year 2025
python -m src.catalog --record-missing

python -m src.analysis.contradictions --year 2023
python -m src.analysis.validate

python -m src.generate.brief --structure 013450 --year 2023 --print
python -m src.ui.server                  # http://127.0.0.1:8765

python -m src.eval.run_all
```

Every command exits non-zero with an actionable message when its input is missing. None
of them will invent anything to fill a gap.

### Configuration

All by environment variable; nothing hardcoded, no secret committed.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(unset)* | Required **only** for the optional LLM drafter |
| `BRIDGE_BRIEF_DRAFTER` | `template` | `template` (deterministic, default) or `llm` |
| `BRIDGE_BRIEF_MODEL` | `claude-sonnet-5` | Model id for the LLM drafter |
| `BRIDGE_BRIEF_DATA` | `./data` | Root of the data tree |

The core runtime is the **standard library only** — no web framework, no database
server, no network access. Pillow is an optional extra for imagery; without it those
paths report it is missing and everything else still runs.

---

## Documents

* **[README.md](README.md)** — what it does, setup, download links, run order
* **[HANDOFF.md](HANDOFF.md)** — step by step for when the data arrives
* **[ASSUMPTIONS.md](ASSUMPTIONS.md)** — every decision and format guess, with its change point
* **[CLAUDE.md](CLAUDE.md)** — the project specification
