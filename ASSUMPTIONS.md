# Assumptions and decisions

Everything here is a decision I made that `CLAUDE.md` or the kickoff prompt did not
settle, or a guess about a file format I could not verify because the data is not on
disk. Each entry says what I assumed, why, and — where it matters — the single place
to change it if I guessed wrong.

**Nothing in this repository has been run against real data.** No dataset was
downloaded, no file was created under `data/`, and no metric in any document is a
measured number. Every number you see in output is produced at run time or not at all.

---

## A. Environment and dependencies

**A1. The runtime is the Python standard library.**
Ingest, the contradiction engine, brief generation, the eval harness and the review UI
use only stdlib (`sqlite3`, `csv`, `xml.etree`, `http.server`, `json`, `hashlib`). No
web framework was added: the review UI is a few pages and a handful of POST endpoints,
and `http.server` carries that without earning a dependency. *Change point:* if you
would rather have Flask, `src/ui/server.py` is the only file affected.

**A2. Pillow is an optional extra, needed only for imagery.**
Milestones 5 and 6 (tiling, the detector, the CODEBRIM benchmark) need image decoding.
Pillow is imported defensively; without it those paths report that Pillow is missing and
exit cleanly, and everything else still runs. Install with `pip install -e ".[imagery]"`.

**A3. The LLM is optional and off by default.**
The default sentence drafter is deterministic and template-based, so a brief can be
produced with no API key and no network. An LLM drafter is available behind the same
interface (`ANTHROPIC_API_KEY`, `BRIDGE_BRIEF_DRAFTER=llm`). The grounding gate is
deterministic code in both cases and does not trust the drafter (see G1).

**A4. Python 3.11+.** Uses `X | Y` type syntax and `tomllib`-era stdlib behaviour.

---

## B. Structure-number normalisation (`src/ids.normalise_struct`)

This is the single shared function required by the kickoff prompt; every module calls it
and nothing re-implements it. Both the raw and normalised forms are stored
(`structures.struct_raw`, `structures.struct_norm`).

**B1. Rules applied, in order:** upper-case → drop every character outside `[A-Z0-9]` →
strip leading zeros → if the result is all digits and shorter than 6 characters, left-pad
with zeros to width 6.

**B2. Width 6 is a guess taken from `CLAUDE.md`,** which writes `013450` throughout. NBI
item 8 is a 15-character field and states use it differently. The padding rule only
affects short all-numeric identifiers; anything longer keeps its natural width.

**B3. Punctuation is removed, not preserved.** Two reasons: padding and separators are
exactly the inconsistency we are normalising away, and artifact IDs are hyphen-delimited,
so a structure number containing `-` would make IDs ambiguous to parse. *Risk:* if a
state distinguishes two structures only by punctuation (`12-345` vs `123-45`), they would
collide. I judged this unlikely but did not verify it. `src/catalog.py` reports the
number of distinct raw spellings per normalised key, which is where such a collision
would show up as an implausible count.

**B4. An all-zero or empty structure number is an error, not a default.** It is logged to
`rejected_rows` with a reason and the row is skipped.

---

## C. NBI file format (`src/ingest/nbi.py`)

Written against the FHWA delimited format described in the kickoff prompt, and verified
against nothing, because no file is on disk.

**C1. Columns are resolved by header name, never by position,** as required. The header
row is read first and mapped through a synonym table (`NBI_FIELD_SYNONYMS`); the FHWA
item number embedded in the name (`..._058`) is used as the primary signal, with the
full documented name as a fallback, because FHWA has published both
`DECK_COND_058` and `DECK_COND` spellings across releases.

**C2. A missing *required* column fails loudly** with a message naming the column and
listing the headers actually present. Required = structure number, state code, and the
three component ratings. A missing *optional* column (latitude, facility, …) is logged
once and the field stored as NULL.

**C3. The text qualifier is a single quote (`'`),** per the prompt.
`csv.reader(..., quotechar="'")` handles it. *Change point:* `NBI_DIALECT` in
`src/ingest/nbi.py`.

