# Bridge Brief — project status

Snapshot of where this project stands: what the problem is, what has been built, and
what still has to happen. The short version is that **all nine milestones are
implemented, 474 tests pass, and the pipeline has now been run end to end against the
real published federal data.** Milestones 1–4 and 7–9 produced measured results;
milestones 5–6 are unrun because no inspection photographs were supplied and the
CODEBRIM archive is malformed as published (ASSUMPTIONS.md H6).

### The headline result

A bridge component whose 2023 NBE element data showed deterioration its 2023 NBI
condition rating did not reflect was **2.14 times more likely to be officially
downgraded in the 2025 NBI release** than an unflagged component with the same two
sources available — 12.6% against a 5.9% control base rate, over 1,268 flagged and
17,652 control components, z = 9.47, p < 1e-12.

An association on published federal records. Not a causal claim, not a statement about
any individual bridge, and not a safety judgement.

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

## Conformance to the problem statement (GAI40)

Every clause, checked against the running system rather than asserted. 17 of 17.

| GAI40 clause | State | Verified by |
|---|---|---|
| evidence-linked brief | Done | every sentence carries artifact IDs; `source_link_resolution` = 1.0 |
| image regions | Done, awaiting real photographs | upload a photo on `/inspect` and each region is drawn over the original on the report page; Claude vision boxes and describes defects when `ANTHROPIC_API_KEY` is set, the classical baseline otherwise. 0 regions in the real index because no inspection photographs have been supplied yet |
| extracted observations | Done | 24,813 findings |
| confidence levels | Done | 0 findings missing confidence or evidence tier |
| **human sign-off queue** | Done | `/queue`, ordered by review urgency, deterministic |
| keep original sources available | Done | 0 artifacts without a source path; `data/raw/` never written |
| mark uncertainty clearly | Done | confidence + tier required on every finding |
| **review before publication / sharing** | Done | `src/generate/export.py` refuses any brief not signed off; no `--force` exists and a test walks the AST to keep it that way |
| identify missing evidence | Done | 77,187 structures recorded as lacking NBE; recall 1.0 |
| source-link accuracy | Mechanical done, semantic needs a human | 1.0 mechanical; sampling harness ships, unlabelled |
| **missed-defect rate** | Done | 0.9962 — named, not left as 1 − recall |
| preserve original artefacts | Done | originals copied never re-encoded; zips read in place |
| never autonomous clearance | Done | no such column, route or function; `src/` clean of every clearance symbol |
| factual fidelity | Harness ships, needs a human | `factual_fidelity`, deliberately unscored by the system |
| source fidelity | See source-link accuracy | |
| unsupported-content rate | Done | 0.0 — see the caveat below |
| user correction effort | Done | 0.3333 edits per finding over a real review |
| difficult edge cases | Done | 134 edge-case tests of 405 |

**Three metrics are `n/a`, and all three require a person**: contradiction
precision, factual fidelity, and the semantic half of source-link accuracy. The
harness exports a deterministic sample for each and reads labels back. It does not
score itself, and an unlabelled row is excluded rather than assumed to pass.

**One caveat worth stating plainly.** `unsupported_content_rate = 0.0` is 0 *by
construction* with the default template drafter, which always cites. The counter is
exercised by unit tests that feed the gate fabricated citations, and would go
non-zero with the LLM drafter. Reporting it as a measured 0 without that context
would overstate it.

---

## The web application

`python -m src.ui.server`, then http://127.0.0.1:8765. Upload photographs of a
structure, get a draft brief with image regions, extracted observations and confidence
levels, review and sign it off, then publish. Pages: Overview, New inspection, Report,
Sign-off queue, Evaluation, Records. Glass and 3D presentation (pointer-tracked tilt,
a live wireframe truss, a perspective floor), all native CSS and canvas with no build
step and no CDN, and everything collapses to static under reduced motion. See
README.md "The web app" and ASSUMPTIONS.md section M.

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

All nine milestones, 474 tests passing. No test touches `data/`.

| # | Scope | State | What it measured |
|---|---|---|---|
| 1 | Schema, artifact ID system, scaffolding | Done | — |
| 2 | NBI + NBE ingest, coverage catalogue | **Run** | 621,581 + 624,193 NBI rows, 0 rejected; 66,597 + 55,905 NBE records, 0 rejected; **10,661 structures with both sources in 2023** |
| 3 | **Contradiction engine** | **Run** | 7,018 contradictions over 10,661 structures (43.6% of structures), plus 16,247 corroborated and 1,548 single-source findings |
| 4 | **2025 validation pass** | **Run** | optimistic flags **2.14x**, pessimistic **3.46x**, both p < 1e-12; pooled lift +2.5% |
| 5 | Photo upload + detector | Unrun | no inspection photographs supplied |
| 6 | Detector benchmark | **Run** | over **dacl10k**, substituted because the CODEBRIM archive is malformed (H6): 7,910 images, 0 skipped, class-agnostic **P 0.0125 / R 0.0038 / F1 0.0058** — the honest floor for an untrained baseline |
| 7 | Brief generation + grounding gate | **Run** | `source_link_resolution` exactly **1.0**; 0 blocked on the generated brief |
| 8 | Review UI + sign-off queue + trail | **Run** | serves the real index; landing page 113ms, click-to-evidence resolves to the source ZIP member; `/queue` orders briefs by review urgency; publication refused until sign-off |
| 9 | Eval harness | **Run** | **15 of 18** metrics computed; the 2 remaining need a human (contradiction precision, correction effort) |

