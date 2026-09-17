# samples/

`CLAUDE.md` requires 2–3 **real** records per source to be committed here, so that
record shape can be checked without reading a 624k-row file into context.

**This directory is empty of samples right now, and that is correct.** The datasets
are not on disk, and fabricating a "representative" record would be exactly the
invented data this project forbids — worse than most, because a fake sample would
then be read as the authority on record shape.

Once the real files are in place, run:

```bash
python -m scripts.extract_samples
```

It copies the first few genuine records out of each source into:

```
samples/nbi_2023.txt       header row + 3 real records
samples/nbe_2023_AL.xml    first 2 real structure subtrees
samples/codebrim_annotation.xml   one real annotation file
```

Those files are then committed. They are excerpts of the originals, never edits of
them, and nothing under `data/raw/` is touched to produce them.