**C4. Encoding is `latin-1` with `utf-8` tried first.** The files are described as ASCII,
but state-supplied free-text fields routinely carry stray high bytes, and refusing to
read a 624k-row file over one byte in a facility name would be the wrong failure.
Decoding fallbacks are counted and reported.

**C5. Rating values.** Item 58/59/60/62 are single characters `0`–`9`, or `N` for "not
applicable". `N`, blank and any unexpected value are stored as `rating = NULL` with the
original characters kept in `rating_raw`. A NULL rating never participates in a
contradiction — absence of a rating is not evidence of a good one.

**C6. Culverts.** Item 62 is the culvert rating; on a culvert structure, items 58–60 are
typically `N`. The engine treats a NULL component as "not applicable here" and skips it
rather than reading it as 0.

**C7. One file or many.** The loader accepts a directory and processes every `*.txt` /
`*.csv` in it, or a single file. Per-file progress and resumability are keyed on the
file's SHA-256 recorded in `ingest_log`.

---

## D. NBE file format (`src/ingest/nbe.py`) — the biggest guess in the project

The prompt says explicitly that the exact shape cannot be known without a real file, and
asks that the element-extraction step be isolated behind one small function and the
assumption recorded in one place. It is:
**`src/ingest/nbe.py :: extract_elements(root, ...)`** — that function, and the XPath
constants immediately above it, are the only things that should need editing.

**D1. Assumed shape.** One XML document per state per year, containing a sequence of
structure elements, each carrying a structure number attribute or child, and within it a
sequence of element records each carrying: an AASHTO element number, a total quantity,
units, and four condition-state quantities. I match on *local tag names,
case-insensitively, ignoring XML namespaces*, against a list of candidate names for each
concept (`ELEMENT_NUM_KEYS`, `CS_QTY_KEYS`, …), and accept the value whether it appears
as an attribute or as a child element's text.

**D2. Condition-state quantities may be either four fields (`CS1..CS4`) or four repeated
child records.** Both are handled; whichever is found first wins, and which was used is
recorded in `ingest_log.message` so you can see what the parser actually did.

**D3. ZIP input is supported directly.** The prompt says one ZIP per state per year, so
the loader reads `.zip` without unpacking into `data/raw/` — that directory is never
written to.

**D4. The state is taken from the directory name** (`data/raw/nbe/{year}/{state}/`),
falling back to a state field inside the XML if present. AL, AZ and IA are the expected
set and the catalog reports them separately.

**D5. If the parser finds zero elements in a file it fails loudly** rather than recording
an empty state. A structure with genuinely no elements and a file we failed to parse look
identical in the database otherwise, and confusing the two would corrupt the
missing-evidence metric.

---

## E. Element → NBI component mapping (`src/analysis/element_map.py`)

The contradiction engine has to compare an NBI component rating with element condition
states, so it needs to know which AASHTO elements belong to which component. The mapping
uses the AASHTO MBE national element numbering:

- **Deck/slab:** 12, 13, 15, 16, 17, 18, 22, 28, 29, 30, 31, 38, 54, 60, 65
- **Superstructure:** 102, 104, 105, 106, 107, 109, 110, 111, 112, 113, 115, 116, 117,
  120, 121, 126, 131, 135, 136, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 152,
  154, 155, 156, 161, 162
- **Substructure:** 155, 202, 203, 204, 205, 206, 207, 208, 210, 211, 212, 213, 215,
  216, 217, 218, 219, 220, 225, 226, 227, 228, 229, 231, 233, 234, 235, 240, 241
- **Culvert:** 240, 241

**E1. Elements not in the table are ingested and stored but do not drive a contradiction.**
They are still citable artifacts. Unmapped element numbers encountered during analysis are
counted and reported so the table can be extended from real data rather than from memory.

