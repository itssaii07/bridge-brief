# How to set up Bridge Brief on a new laptop

This guide takes a fresh Windows laptop to a running app. It takes about
**15 minutes plus download time**. macOS and Linux work too; the differences are
noted where they matter.

Already set up? See **[HOW_TO_RUN.md](HOW_TO_RUN.md)** instead.

---

## First: do you need the datasets?

**Short answer: the code runs without them, but the app is empty until the data is
in place.** The Git repository (about 1 MB) holds only the code, the tests and the
published results. It does **not** hold the datasets, and it never will.

| Without any data | With the data |
|---|---|
| The website opens and every page loads | You can search 632,140 real bridges |
| The tests pass (472 passed, 2 skipped) | You can upload photos of a bridge and get a brief |
| The evaluation page shows the committed results | The sign-off queue and live metrics fill in |
| **No bridge can be searched, so no brief can be generated** | Everything works |

Why the data is not in the repo:

- **It is large.** The raw downloads are about 0.5 GB (plus 4.8 GB for the optional
  image set), and the built index is about 3.4 GB. GitHub rejects files over 100 MB.
- **Some of it cannot be redistributed.** The dacl10k image set is licensed
  CC BY-NC 4.0 (non-commercial, attribution required).
- **The project promises never to alter the originals.** Keeping them out of Git
  means nobody can "fix" one by committing a change.

`data/` is listed in `.gitignore`, so nothing in it can be committed by accident.

### So if you send the repo to a friend, what do they do?

Your friend has two options. **Option B is much faster.**

**Option A: download the public datasets and build the index (about 10 minutes of
processing).** This is the clean way, and the only way to reproduce the results
from scratch. Follow step 4 below, then run `run.bat build`.

**Option B: copy your built index to them (no processing).** The app reads only
one file at run time:

```
data\derived\assets.sqlite      (about 3.4 GB)
```

Send them that file (zip it first, or put it on a USB stick or a shared drive). They
place it at the same path inside their copy of the repo and run `run.bat`. Search,
upload, brief generation, sign-off and export all work, because every record the
app cites is stored inside the index. They do not need `data\raw\` at all.

Three things to know about Option B:

1. **The source paths shown next to each record will be your paths**, for example
   `C:\Users\saima\Desktop\bridge-brief\data\raw\nbi\...`, in the app and in exported
   briefs. The record itself is in the index; only the "where it came from on disk"
   label points at your laptop. The app displays that path but never opens it.
2. **It includes your review history.** Any briefs, uploads, edits and sign-offs you
   made are in the file. Photographs you uploaded are *not* (those live in
   `data\uploads\`), so their images will not display on the other machine.
3. **Licences.** The bridge records (NBI and NBE) are U.S. federal public data. The
   index also holds dacl10k annotation data, which is CC BY-NC 4.0: sharing it
   privately for non-commercial use with attribution is allowed. **Never commit the
   index to Git or upload it publicly.**

**Do you need to re-download the data every time?** No. Once `data\derived\assets.sqlite`
exists, it stays there. Every later run reads it directly. You only rebuild if you
delete it or add new raw data.

---

## Step 1: Install Python

You need **Python 3.10 or newer**. The project was built and tested on 3.10.
Python 3.11 and 3.12 are the safest choices for a new install.

1. Download it from <https://www.python.org/downloads/>.
2. In the installer, **tick "Add python.exe to PATH"** on the first screen.
3. Open a new Command Prompt and check:

```bash
python --version
```

It should print `Python 3.10.x` or higher.

> **If you have more than one Python installed**, always install packages with
> `python -m pip ...`, never bare `pip`. On the original development laptop `pip`
> pointed at Python 3.14 while `python` was 3.10, so packages installed with bare
> `pip` were invisible to the project.

Git is optional. You need it only to clone; downloading the repo as a ZIP works too.

---

## Step 2: Get the code

```bash
git clone <your repo URL> bridge-brief
```

Or download the ZIP from GitHub and unzip it. Everything below assumes you are
inside the `bridge-brief` folder:

```bash
cd bridge-brief
```

---

## Step 3: Install the Python packages

The core of the project uses **only the Python standard library**. Three optional
packages add the imagery features, the Claude integration and the tests:

```bash
python -m pip install -e ".[dev,imagery,llm]"
```

| Extra | Package | What it enables |
|---|---|---|
| `imagery` | Pillow | Reading photographs, the defect detector, the image benchmark |
| `llm` | anthropic | Claude vision on uploaded photos, the optional LLM drafter |
| `dev` | pytest | The test suite |

`run.bat` does this step for you on first run. Check the install:

```bash
python -m pytest -q
```

**Expect `472 passed, 2 skipped`** on a laptop without the datasets, and `474 passed`
once the data is in place. The two skipped tests check that the committed samples in
`samples/` are verbatim excerpts of the real downloads, so they need the downloads to
compare against. Every other test runs on small fixtures in `tests/`.

---

## Step 4: Get the data

Skip this step if a friend gave you `assets.sqlite` (Option B above). Put the file at
`data\derived\assets.sqlite` and go to step 6.

Otherwise, download these public files and place them **exactly** as shown. Folder
names matter: the state folder name is how the loader knows which state a file is.
Do not rename or edit the files. Keep the NBE ZIPs zipped.

```
bridge-brief\
└── data\
    └── raw\
        ├── nbi\
        │   ├── 2023\   2023HwyBridgesDelimitedAllStates.txt
        │   └── 2025\   2025HwyBridgesDelimitedAllStates.txt
        ├── nbe\
        │   ├── 2023\
        │   │   ├── AL\  2023AL_ElementData.zip
        │   │   ├── AZ\  2023AZ_ElementData.zip
        │   │   └── IA\  2023IA_ElementData.zip
        │   └── 2025\
        │       ├── AL\  2025AL_ElementData.zip
        │       ├── AZ\  2025AZ_ElementData.zip
        │       └── IA\  2025IA_ElementData.zip
        └── dacl10k\    (optional)  dacl10k_v2_devphase\images\...  annotations\...
