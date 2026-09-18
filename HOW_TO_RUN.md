# How to run Bridge Brief

First time on this laptop? Do **[HOW_TO_SETUP.md](HOW_TO_SETUP.md)** first.

---

## The one command

On Windows, double-click **`run.bat`**, or in a Command Prompt inside the
`bridge-brief` folder:

```bash
run.bat
```

It checks Python, installs any missing packages, loads your `.env`, checks the data
index, starts the server and **opens your browser at <http://127.0.0.1:8765>**.

Keep the black window open while you use the app. **Press `Ctrl+C` in it to stop.**

| Command | What it does |
|---|---|
| `run.bat` | Start the app on port 8765 and open the browser |
| `run.bat 9000` | Same, on port 9000 |
| `run.bat build` | Build the data index from the raw downloads, then start the app |
| `run.bat test` | Run the test suite instead of the app |

### Without run.bat (macOS, Linux, or by hand)

```bash
python -m src.ui.server
```

Then open <http://127.0.0.1:8765>. Options: `--port 9000`, `--host 127.0.0.1`,
`--db path\to\assets.sqlite`. To use Claude vision this way, set the key in the
shell first (the `.env` file is read only by `run.bat`):

```bash
set ANTHROPIC_API_KEY=sk-ant-your-key-here
```

On macOS or Linux use `export ANTHROPIC_API_KEY=...` instead.

---

## Using the app: a two-minute demo

### 1. Start a new inspection

Click **New inspection** in the top bar.

1. **Pick a bridge.** Type a structure number, a road or a river. Good ones to try:

   | Type this | What you get |
   |---|---|
   | `AL012757` | The flagship example: the official record says the deck is "good" (7), the element data says 56% of it is in poor condition. The 2025 record later downgraded it to 6. |
   | `US 80` | Search by road |
   | `Cahaba River` | Search by what the bridge crosses |

   Bridges in **Alabama, Arizona and Iowa** have both kinds of federal record, so they
   are cross-checked for contradictions. Other states work too, with less to compare.

2. **Add photographs of that bridge.** JPEG, PNG, WebP, TIFF or BMP, up to 40 MB each,
   up to 24 at a time. Drag them onto the drop zone or click to browse.

   Use photos **of the bridge you picked**. Every uploaded photo is labelled as
   inspection evidence for that bridge, so a photo of a different bridge would be
   mislabelled evidence. Close-ups of concrete cracks, rust stains, spalling,
   white salt deposits or exposed steel bars give the detector something to find.

3. **Choose who reads the photographs.** **Claude vision** (needs an API key; it
   boxes, classifies and describes each defect) or the **Classical baseline** (no key
   needed; it finds unusual texture but does not name the defect). Claude vision is
   selected automatically when a key is set.

4. **Click Generate brief.** You watch each stage happen live: anchor to the federal
   record, preserve the originals, propose defect regions, cross-check the condition
   records, draft through the grounding gate.

### 2. Read the report

The report page is the core of the project. Look for:

- **Photos with boxes drawn on them.** Each box is a proposed defect region, with its
  own ID, such as `IMG-AL012757-deck_crack_3f9a1c2b7e-r2` (bridge, photo name plus a
  fingerprint of its contents, region number). Click **Open original** to see the
  exact file you uploaded, unmodified.
- **Every sentence has chips** under it. Each chip is the ID of the record it came
  from. **Click a sentence** and its evidence lights up.
- **Evidence tier and confidence on every finding:** `conflicting` (sources disagree),
  `corroborated` (two sources agree) or `single_source`.
- **The blocked counter.** Sentences the system tried to write but could not back with
  a record were dropped, and the count is shown. This is a feature: it proves nothing
  unsupported slipped through.
- **Missing evidence**, listed openly. For example: no sensor readings, no field notes.

### 3. Review and sign off

Still on the report page:

1. Type your name in the **Your name** box.
2. **Approve, Edit or Reject** individual sentences. Each action is recorded.
3. Click **Sign off** (or **Reject brief**). After that its sentences can no longer
   be edited, and the decision is kept in a permanent audit trail.
4. Only now does **Publish** work. Before sign-off it is refused, on purpose.

The app never marks a bridge safe and never orders maintenance. It drafts; a named
person decides.

### 4. The other pages

| Page | What it shows |
|---|---|
| **Overview** `/` | Every requirement from the problem statement, with the live number behind it |
| **Sign-off queue** `/queue` | Briefs waiting for a reviewer, most urgent first |
| **Evaluation** `/metrics` | The full metrics table. Where a number cannot be computed, the reason is shown instead of a made-up value. |
| **Records** `/structures` | Browse the federal records for any bridge |

---

## Running the pipeline from the command line

The web app does all of this for you. These commands are for reproducing the
published results, or scripting.

```bash
# Draft a brief for one bridge and print it
python -m src.generate.brief --structure AL012757 --year 2023 --print

# Upload photos for a bridge from a folder, with detection
python -m src.ingest.uploads --structure AL012757 --dir C:\photos --detect

# The 2023 -> 2025 validation: did flagged bridges get downgraded?
python -m src.analysis.validate --json reports/validation.json

# The detector benchmark against dacl10k (needs dacl10k, slow)
python -m src.eval.codebrim_benchmark --corpus dacl10k --json reports/detector_dacl10k.json

# The whole metrics table
python -m src.eval.run_all --benchmark reports/detector_dacl10k.json --json reports/metrics.json

# Publish a signed-off brief. Refuses anything not signed off; there is no override.
python -m src.generate.export --brief BRIEF-AL012757-2023-v1 --out reports/brief.md
```

Every command exits with an error and a clear message if its input is missing. None
of them invent data to fill a gap.

---

## Settings

All optional. Put them in `.env` (read by `run.bat`) or set them in the shell.

| Setting | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | not set | Turns on Claude vision for photos, and the LLM drafter |
| `BRIDGE_BRIEF_VISION_MODEL` | `claude-opus-5` | Which Claude model reads the photos |
| `BRIDGE_BRIEF_DRAFTER` | `template` | `template` writes sentences from fixed templates; `llm` asks Claude to phrase them. Both go through the same citation check. |
| `BRIDGE_BRIEF_MODEL` | `claude-sonnet-5` | Which Claude model the LLM drafter uses |
| `BRIDGE_BRIEF_DATA` | `.\data` | Where the data folder is, if not inside the repo |

---

## Safety notes

- The server listens on **127.0.0.1 only**, meaning only your own laptop can reach it.
  It has no login, and reviewer names are typed, not verified. Do not expose it to a
  network.
- Uploaded photos are stored under `data\uploads\`, named by their own SHA-256 hash,
  and never modified or overwritten.