**E2. Element 155 appears under both superstructure and substructure** in different state
practices. It is mapped to superstructure for contradiction purposes and flagged as
ambiguous in the finding detail. *Change point:* `AMBIGUOUS_ELEMENTS`.

**E3. Protective-system elements (defect and protection elements, 5xx/8xx) are excluded**
from component roll-ups: they describe coatings and wearing surfaces, not the component's
structural condition, and including them inflates apparent deterioration.

---

## F. Contradiction thresholds (`src/analysis/contradictions.py`)

`CLAUDE.md` gives the shape of a contradiction and one worked example but no numeric
thresholds. These are mine, they are all named constants in one block at the top of the
module, and every finding records the threshold values that produced it so a later
re-tune is auditable.

**F1. NBI rating bands.** 0-4 = poor, 5-6 = fair, 7-9 = good, following the FHWA
good/fair/poor classification used in National Bridge Inventory condition reporting.

**F2. Element-implied bands.** For a component, `deteriorated_fraction =
(CS3 + CS4 quantity) / total mapped quantity`. CS2 ("fair") is deliberately excluded from
the numerator: a bridge with most of its area in CS2 and a rating of 6 is not a
contradiction. The implied band is then:

- **poor** — `deteriorated_fraction >= 0.20`, or `CS4 fraction >= 0.02` (CS4 is "severe";
  even a small severe fraction is not a good component)
- **fair** — `deteriorated_fraction >= 0.05`, **or** absolute deteriorated quantity
  `>= 250 units`
- **good** — otherwise

**F3. The absolute-quantity rule exists to reproduce the worked example in `CLAUDE.md`.**
That example — deck rated 7, 340 sq ft of deck in CS3 — is a contradiction by
specification, but on a typical 8,000 sq ft deck it is only 4.3% deteriorated and a
fraction-only rule would miss it. `ABS_DETERIORATED_QTY = 250` makes a materially large
defect area count regardless of how large the denominator is. The units are whatever the
source publishes, which means the constant is only meaningful for area-scaled elements;
this is a real weakness of the rule and a thing to revisit against real data.

**F4. A contradiction is a disagreement of one band or more**, in either direction, once
the gates pass. The NBI-pessimistic direction (rating worse than the elements) is
additionally required to have `deteriorated_fraction <= 0.02` — the elements must be
near-pristine before we will call a poor rating a disagreement, since a low rating often
reflects something an element inspection does not capture. Both directions are flagged
because suppressing the second would bias the engine toward the story it is looking for.

**F5. Minimum quantity gate.** A component whose total mapped quantity is below
`MIN_TOTAL_QTY = 100` produces no contradiction; below that, one small element in CS3
swings the fraction and the finding is noise. Structures gated out are reported as
single-source, never as agreement. This is the single threshold I would most want to tune
against real distributions.

**F6. Severity is continuous:** `min(1, base * (0.8 + 0.4 * magnitude))` where
`base = band_distance / 2` and `magnitude` scales with how far past the deciding threshold
the element evidence sits. A one-band disagreement scores 0.4-0.6, a two-band
disagreement 0.8-1.0 — band distance always dominates.

**F7. Confidence is not severity.** Confidence expresses trust in the *observation* and is
reduced, with a stated reason attached to the finding, when the total quantity is near the
gate, when a mapped element reported no quantity, when an ambiguous element is in the
roll-up, or when the denominator had to be inferred. Severity expresses how bad the
disagreement is. They are separate columns, separately displayed, and floored at 0.3.

**F8. Every finding records the threshold values that produced it** in its detail payload,
so a later re-tune is auditable rather than archaeological.

**F9. All three tiers are emitted, never inferred by the UI.** A contradiction is
`conflicting` by construction. Agreement between NBI and NBE is `corroborated`. NBI with
no element data to check it — or with a quantity too small to reason from — is
`single_source` with confidence capped at 0.6.

---

## G. Brief generation and the grounding gate (`src/generate/`)

