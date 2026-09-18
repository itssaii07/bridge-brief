---
marp: true
theme: default
paginate: true
title: Bridge Brief
---

<!--
  A short, plain-language explanation of the project, as slides.
  Reads as a normal page on GitHub. To present it as a slide deck, open it in
  VS Code with the "Marp for VS Code" extension, or run:  npx @marp-team/marp-cli PROJECT_SLIDES.md
-->

# Bridge Brief

### An AI assistant that drafts bridge inspection reports, where every sentence shows its proof

Built for **GAI40: Generative AI Infrastructure Inspection Brief Generator**

---

## The problem

- The U.S. has **over 620,000 bridges**. Each one is inspected regularly.
- Inspectors collect **photos**, **official condition records** and **notes**, then
  write a report **by hand**, combining all of it.
- This is slow, and mistakes are hard to spot.
- AI could help write the report, but AI can also **make things up**. For a bridge,
  a made-up sentence could be dangerous.

**The question:** can AI help write the report *without* ever being trusted blindly?

---

## Our answer, in one line

> **The AI drafts. Every sentence must point to its proof. A human signs it off.**

- If the AI writes a sentence it cannot back up with a real record, **the sentence is
  thrown away**, and we **count** how many were thrown away.
- Nothing is published until a **named person** approves it.
- The system **never** says a bridge is safe and **never** orders repairs.

---

## What you do

1. **Pick a bridge.** Search any of 632,140 real U.S. bridges by number, road or river.
2. **Upload photos** of it.
3. **Click Generate.**

Within a minute or so you get a draft report:

- your photos, with **boxes around possible damage** (cracks, rust, broken concrete)
- what the **official records** say about this bridge
- where those records **disagree with each other**
- what evidence is **missing**

---

## The surprise we found: official records disagree

Many U.S. bridges have **two** official condition records, kept separately:

| Record | What it says |
|---|---|
| **Overall score** (NBI) | One number from 0 to 9 for each part. 7 means "good". |
| **Detailed breakdown** (NBE) | How much of each part is good, fair, poor or severe |

**Nobody checks whether they agree.** We did.

**Real example, bridge AL012757 in Alabama, 2023:**
the score says the deck is **good (7)**, but the breakdown says **56% of it is in poor
or severe condition**. Two years later, the official score was **lowered to 6**.

---

## Does spotting disagreements actually predict anything?

We tested it honestly, on real public data:

1. Found every disagreement in the **2023** records.
2. Waited for the **2025** records.
3. Checked: did those bridges get **downgraded**?

**Result:** when the overall score looked better than the breakdown, the bridge was
downgraded **2.14 times as often** as bridges with no disagreement
(12.6% vs 5.9%). That is very unlikely to be chance.

So the disagreement is an **early warning sign** that nobody was looking for.

---

## How the AI is kept honest

| Safeguard | What it means |
|---|---|
| **Proof on every sentence** | Each sentence carries tags like `NBI-AL012757-2023-deck`. Click it and you see the exact record. |
| **The gate** | Plain code (not AI) checks every tag is real. No real tag, no sentence. |
| **Confidence shown** | Every finding says how sure it is, and whether one source or two support it. |
| **Photos never changed** | The original photo is stored exactly as uploaded, and can always be opened. |
| **Honest labels** | Photos of *this* bridge are never mixed up with example photos from a research dataset. |
| **Human sign-off** | Publishing is blocked until a named reviewer approves. |

---

## Where AI is used

- **Claude (Anthropic's AI) looks at each photo** and marks what it sees: a crack here,
  rust there, with a short description. It is told **never** to judge safety or
  suggest repairs, and its confidence is **capped**, because AI is often overconfident.
- **Optionally, Claude writes the sentences**, and the same gate checks every one.
- **Without an AI key**, a simple built-in detector reads the photos instead, and
  sentences come from fixed templates. The app works either way.

---

## We measure everything, including what is weak

| What we measured | Result |
|---|---|
| Records processed | 1.2 million bridge records, 0 rejected |
| Bridges cross-checked | 10,661 with both kinds of record |
| Early-warning signal | 2.14x more downgrades when records disagree |
| Simple photo detector | Weak: finds under 1% of labelled damage. **We publish that**, rather than hide it. |
| Unsupported sentences | Counted and shown on every report |

Where a number needs a human to check it, we say **"not yet measured"** instead of
guessing.

---

## Built with

- **Python** only, no heavy frameworks. Runs on any laptop.
- **SQLite**: all 632,140 bridges in one file.
- **Claude Opus 5** for reading photos.
- **Real public data**: U.S. Federal Highway Administration bridge records, and the
  dacl10k research dataset of bridge damage photos.
- **A modern web app**: glass-style design with 3D effects. Runs offline; only the
  optional Claude photo reading needs the internet.
- **474 automated tests**.

Details: [TECH_STACK.md](TECH_STACK.md) · Flow: [PROCESS.md](PROCESS.md)

---

## Honest limits

- It uses **photos and official records** only. Sensor data and inspectors'
  handwritten notes are not publicly available at scale, so the brief **says** they
  are missing rather than pretending.
- The detailed breakdown exists only for **Alabama, Arizona and Iowa** in this build.
- The built-in photo detector is a **simple baseline**, not a trained model.
- It is a **drafting assistant**. A qualified inspector always makes the call.

---

# Summary

**Bridge Brief turns photos and official records into a draft inspection report
where every sentence can be traced to its source, flags where the official records
contradict each other, and leaves every decision to a human.**

Try it: `run.bat`, then open <http://127.0.0.1:8765>

Setup: [HOW_TO_SETUP.md](HOW_TO_SETUP.md) · Running: [HOW_TO_RUN.md](HOW_TO_RUN.md)