```

| Dataset | Where to get it | Size | Needed? |
|---|---|---|---|
| **NBI** (National Bridge Inventory) 2023 and 2025 | <https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm>. Choose the **delimited** file, **all states**, for each year. If it arrives as a ZIP, unzip the `.txt` into the year folder. | ~243 MB per year | **Yes** |
| **NBE** (National Bridge Elements) 2023 and 2025, for Alabama, Arizona and Iowa | <https://www.fhwa.dot.gov/bridge/nbi/elements.cfm>. One ZIP per state per year. | ~1.4 MB for all six | **Yes** |
| **dacl10k** bridge damage images | <https://dacl10k.s3.eu-central-1.amazonaws.com/dacl10k-challenge/dacl10k_v2_devphase.zip>. Unzip it inside `data\raw\dacl10k\`. | 4.76 GB | Optional: only for re-running the detector benchmark |

Why only three states for NBE? Element data is published per state, and Alabama,
Arizona and Iowa give 10,661 bridges that have both kinds of record. That is the
population the contradiction engine checks. Bridges in other states still work in
the app; their briefs just have no element data to cross-check.

**Disk space:** about 5 GB without dacl10k, about 15 GB with it (the ZIP plus the
unzipped copy).

---

## Step 5: Build the index

One command reads every raw file and builds `data\derived\assets.sqlite`:

```bash
run.bat build
```

On macOS or Linux, run the same steps by hand:

```bash
python -m src.ingest.nbi --year 2023 --year 2025       # about 1 minute per year
python -m src.ingest.nbe --year 2023 --year 2025       # a few seconds
python -m src.catalog --record-missing                 # coverage table
python -m src.analysis.contradictions --year 2023      # the contradiction engine
python -m src.ingest.dacl10k                           # optional, only if downloaded
```

What you should see: NBI reports **621,581 rows for 2023 and 624,193 for 2025, with 0
rejected**. NBE reports **66,597 and 55,905 element records, with 0 rejected**. The
catalog reports **10,661 structures with both sources in 2023**.

Every step is **resumable**. If one is interrupted, run it again: completed files are
skipped and the interrupted one is redone. If a step stops with an error, the message
names the missing or unreadable file.

Nothing in this step ever writes to `data\raw\`. All output goes to `data\derived\`.

---

## Step 6: Optional, switch on Claude vision

Without an API key the app reads photos with a simple classical detector (edge
analysis). With a key, **Claude reads each photograph** and describes the defects it
sees. Everything else works the same either way.

1. Get a key at <https://console.anthropic.com/>.
2. Create a file called `.env` in the `bridge-brief` folder containing:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

One setting per line, no spaces around `=`, no quotes. `run.bat` loads it
automatically. `.env` is in `.gitignore`, so the key is never
committed. **Never paste your key into any file that Git tracks.**

---

## Step 7: Run it

```bash
run.bat
```

Your browser opens at <http://127.0.0.1:8765>. Full usage is in
**[HOW_TO_RUN.md](HOW_TO_RUN.md)**.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Python 3.10 or newer was not found` | Install Python and tick "Add python.exe to PATH". Open a **new** Command Prompt afterwards. |
| `'run.bat' is not recognized` | You are not in the `bridge-brief` folder. `cd` into it, or double-click `run.bat` in File Explorer. |
| `ModuleNotFoundError: No module named 'PIL'` | Run `python -m pip install -e ".[dev,imagery,llm]"` with the **same** `python` you run the app with. |
| The search box finds no bridges | The index is missing or empty. Do step 4 and step 5, or copy in an index (Option B). |
| `No NBI directory for 2023` | The NBI file is not at `data\raw\nbi\2023\`. Check the folder names in step 4. |
| `Something is already running on port 8765` | The app is already open in another window. Use it, or run `run.bat 8766` for a second copy. |
| `UnicodeDecodeError` on a Windows machine | Make sure you are on the current code; every file read passes `encoding="utf-8"` and a test guards it. |
| The page opens but photos show "classical baseline" | That is expected without an API key. See step 6. |

---

## What goes where

| Folder | In Git? | Contents |
|---|---|---|
| `src/` | yes | All the code |
| `tests/` | yes | 474 tests; all but two run without the datasets |
| `samples/` | yes | 2 to 3 real records per source, for reading the format |
| `reports/`, `reviews/` | yes | Published results: metrics, validation, benchmark, review samples |
| `data/raw/` | **no** | Your downloads. Never modified by the project. |
| `data/derived/` | **no** | The built index, `assets.sqlite` |
| `data/uploads/` | **no** | Photographs uploaded through the app, stored byte for byte |
| `.env` | **no** | Your API key |