**G1. The gate is deterministic code and does not trust the drafter.** Every candidate
sentence passes through `src/generate/grounding.py`, which extracts artifact IDs from the
sentence's own citation list *and* from its text, validates each through `src.ids.parse`,
and requires that each resolves to a row in `artifacts`. Zero surviving citations → the
sentence is dropped, written to `blocked_sentences` with a reason, and counted in
`blocked_unsupported`. A sentence citing an ID that does not resolve is treated as having
zero citations; a fabricated citation must not be able to launder a sentence through.

**G2. Citations are rendered inline as `[ID]` and are part of the sentence text.**
The UI parses them back out to drive the click-to-highlight behaviour, so what the
inspector reads and what the system verified are the same string.

**G3. The default drafter is template-based.** Findings carry structured payloads; the
templates turn them into sentences. This keeps milestone 7 runnable with no API key, and
means the gate's counter is exercised by the LLM path rather than depended on by it.

**G4. `blocked_unsupported` is stored per brief and shown in the UI** as required by
invariant 2, alongside the unsupported-content rate (blocked / total generated).

---

## H. Imagery (milestones 5 and 6)

**H1. The bundled detector is a classical baseline, not a trained model.**
`src/detect/baseline.py` is a deterministic edge/texture-response detector over image
tiles. It is behind `src/detect/base.py :: DefectDetector`, so a trained model can be
dropped in without touching the pipeline. I did not train or download a model: the prompt
forbids downloads, and a benchmark number from an untrained baseline is an honest number
that will simply be low. **Expect poor precision/recall from it** — that is the correct
outcome to report, not a failure to hide.

**H2. Every image row carries `provenance` and the column is `NOT NULL` with a CHECK
constraint.** CODEBRIM images are ingested as `reference_corpus` with `struct_norm =
NULL`, so a corpus image is structurally incapable of being attached to a bridge
(invariant 6). The UI labels both kinds visibly.

**H3. Uploads go to `data/uploads/{structure}/` and originals are copied, never moved or
re-encoded.** Tiles and any derived rendering are written under `data/derived/tiles/`.

**H4. CODEBRIM annotation format is assumed to be per-image XML in Pascal-VOC style**
(`<object><name>…</name><bndbox>…</bndbox></object>`), which is what the published
release uses for its bounding-box annotations. Parsing is isolated in
`src/eval/codebrim_benchmark.py :: parse_annotation_file`. If the real layout differs,
that one function is the change point.

**H5. Benchmark matching rule:** a detection matches an annotation when IoU ≥ 0.5 and the
defect class agrees; greedy matching by descending confidence; unmatched detections are
false positives, unmatched annotations false negatives. Class-agnostic scores are also
reported, because the baseline detector localises better than it classifies.

---

## I. Review UI and sign-off

**I1. Reviewer identity is a name typed into the UI, not an authenticated account.**
There is no auth system; this is a single-operator research tool. The name is recorded on
every review action and sign-off. *If this were deployed, this is the first thing that
needs replacing* — the audit trail is only as good as the identity behind it.

**I2. `review_actions` and `signoffs` are append-only.** Nothing updates or deletes a row.
The current state of a finding is its most recent action. An edit stores both the text
before and after, which is also how correction effort is measured.

**I3. No endpoint can mark a structure cleared, safe, or scheduled.** There is no such
column and no such route. Sign-off marks *the brief* as reviewed by a named human, and
the brief's own language is drafting and recommendation only (invariant 4).

**I4. The UI binds to 127.0.0.1 by default** and is not hardened for exposure.

---

## J. Validation pass (milestone 4)

**J1. "Did the rating drop?" means the 2025 NBI rating for the same structure and the same
component is strictly lower than the 2023 rating.** Both must be non-NULL; a structure
missing from the 2025 file, or with a NULL rating, is counted separately as
*unevaluable* and excluded from the denominator rather than counted as a miss. Reporting
an unevaluable structure as a negative would understate precision by an unknown amount,
which is worse than a smaller denominator that is honestly labelled.

