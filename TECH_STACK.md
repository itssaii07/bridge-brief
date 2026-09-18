# Tech stack

Bridge Brief is deliberately small. The core runs on **plain Python with no
third-party packages**: no web framework, no database server, no JavaScript build
step, no CDN. Three optional packages add imagery, Claude and testing.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Browser    HTML + CSS + vanilla JavaScript  (glass UI, 3D canvas)      │
├─────────────────────────────────────────────────────────────────────────┤
│  Server     Python http.server  (ThreadingHTTPServer, NDJSON streaming) │
├─────────────────────────────────────────────────────────────────────────┤
│  Logic      Python 3.10 standard library                                │
│             ingest · contradiction engine · grounding gate · review     │
├───────────────────────────────┬─────────────────────────────────────────┤
│  AI (optional)                │  Imagery (optional)                     │
│  Claude Opus 5  - vision      │  Pillow - read, orient, tile photos     │
│  Claude Sonnet 5 - drafting   │  classical edge-energy detector         │
├───────────────────────────────┴─────────────────────────────────────────┤
│  Storage    SQLite (one file: data/derived/assets.sqlite, ~3.4 GB)      │
├─────────────────────────────────────────────────────────────────────────┤
│  Data       FHWA NBI 2023/2025 · FHWA NBE 2023/2025 · dacl10k images    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Language and runtime

| Item | Choice | Why |
|---|---|---|
| Language | **Python 3.10+** (built and tested on 3.10.0) | One language for ingest, analysis, AI calls and the web server |
| Dependencies at run time | **None required** | Runs on a bare Python install; optional extras are imported defensively and report what is missing instead of crashing |
| Packaging | `pyproject.toml`, installed with `python -m pip install -e ".[extras]"` | Standard, editable install |
| Launcher | `run.bat` (Windows batch) | One command: checks Python, installs packages, starts the app |

## Python standard library modules doing the heavy lifting

| Module | Used for |
|---|---|
| `sqlite3` | The evidence index: 632,140 structures, 5.4 million artifacts, every finding, brief, review action and sign-off |
| `csv` | Streaming the 243 MB National Bridge Inventory files row by row |
| `zipfile`, `xml.etree.ElementTree` | Reading element-level XML straight out of the FHWA ZIPs, without unpacking |
| `http.server` (`ThreadingHTTPServer`) | The web server and JSON API |
| `email.parser` | Parsing multipart photo uploads (no `cgi`, which is removed in Python 3.13) |
| `hashlib` | SHA-256 fingerprints: every stored photo and every raw file |
| `json` | API responses, NDJSON progress streaming, reports |
| `argparse` | Every pipeline step is also a command-line tool |
| `threading` | Background metrics recompute without blocking page loads |
| `math`, `statistics` | Validation significance tests (z-score, risk ratio); the baseline detector's texture thresholds |

## Optional packages

| Extra | Package | Version used | What it adds |
|---|---|---|---|
| `imagery` | **Pillow** | 12.3.0 | Opening photos, honouring camera orientation (EXIF), tiling, the baseline detector, the dacl10k benchmark |
| `llm` | **anthropic** (official Anthropic Python SDK) | 1.6.0 | Claude vision on uploaded photos; the optional LLM sentence drafter |
| `dev` | **pytest** | 9.1.1 | 474 tests |

## AI models

| Model | Role | How it is kept honest |
|---|---|---|
| **Claude Opus 5** (`claude-opus-5`) | Reads each uploaded photograph and returns defect boxes, a class, a one-line description and a confidence | Structured outputs (a JSON schema the reply must match); told never to judge safety or recommend repairs; confidence capped at 0.75 because self-reported certainty is not calibrated; every region is labelled "automated proposal, unconfirmed" |
| **Claude Sonnet 5** (`claude-sonnet-5`) | Optional: phrases brief sentences (`BRIDGE_BRIEF_DRAFTER=llm`) | Its output goes through the same deterministic citation check as the template drafter. It is never trusted to cite correctly; every citation is verified. |
| **Classical baseline** (our own code) | Edge-energy texture detector, used when no API key is set | Clearly labelled as a baseline; its measured precision and recall are published, including how poor they are |

The default path uses **no AI at all for writing**: a deterministic template drafter
produces the sentences. Models can be switched by environment variable, and nothing
is hardcoded.

## Front end

| Item | Choice |
|---|---|
| Markup | Server-rendered HTML from Python (`src/ui/webapp.py`) |
| Styling | Hand-written CSS (`src/ui/static/app.css`, about 630 lines): **glassmorphism** with `backdrop-filter`, layered translucent panels, aurora background, perspective grid floor |
| Interaction | Vanilla JavaScript (`src/ui/static/app.js`, about 770 lines), no framework |
| 3D | **Canvas 2D** with hand-written 3D projection: a rotating wireframe truss bridge with an inspection sweep; pointer-tracked 3D tilt on cards |
| Live progress | Upload streams back as **NDJSON**, one line per stage, so the progress shown is real work, not an animation |
| Icons | **Phosphor Icons** (MIT licence), vendored as SVG path data in `src/ui/icons.py` |
| Fonts | System fonts (Segoe UI Variable, SF Pro, Cascadia Code): nothing downloaded |
| Accessibility | Honours `prefers-reduced-motion`; keyboard-reachable controls; ARIA labels |

No npm, no bundler, no CDN. The page works offline.

## Data

| Dataset | Publisher | Size | Licence | Role |
|---|---|---|---|---|
| **NBI** National Bridge Inventory, 2023 and 2025 | U.S. Federal Highway Administration | ~243 MB per year, ~622k bridges | U.S. public data | Every bridge's official 0 to 9 condition ratings |
| **NBE** National Bridge Elements, 2023 and 2025, AL / AZ / IA | U.S. Federal Highway Administration | ~1.4 MB (six ZIPs) | U.S. public data | How much of each bridge part is in condition state 1 to 4 |
| **dacl10k** | Flotzinger, Rösch and Braml, WACV 2024 (arXiv:2309.00460) | 4.76 GB, 7,910 annotated images | CC BY-NC 4.0 | Benchmark only: measures the detector. Never used as evidence about a real bridge. |

CODEBRIM was the originally specified image set; its published archive is malformed
(a ZIP written past 4 GB without ZIP64), so dacl10k replaced it. See ASSUMPTIONS.md H6.

## Storage design

- **One SQLite file**, `data/derived/assets.sqlite`. Schema in `src/schema.sql`,
  changes as numbered migrations in `src/migrations/` (currently schema version 2).
- **Raw data is read-only.** Nothing ever writes to `data/raw/`.
- **Append-only audit tables** for review actions and sign-offs.
- **Deterministic IDs** for every piece of evidence (`NBI-AL012757-2023-deck`,
  `NBE-AL012757-2023-12-cs3`, `IMG-...-r2`), derived from where the data came from,
  never from row order or timestamps.

## Testing and quality

| Item | Detail |
|---|---|
| Test framework | pytest, **474 tests**, about 30 seconds |
| Test data | Small fixtures in `tests/`. 472 tests run without any dataset; 2 more check the committed samples against the real downloads when present |
| Guard tests | Every text file read passes `encoding="utf-8"` (Windows safety); no code path can mark a bridge safe or cleared; ingest never imports from generation |
| Evaluation harness | `python -m src.eval.run_all` rebuilds the whole metrics table from the real data |

## Tools used to build it

| Tool | Use |
|---|---|
| Git | Version control; `data/` and `.env` are gitignored |
| Windows 11, Git Bash, PowerShell | Development environment |
| Claude Code | AI pair programmer used throughout development |
