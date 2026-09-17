# Handoff — what to do when the data arrives

Written to be followed without reading the code. Each step says where files go, what
command to run, what you should see, and what to send me if it goes wrong.

Nothing here downloads anything. You place the files; the commands read them.

---

## 0. Before anything

```bash
cd bridge-brief
python -m pip install -e ".[dev,imagery]"
python -m pytest -q
```

**Expect:** `286 passed`. These tests touch no data and must pass before you start.
If they do not, stop and send me the pytest output — the pipeline is not worth running
against a broken build.

---

## 1. Put the files in place

Create the tree and drop each download in. Do not rename, unpack the NBE ZIPs, or edit
anything: `data/raw/` is treated as immutable and nothing will ever write to it.

```
data/raw/nbi/2023/      the 2023 NBI delimited file(s), .txt or .csv
data/raw/nbi/2025/      the 2025 NBI delimited file(s)
data/raw/nbe/2023/AL/   Alabama 2023 element data (.zip or .xml)
data/raw/nbe/2023/AZ/   Arizona 2023
data/raw/nbe/2023/IA/   Iowa 2023
data/raw/nbe/2025/AL/   and the same three for 2025
data/raw/nbe/2025/AZ/
data/raw/nbe/2025/IA/
data/raw/codebrim/      CODEBRIM original images + annotation files
```

The **state subdirectory name matters** — it is where the parser gets the state from.
If your NBI download arrives as a ZIP, unpack it into the year directory; the NBE ZIPs
are read in place and should stay zipped.

---

## 2. Ingest the NBI

```bash
python -m src.ingest.nbi --year 2023 --year 2025
```

**Expect:** per-file progress every 25,000 rows, then a line per file like
`[done] 2023HwyBridgesDelimitedAllStates.txt: 623,xxx structures, N rejected`. It takes
a few minutes per year. It is resumable: if you interrupt it, re-run the same command
and it will redo the interrupted file and skip completed ones.

**If it stops immediately with `missing required column(s)`:** this is the most likely
failure, and it is deliberate — the loader will not guess which column holds the deck
rating. The error lists the headers it actually saw. **Send me that whole error
message.** The fix is one table, `FIELDS` in `src/ingest/nbi.py`.

**If it reports a large number of rejected rows:** run step 4 and look at the rejection
summary before continuing. A few thousand rejections across 624k rows is normal
(blank structure numbers); hundreds of thousands is a format problem.

---

## 3. Ingest the NBE — the step most likely to need a fix

```bash
python -m src.ingest.nbe --year 2023 --year 2025
```

**Expect:** a line per state file, `[read] AL.zip!elements.xml: N element records,
M structures so far`, then `[done]`.

**If it stops with `extracted no usable element records`:** this is expected to be
possible. I wrote the XML parser against the published NBE structure without ever
seeing a real file, and I said so in ASSUMPTIONS.md section D. The error prints the
parser's own statistics (how many candidate element nodes it saw, how many lacked a
structure number, and so on).

**What to send me:**

```bash
python - <<'PY'
import zipfile
with zipfile.ZipFile("data/raw/nbe/2023/AL/<the file>.zip") as z:
    name = [n for n in z.namelist() if n.endswith(".xml")][0]
    print(z.read(name)[:4000].decode("utf-8", "replace"))
PY
```

The first 4,000 bytes of one real file is enough. The fix is confined to
`extract_elements` and the tag-name lists directly above it in `src/ingest/nbe.py`.

---

## 4. See what you actually have

```bash
python -m src.catalog --record-missing
```

**Expect:** an ingested-files table, then per year: total structures, how many have NBI,
how many have NBE, **how many have both** (this is the population the engine can run
on), and a per-state breakdown for AL, AZ and IA. Then a rejected-rows-by-reason table.

**This is the milestone 1–2 gate.** The number that matters is *structures with both*.
If it is zero while both sources ingested fine, the join key is not matching — send me
the per-state table and I will look at the normalisation.

Also run this now, to commit real record shapes for review:

```bash
python -m scripts.extract_samples --year 2023
git add samples && git commit -m "samples: real records from the 2023 sources"
```

---

## 5. Run the contradiction engine

```bash
python -m src.analysis.contradictions --year 2023
```

**Expect:** progress every 2,000 structures, then a table of contradictions broken down
by direction (`nbi_optimistic` / `nbi_pessimistic`) and component with mean severity,
and a count of all findings by evidence tier.

**How to read it:** if it flags almost nothing, the thresholds are too tight; if it
flags nearly every structure, they are too loose. Both are tuning problems, not bugs,
and the whole tunable surface is the constants block at the top of
`src/analysis/contradictions.py`. `MIN_TOTAL_QTY` is the one I would touch first
(ASSUMPTIONS.md F5). **Send me the summary table and I will suggest values.**