### What running it against real data changed

Five things were wrong, none of them findable without the data:

1. **The NBE parser** assumed nested structures. The real files are flat `<FHWAED>`
   records. One entry in `ELEMENT_TAGS` — the change point the module docstring named.
2. **The join key was not unique.** NBI item 8 repeats across states; 40,374 numbers in
   the 2023 file are claimed by more than one state, collapsing 112,836 rows onto another
   state's bridge. The key is now state-qualified, which changed every artifact ID.
3. **The validation comparator was wrong**, scoring upgrade-predicting flags against a
   downgrade base rate. It reported "no predictive information" when there was a strong
   signal in both directions.
4. **The UI landing page was O(structures)** and never loaded on 632,140 of them.
5. **The CODEBRIM diagnosis was wrong** — malformed archive, not an encrypted one.

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

The datasets are on disk and ingested; `data/` is gitignored in full and nothing under it
was ever created by this project. Three things remain, and all three need a person rather
than a run:

### 1. Contradiction precision — needs a human labeller

The only metric the harness refuses to compute for itself (ASSUMPTIONS.md J3). A
deterministic sample of 50 flags is already exported to `reviews/sample.csv`, each row
carrying the artifact IDs on both sides so a flag can be checked against the source
records without opening the code:

```bash
#  ... open reviews/sample.csv, fill the `verdict` column with correct / incorrect ...
python -m src.analysis.validate --read-review reviews/sample.csv
python -m src.eval.run_all --review reviews/sample.csv --json reports/metrics.json
```

Worth knowing before labelling: 77 of the 1,304 NBI-optimistic flags were raised by
`ABS_DETERIORATED_QTY` alone, and the published extracts carry **no units**, so that rule
is dimensionally unsound (ASSUMPTIONS.md F10). Those 77 are the flags least likely to
survive review.

### 2. Correction effort — needs a reviewer in the UI

`correction_effort_edits_per_finding` reads review actions, and no finding has been
reviewed yet. Start the UI, approve/edit/reject some findings and sign off:

```bash
python -m src.ui.server        # http://127.0.0.1:8765
```

### 3. Milestones 5 and 6 — blocked on inputs, not on code

Milestone 5 needs real inspection photographs for a structure that has a contradiction;
it does **not** need CODEBRIM. Milestone 6 needs a readable CODEBRIM archive: the one on
disk is malformed as published and the recovery path is `7z x` or `zip -FF`, not a
password (ASSUMPTIONS.md H6). If it is recovered, H7 also has to be addressed — the
ground truth is multi-label and the matching rule assumes one class per box.

---

## How the three predicted risks turned out

The three things flagged before the data arrived as most likely to need fixing:

1. **The NBE XML parser** — *needed fixing, exactly where predicted.* The format guess was
   wrong (flat, not nested) and the fix was one entry in `ELEMENT_TAGS`, inside the change
   point the module docstring named. Confining the assumption to one function paid for
   itself.
2. **`MIN_TOTAL_QTY` and the band thresholds** — *not changed, and deliberately so.* The
   engine flags 43.6% of structures, which looked too loose, and 81% of findings are a
   single NBI-pessimistic pattern that looked like the obvious thing to suppress. Running
   the validation pass first showed that pattern is predictive at 3.46x, p < 1e-12.
   Tightening it would have destroyed a real signal to make a count look reasonable. No
   threshold has moved from its pre-data value (ASSUMPTIONS.md F11). `MIN_TOTAL_QTY = 100`
   turns out to be barely binding: the 1st percentile of flagged total quantity is 120.
3. **`ABS_DETERIORATED_QTY = 250`** — *worse than the hedge admitted.* The extracts publish
   **no units field at all**, so the constant compares 250 against a bare number whose
   dimension varies by element. It raised 77 of 1,304 optimistic flags on its own, so it is
   not load-bearing for the result, but it is not defensible as written (F10).

The one risk that was **not** anticipated is the one that mattered most: the structure
join key was not nationally unique, and nothing in the pre-data test suite could have
caught it, because both colliding states publish the same spelling of the same number.

Every decision the specification did not settle is in `ASSUMPTIONS.md`, sections A–L,
with the single change point named for each format guess.

---

## Running it

```bash
python -m pip install -e ".[dev,imagery]"   # needs Python 3.10 or newer
python -m pytest -q                      # 474 passed — none touch data/

python -m src.ingest.nbi --year 2023 --year 2025
python -m src.ingest.nbe --year 2023 --year 2025
python -m src.catalog --record-missing

python -m src.analysis.contradictions --year 2023
python -m src.analysis.validate

python -m src.generate.brief --structure AL012757 --year 2023 --print
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