**J2. A control group is computed.** The same drop rate is measured over structures that
have both NBI and NBE data in 2023 and were *not* flagged. Predictive alignment without a
base rate is not a result — if flagged and unflagged bridges drop at the same rate, the
engine has found nothing, and the harness will say so.

**J3. Contradiction precision requires a human.** The harness samples flagged
contradictions, writes them to a review file with their artifact IDs, and computes
precision from the labels a human puts back. It does not score itself.

---

## K. Things I could not check and would most want a real sample for

1. **One real NBE XML file** for any state/year — the parser in D1/D2 is educated
   guesswork and is the most likely thing to need a fix.
2. **One real NBI header row** — to confirm the exact column spellings in C1.
3. **One CODEBRIM annotation file** — to confirm H4.
4. **The real distribution of element total quantities**, to set `MIN_TOTAL_QTY` (F4)
   from data instead of from judgement.
5. **Whether NBE `total_qty` is published per element or must be summed from the four
   condition states.** The parser handles both and records which it used, but which is
   authoritative affects F2.

---

## L. Things decided while building that nobody asked about

**L1. The review UI is stdlib `http.server`, not Flask.** The UI is five pages and four
POST endpoints. A framework would have been a dependency carrying no weight. *Change
point:* `src/ui/server.py` is the only file that would need rewriting.

**L2. Findings are regenerated, not accumulated.** Finding IDs are deterministic
functions of what the finding is about, so re-running the engine supersedes the previous
result for the same structure, year and component rather than producing duplicates.
Briefs work the other way — each generation is a **new version**, because a brief may
already have been reviewed and rewriting it under a reviewer would destroy the trail.

**L3. `corroborated` findings are emitted, not just contradictions.** They cost little,
they let the brief say "two independent sources agree" with citations, and they are the
control population for the validation pass. Without them the brief would only ever
speak where there was a problem, which reads as a much more alarming document than the
records support.

**L4. A component that the engine could not evaluate is reported `single_source`,
never as agreement.** This applies both to structures with no element data and to
structures gated out by `MIN_TOTAL_QTY`. Silence would have been the dangerous default.

**L5. Gap sentences cite the records that do exist.** A sentence saying "no NBE data is
available for this structure" is still a claim about this structure's record, so it
cites the NBI artifacts for that structure and year. Without an anchor it would carry
zero artifact IDs and the grounding gate would drop it — correctly, by its own rule —
and the brief would lose exactly the statement that matters most for missing evidence.

**L6. Coordinates are parsed from the packed NBI `DDMMSSss` form** and stored as decimal
degrees, with west longitude negated. Anything that does not parse cleanly, or lands
outside the valid range, is stored NULL. No finding depends on a coordinate; they exist
so the UI can eventually show where a structure is.

**L7. The FHWA state-code to postal-abbreviation table is used only for labelling.** An
unrecognised code yields NULL, not a guess. It matters because the per-state coverage
breakdown for AL, AZ and IA depends on it; if that table were wrong, those rows would
read zero and the NBE join would look broken when it was not.

**L8. Reviewer identity, again, because it is the weakest point in the system.** Review
actions and sign-offs record a typed name with no authentication behind it. For a
research tool run by one operator this is adequate; for anything real it is the first
thing that must be replaced, because the entire audit trail rests on it.

**L9. Tests are permitted to fabricate sentences and citations, and do.** Testing the
grounding gate means feeding it a sentence citing an artifact that does not exist —
that is precisely the case that must fail closed. This is logic testing, not data
fabrication: no file is written under `data/`, and no fabricated object ever reaches a
brief or a metric. `tests/test_invariants.py` guards the distinction by failing if
anything is ever committed under `data/`, if `samples/` gains an unexplained file, or
if a mock-data generator appears in `src/`.

**L10. The module boundary is enforced by a test, not by good intentions.**
`tests/test_invariants.py` fails if the ingest layer imports from generation, or if the
analysis layer imports from generation or the UI.