Then try a quick look at a single structure before committing to a full run:

```bash
python -m src.analysis.contradictions --year 2023 --limit 200
```

---

## 6. The validation pass — the result

```bash
python -m src.analysis.validate --json reports/validation.json
```

**Expect:** counts of flagged components, how many were evaluable in 2025, how many
were confirmed / unchanged / contrary, the **control base rate** from unflagged
structures, and the **lift** between them — followed by an interpretation line.

**Read the lift, not the alignment.** If lift is at or below zero, the flags carry no
predictive information on this data, the report will say so plainly, and that is the
honest result to write up. A positive lift is an association on published records, not
a causal claim and not a safety judgement.

To get contradiction *precision*, a human has to look at some flags:

```bash
python -m src.analysis.validate --export-review reviews/sample.csv --sample-size 50
#  ... open reviews/sample.csv, fill the `verdict` column with correct / incorrect ...
python -m src.analysis.validate --read-review reviews/sample.csv
```

Each row carries the artifact IDs on both sides, so you can check a flag against the
source records without opening the code.

---

## 7. Imagery (optional — skip if the schedule is tight)

```bash
python -m src.ingest.uploads --structure 013450 --dir ~/inspection-photos --detect
python -m src.ingest.codebrim
python -m src.eval.codebrim_benchmark --json reports/codebrim.json
```

**Expect:** uploads copied into `data/uploads/013450/` and one `[detect]` line per
photo; the corpus registered; then a benchmark table with class-aware and
class-agnostic precision/recall/F1.

**Expect the numbers to be poor.** The shipped detector is a classical edge-energy
baseline, not a trained model, and its class-aware score is zero by construction
because it does not classify. That is the honest floor for the pipeline, and the report
says so in as many words. Swapping in a trained model means implementing
`src/detect/base.py::DefectDetector` and registering it; nothing else changes.

**If the benchmark skips every image with `no annotation file found`,** the annotation
layout differs from what I assumed. Send me one real annotation file; the fix is
`parse_annotation_file` in `src/eval/codebrim_benchmark.py`.

---

## 8. Generate a brief and review it

Pick a structure with a high-severity contradiction from step 5, then:

```bash
python -m src.generate.brief --structure 013450 --year 2023 --print
```

**Expect:** a draft brief with a counter block at the top —
`Sentences generated / Rendered / blocked_unsupported` — followed by sections, each
sentence tagged with its evidence tier and confidence and ending in its artifact IDs
in square brackets. Then:

```bash
python -m src.ui.server
# open http://127.0.0.1:8765
```

**In the UI:** the structure list, then a brief. Click any sentence and the artifacts it
cites highlight in the evidence panel. Approve, edit or reject per finding with your
name; sign off at the bottom. The sign-off trail is append-only — an edit keeps the
original text, which is what the correction-effort metric reads.

The UI binds to localhost and has no authentication. The reviewer name is typed, not
verified. Do not expose it (ASSUMPTIONS.md I1 and I4).

---

## 9. The metrics table

```bash
python -m src.eval.run_all \
    --review reviews/sample.csv \
    --benchmark reports/codebrim.json \
    --json reports/metrics.json
```

**Expect:** every metric from the specification, with its ground-truth source. Metrics
that could not be computed read `n/a` **with the reason and the command that would fill
them**. The footer says how many of the rows were computed.

Two rows to read carefully:

* `source_link_resolution` should be exactly `1.0`. It is 1.0 by construction when the
  grounding gate is working. **Anything below 1.0 is a bug report, not a measurement** —
  send it to me.
* `predictive_lift` is the real result of the project. `predictive_alignment` on its
  own, without the control rate beside it, means nothing.

---

## If something fails, send me

1. The **whole** error message, not the last line.
2. Which command you ran and which step number this was.
3. For a format failure, a small excerpt of the real file (the NBE snippet in step 3,
   the NBI header row, or one CODEBRIM annotation file). A few thousand bytes.
4. `python -m src.catalog` output, so I can see what state the index is in.

Do not edit `data/raw/` to make something parse. If a file needs different handling,
the handling is what changes.

---

## Rebuilding from scratch

The asset index is derived data and can always be thrown away:

```bash
rm -rf data/derived/assets.sqlite*      # originals under data/raw are untouched
```

Then re-run from step 2. Ingest is idempotent and artifact IDs are deterministic, so a
rebuilt index is byte-for-byte equivalent in every ID a brief could have cited.

---

## One housekeeping item

`CLAUDE.md` contains a block headed **"⚠️ CURRENT STATUS — the data is NOT on disk
yet"**. Delete that block once you have completed step 4 and the coverage table looks
right. Leaving it in place after the data arrives would make the next reader think
there is still nothing to run.
