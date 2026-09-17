# samples/

`CLAUDE.md` requires 2–3 **real** records per source to be committed here, so that
record shape can be checked without reading a 624k-row file into context.

Everything in this directory is a verbatim excerpt of a published source, produced by:

```bash
python -m scripts.extract_samples --year 2023
```

| File | What it is |
|---|---|
| `nbi_2023.txt` | the real header row plus the first 3 records of the NBI 2023 delimited file |
| `nbe_2023_AL.xml` | every element record for the first 2 structures in Alabama's 2023 extract |
| `nbe_2023_AZ.xml` | the same for Arizona |
| `nbe_2023_IA.xml` | the same for Iowa |

`codebrim_annotation.xml` is absent because the CODEBRIM archive is encrypted and could
not be opened — see ASSUMPTIONS.md H6.

Nothing here is edited, summarised or reconstructed, and nothing under `data/raw/` was
touched to produce it. `tests/test_invariants.py` enforces that: the NBI sample must match
the head of the real file line for line, and every NBE record must be present in the real
archive field for field. When the sources are not on the machine those checks skip rather
than pass silently.

Two things the samples are the authority on, both of which differ from what was assumed
before the data arrived:

* The NBE files are **flat** — a `<FHWAELEMENT>` root of repeated `<FHWAED>` records, each
  carrying its own `STATE`, `STRUCNUM`, `EN`, `TOTALQTY` and `CS1..CS4`. There is no
  structure-level nesting and **no units field** (ASSUMPTIONS.md D6).
* `STRUCNUM` is unique only **within a state** — Alabama and Arizona both publish low
  numbers like `000042`. The join key is therefore state-qualified (ASSUMPTIONS.md B5).
