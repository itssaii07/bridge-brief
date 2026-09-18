"""Server-rendered pages for the web app.

Each page is a complete HTML document with its data embedded as JSON, so it
renders and reads correctly before any script runs; ``/static/app.js`` then adds
the interaction. Every figure on these pages comes from :mod:`src.ui.app_data`,
which reads the asset index. Nothing is illustrative except the wireframe bridge
on the home page, which carries no data and says so in the source.
"""

from __future__ import annotations

import html
import json

from .icons import icon

E = html.escape

NAV = (
    ("home", "/", "Overview"),
    ("inspect", "/inspect", "New inspection"),
    ("queue", "/queue", "Sign-off queue"),
    ("metrics", "/metrics", "Evaluation"),
    ("records", "/structures", "Records"),
)


def _json(data) -> str:
    """JSON safe to embed inside a <script> element."""
    return (json.dumps(data, default=str)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _n(value, dash: str = "n/a") -> str:
    return dash if value is None else f"{value:,}"


def _pct(value, digits: int = 1, dash: str = "n/a") -> str:
    return dash if value is None else f"{value * 100:.{digits}f}%"


def shell(title: str, body: str, *, page: str, pending: int = 0, data=None,
          description: str = "") -> str:
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if key == page else ""}>{E(label)}'
        + (f'<span class="nav-badge">{pending}</span>' if key == "queue" and pending else "")
        + "</a>"
        for key, href, label in NAV)
    payload = (f'<script type="application/json" id="page-data">{_json(data)}</script>'
               if data is not None else "")
    favicon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
               "%3Crect width='32' height='32' rx='8' fill='%23ffb547'/%3E%3Cpath d='M5 21h22M7 21l4-9 4 9 4-9 4 9M11 12h12' "
               "stroke='%231a1004' stroke-width='2.4' fill='none' stroke-linejoin='round'/%3E%3C/svg%3E")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)} &middot; Bridge Brief</title>
<meta name="description" content="{E(description or 'Evidence-linked bridge inspection briefs with a human sign-off queue.')}">
<link rel="icon" href="{favicon}">
<link rel="stylesheet" href="/static/app.css">
<script src="/static/app.js" defer></script>
</head>
<body data-page="{E(page)}">
<div class="backdrop" aria-hidden="true">
  <div class="aurora"><span></span><span></span><span></span></div>
  <div class="floor"></div><div class="grain"></div>
</div>
<div class="shell">
<header class="nav glass">
  <a class="brand" href="/"><span class="brand-mark">{icon('contradiction', size=17)}</span>Bridge Brief</a>
  <nav class="nav-links" aria-label="Primary">{links}</nav>
  <span class="nav-note">{icon('signoff', size=15)} Drafts only. No autonomous clearance.</span>
</header>
<main>{body}</main>
<footer class="footer">Drafts and recommendations only. Nothing here is a safety clearance,
a maintenance order, or an authorisation to act. Sources: FHWA National Bridge Inventory and
National Bridge Elements, unmodified on local disk.</footer>
</div>
<div class="tooltip" id="tooltip" role="tooltip"></div>
<div class="toast glass" id="toast" role="status" aria-live="polite"></div>
{payload}
</body></html>"""


# --------------------------------------------------------------------------
# home
# --------------------------------------------------------------------------

def home(data: dict) -> str:
    counts, queue, metrics = data["counts"], data["queue"], data["metrics"]
    detector = metrics.get("detector") or {}
    validation = metrics.get("validation") or {}

    recent = "".join(f"""
      <a class="row-link glass" href="/report/{E(b['brief_id'])}">
        <div><div class="row-title mono">{E(b['brief_id'])}</div>
          <div class="row-sub">{E(' over '.join(p for p in (b.get('facility'), b.get('feature_crossed')) if p) or b['struct_norm'])}
            &middot; {b['photos']} photo(s) &middot; {b['total_sentences']} sentence(s)</div></div>
        <div class="row-meta"><span class="chip chip-{E(b['status'])}">{E(b['status'].replace('_', ' '))}</span>
          <span class="chip">{b['blocked_unsupported']} blocked</span></div>
      </a>""" for b in data["recent"]) or """
      <div class="glass panel muted">No briefs yet. Start an inspection to draft the first one.</div>"""

    deliverables = [
        ("Image regions", f"{_n(counts['regions'])} proposed regions on {_n(counts['photos'])} "
                          "inspection photograph(s), each an addressable artifact.",
         counts["regions"] > 0),
        ("Extracted observations", f"{_n(counts['conflicts'])} places where the two federal "
                                   "records disagree" + (", plus a written description of each "
                                   "defect Claude sees in a photo." if data.get("vision") else
                                   ". Defect descriptions per photo need Claude vision."),
         True),
        ("Confidence levels", "Every finding carries a confidence and an evidence tier: "
                              "conflicting, corroborated or single source.", True),
        ("Human sign-off queue", f"{queue['awaiting review']} brief(s) awaiting a named "
                                 "reviewer, ordered by urgency.", True),
        ("Missing evidence identified", "Absent element data, sensor readings and field "
                                        "notes are named in every brief, never assumed.", True),
        ("Source-link accuracy", f"{_pct(metrics.get('source_link_resolution'), 1)} of citations "
                                 "resolve to a stored record.", True),
        ("Missed-defect rate", f"{_pct(detector.get('missed'), 1)} on "
                               f"{E(str(detector.get('corpus') or 'the benchmark'))}, reported "
                               "rather than hidden.", detector.get("missed") is not None),
        ("Originals preserved", "Every upload is stored byte-for-byte under its own hash; "
                                "the federal files are read-only.", True),
        ("No autonomous clearance", "Publication is refused until a named human signs off, "
                                    "with no override flag.", True),
    ]
    cards = "".join(f"""
      <div class="deliverable glass reveal">
        <span class="tick{'' if done else ' pending'}">{'&#10003;' if done else '&middot;'}</span>
        <div><h3>{E(title)}</h3><p>{text}</p></div>
      </div>""" for title, text, done in deliverables)

    picks = "".join(
        f'<a class="pick" href="/inspect?structure={E(s["struct_norm"])}">{E(s["struct_norm"])}</a>'
        for s in data["suggestions"])
    # Only claim the model reads the photographs when it actually can.
    reader = ("Claude reads each photograph and marks what it sees." if data.get("vision")
              else "Each photograph is scanned for defect regions: by Claude vision when an "
                   "API key is configured, by a classical detector otherwise.")

    body = f"""
<section class="hero">
  <div class="reveal">
    <p class="eyebrow">{icon('inspect', size=13)} GAI40 inspection brief generator</p>
    <h1>Upload inspection photos.<br><span class="grad">Get a brief you can trace.</span></h1>
    <p class="lede">{reader} The official federal record is cross-checked for
      contradictions. Every sentence cites its source, and nothing leaves until an inspector
      signs it.</p>
    <div class="hero-actions">
      <a class="btn btn-primary" href="/inspect">{icon('imagery', size=17)} Start an inspection</a>
      <a class="btn" href="/queue">Review queue ({queue['awaiting review']})</a>
    </div>
    <div class="picks" style="margin-top:22px">
      <span class="faint" style="font-size:12.5px;align-self:center">Bridges where the records disagree:</span>{picks}
    </div>
  </div>
  <div class="hero-stage glass tilt reveal" data-tilt="5">
    <canvas id="bridge" aria-label="Rotating wireframe of a truss bridge, illustrative only"></canvas>
    <div class="stage-hud">
      <span class="hud-pill"><b>{_n(counts['structures'])}</b> structures indexed</span>
      <span class="hud-pill"><b>{_n(counts['artifacts'])}</b> citable artifacts</span>
    </div>
  </div>
</section>

<section class="kpis four">
  <div class="kpi glass tilt reveal"><div class="kpi-value lift">{_n(counts['structures'])}</div>
    <div class="kpi-label">structures indexed</div><div class="kpi-note">every US bridge in the 2023 and 2025 inventory</div></div>
  <div class="kpi glass tilt reveal kpi-conflict"><div class="kpi-value lift">{_n(counts['conflicts'])}</div>
    <div class="kpi-label">record contradictions</div><div class="kpi-note">rating and element data disagree</div></div>
  <div class="kpi glass tilt reveal kpi-accent"><div class="kpi-value lift">{E(str(validation.get('ratio') or 'n/a'))}{'x' if validation.get('ratio') else ''}</div>
    <div class="kpi-label">predictive lift</div><div class="kpi-note">flagged components later downgraded, vs control</div></div>
  <div class="kpi glass tilt reveal kpi-ok"><div class="kpi-value lift">{_pct(metrics.get('source_link_resolution'), 0)}</div>
    <div class="kpi-label">citations resolved</div><div class="kpi-note">a fabricated ID cannot reach a brief</div></div>
</section>

<section class="section">
  <div class="section-head"><div><h2 class="reveal">From photograph to signed brief</h2>
    <p class="reveal">Four steps, each one checkable. Nothing is published until the last.</p></div></div>
  <div class="flow">
    <div class="flow-step glass tilt reveal"><span class="flow-icon">{icon('imagery', size=22)}</span>
      <h3>Upload photographs</h3><p>Drone or hand-held stills of one structure. Originals are stored byte-for-byte and never edited.</p>
      <span class="num">{_n(counts['photos'])} uploaded</span></div>
    <div class="flow-step glass tilt reveal"><span class="flow-icon">{icon('defect', size=22)}</span>
      <h3>Propose defect regions</h3><p>Claude vision boxes and describes each visible defect, with a capped confidence. A classical detector runs without a key.</p>
      <span class="num">{_n(counts['regions'])} regions</span></div>
    <div class="flow-step glass tilt reveal"><span class="flow-icon">{icon('compare', size=22)}</span>
      <h3>Cross-check the record</h3><p>The federal 0 to 9 rating is compared with element condition states, and contradictions are surfaced first.</p>
      <span class="num">{_n(counts['conflicts'])} contradictions</span></div>
    <div class="flow-step glass tilt reveal"><span class="flow-icon">{icon('signoff', size=22)}</span>
      <h3>Human sign-off</h3><p>An inspector approves, edits or rejects each finding. Every change is kept. Only then can the brief be published.</p>
      <span class="num">{queue['signed off']} signed off</span></div>
  </div>
</section>

<section class="section">
  <div class="section-head"><div><h2 class="reveal">What the problem statement asks for</h2>
    <p class="reveal">Each deliverable of GAI40, with the live figure behind it.</p></div>
    <a class="btn btn-sm reveal" href="/metrics">Full evaluation</a></div>
  <div class="deliverables">{cards}</div>
</section>

<section class="section">
  <div class="section-head"><div><h2 class="reveal">Recent briefs</h2></div></div>
  <div class="list">{recent}</div>
</section>"""
    return shell("Overview", body, page="home", pending=queue["awaiting review"])


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def inspect(data: dict) -> str:
    caps = data["capabilities"]
    vision_ok = caps["vision"]["available"]
    picks = "".join(
        f'<button type="button" class="pick" data-key="{E(s["struct_norm"])}">{E(s["struct_norm"])}</button>'
        for s in data["suggestions"])
    body = f"""
<section class="page-head">
  <p class="eyebrow reveal">{icon('imagery', size=13)} New inspection</p>
  <h1 class="reveal">Draft a brief from your photographs.</h1>
  <p class="lede reveal">Choose the structure, add photographs of it, and generate. You will get a
    draft with image regions, extracted observations and confidence levels, waiting in the
    sign-off queue.</p>
</section>

<div class="steps">
  <div>
    <section class="glass panel reveal">
      <div class="panel-title"><span class="step-no" id="step-1">1</span>
        <div><h2>Which structure?</h2><p>Every brief is anchored to a bridge in the federal inventory.</p></div></div>
      <div class="field">
        <label for="structure-search">Structure number, road or river</label>
        <input class="input" id="structure-search" type="search" autocomplete="off"
               placeholder="AL012757, US 72, Paint Rock River">
        <span class="help">Search all 632,140 structures. Bridges in Alabama, Arizona and Iowa also carry element data, so their records can be cross-checked.</span>
      </div>
      <div class="picks"><span class="faint" style="font-size:12.5px;align-self:center">Known contradictions:</span>{picks}</div>
      <div class="results" id="structure-results" aria-live="polite"></div>
      <div class="chosen" id="structure-chosen" hidden></div>
    </section>

    <section class="glass panel reveal">
      <div class="panel-title"><span class="step-no" id="step-2">2</span>
        <div><h2>Photographs of that structure</h2><p>JPEG, PNG, WebP or TIFF, up to 40 MB each and 24 per inspection.</p></div></div>
      <div class="drop" id="drop" role="button" tabindex="0" aria-label="Add photographs">
        <span class="drop-icon">{icon('imagery', size=26)}</span>
        <strong>Drop photographs here, or click to choose</strong>
        <span>They must show the structure you selected. Each is stored unmodified.</span>
      </div>
      <input type="file" id="photo-input" accept="image/*" multiple hidden>
      <div class="row" style="margin-top:10px"><span class="faint" id="photo-count"></span></div>
      <div class="thumbs" id="thumbs"></div>
    </section>
  </div>

  <div class="sticky">
    <section class="glass panel reveal">
      <div class="panel-title"><span class="step-no">3</span>
        <div><h2>Who reads the photographs</h2><p>Both are proposals for an inspector to confirm.</p></div></div>
      <div class="proposers">
        <label class="proposer{'' if vision_ok else ' unavailable'}">
          <input type="radio" name="detector" value="vision" {'checked' if vision_ok else 'disabled'}>
          <div><strong>Claude vision</strong>
            <span>Boxes, classifies and describes each visible defect. {E(caps["vision"]["reason"][:1].upper() + caps["vision"]["reason"][1:])}.</span></div>
        </label>
        <label class="proposer">
          <input type="radio" name="detector" value="baseline" {'' if vision_ok else 'checked'}>
          <div><strong>Classical baseline</strong>
            <span>Edge-energy detector. Locates texture but does not classify. Runs with no key.</span></div>
        </label>
      </div>
    </section>

    <section class="glass panel reveal">
      <div class="panel-title"><span class="step-no">4</span>
        <div><h2>Generate the draft</h2><p>Nothing is published or cleared here.</p></div></div>
      <button class="btn btn-primary" id="generate" type="button" style="width:100%" disabled>
        {icon('brief', size=17)} Generate brief</button>
      <div id="progress" hidden style="margin-top:18px">
        <div class="progress" id="progress-list"></div>
        <div id="progress-result"></div>
      </div>
    </section>
  </div>
</div>"""
    return shell("New inspection", body, page="inspect", pending=data["pending"],
                 data={"preselect": data.get("preselect")})


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def report(data: dict, *, pending: int = 0) -> str:
    brief, structure = data["brief"], data["structure"]
    where = " over ".join(p for p in (structure.get("facility"), structure.get("feature_crossed")) if p)
    ratings = " ".join(
        f'<span class="chip">{E(r["component"])} {E("N" if r["rating"] is None else str(r["rating"]))}</span>'
        for r in data["ratings"])
    body = f"""
<section class="report-head">
  <div>
    <p class="eyebrow reveal">{icon('brief', size=13)} Inspection brief &middot; {E(str(brief['year']))} record</p>
    <h1 class="reveal mono" style="font-size:clamp(28px,4vw,46px)">{E(brief['struct_norm'])}</h1>
    <div class="where reveal">{E(where or 'No facility recorded')}{
      f", built {E(str(structure['year_built']))}" if structure.get('year_built') else ''}
      &middot; <span class="mono">{E(brief['brief_id'])}</span></div>
    <div class="row reveal" style="margin-top:12px"><span class="chip" id="status-chip">{E(brief['status'])}</span>{ratings}</div>
  </div>
  <div class="report-actions reveal">
    <button class="btn" id="publish" type="button">{icon('brief', size=16)} Publish</button>
    <button class="btn" type="button" onclick="window.print()">Print</button>
  </div>
</section>

<div class="banner reveal" id="banner"></div>

<section class="kpis six">
  <div class="kpi glass tilt reveal"><div class="kpi-value lift" id="kpi-findings"></div>
    <div class="kpi-label">findings</div><div class="kpi-note" id="kpi-findings-note"></div></div>
  <div class="kpi glass tilt reveal kpi-accent"><div class="kpi-value lift" id="kpi-regions"></div>
    <div class="kpi-label">image regions</div><div class="kpi-note" id="kpi-regions-note"></div></div>
  <div class="kpi glass tilt reveal"><div class="kpi-value lift" id="kpi-confidence"></div>
    <div class="kpi-label">mean confidence</div><div class="kpi-note">across scored sentences</div></div>
  <div class="kpi glass tilt reveal kpi-ok"><div class="kpi-value lift" id="kpi-links"></div>
    <div class="kpi-label">source links</div><div class="kpi-note" id="kpi-links-note"></div></div>
  <div class="kpi glass tilt reveal"><div class="kpi-value lift" id="kpi-blocked"></div>
    <div class="kpi-label">blocked_unsupported</div><div class="kpi-note" id="kpi-blocked-note"></div></div>
  <div class="kpi glass tilt reveal kpi-conflict"><div class="kpi-value lift" id="kpi-missing"></div>
    <div class="kpi-label">evidence streams missing</div><div class="kpi-note">named in the brief, not assumed</div></div>
</section>

<div class="workspace">
  <div class="stack">
    <section class="glass viewer reveal" id="viewer" aria-label="Inspection photographs"></section>
    <section class="glass panel reveal">
      <div class="panel-title"><div><h2>The brief</h2>
        <p>Select a sentence to trace it to its sources. Approve, edit or reject each one.</p></div></div>
      <div id="brief"></div>
    </section>
  </div>
  <div class="stack sticky">
    <section class="glass evidence reveal">
      <h2 id="evidence-title">Evidence streams</h2>
      <p class="hint" id="evidence-hint"></p>
      <div id="evidence-body"></div>
    </section>
    <section class="glass panel reveal">
      <div class="panel-title"><div><h2>Sign-off</h2>
        <p>A named human decides. Decided briefs are frozen.</p></div></div>
      <div class="field"><label for="reviewer">Your name</label>
        <input class="input" id="reviewer" autocomplete="name" placeholder="Inspector name"></div>
      <div id="signoff-form">
        <div class="field"><label for="signoff-note">Note (optional)</label>
          <textarea class="input" id="signoff-note" placeholder="What you checked"></textarea></div>
        <div class="row"><button class="btn btn-primary" id="signoff-yes" type="button">{icon('signoff', size=16)} Sign off</button>
          <button class="btn btn-danger" id="signoff-no" type="button">Reject brief</button></div>
      </div>
      <h3 style="margin:20px 0 10px;font-size:14px">Audit trail</h3>
      <ul class="trail" id="trail"></ul>
    </section>
  </div>
</div>"""
    return shell(f"Brief {brief['struct_norm']}", body, page="report", pending=pending, data=data)


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------

def queue(rows: list[dict], totals: dict, *, include_decided: bool) -> str:
    cards = "".join(f"""
      <div class="kpi glass tilt reveal{' kpi-accent' if key == 'awaiting review' else ''}">
        <div class="kpi-value lift">{value}</div><div class="kpi-label">{E(key)}</div></div>"""
                    for key, value in totals.items())
    body_rows = "".join(f"""
        <tr>
          <td><a class="mono" href="/report/{E(r['brief_id'])}">{E(r['brief_id'])}</a></td>
          <td><span class="mono">{E(r['struct_norm'])}</span> <span class="faint">{E(r.get('state_abbr') or '')}</span></td>
          <td><span class="chip chip-{E(r['status'])}">{E(r['status'].replace('_', ' '))}</span></td>
          <td class="num">{'-' if r['max_severity'] is None else f"{r['max_severity']:.2f}"}</td>
          <td class="num">{r['conflicting']}</td>
          <td class="num">{r['sentences']}</td>
          <td class="num">{r['blocked_unsupported']}</td>
          <td class="num">{r['findings_reviewed']}/{r['findings_total']}</td>
          <td>{E(r.get('last_reviewer') or '-')}</td>
        </tr>""" for r in rows)
    table = f"""
      <div class="table-wrap"><table>
        <thead><tr><th>brief</th><th>structure</th><th>status</th><th class="num">severity</th>
          <th class="num">conflicting</th><th class="num">sentences</th><th class="num">blocked</th>
          <th class="num">reviewed</th><th>last reviewer</th></tr></thead>
        <tbody>{body_rows}</tbody></table></div>""" if rows else """
      <p class="muted" style="margin:0">No briefs are awaiting sign-off. <a href="/inspect">Start an inspection</a>.</p>"""
    toggle = ('<a class="btn btn-sm" href="/queue">Hide decided briefs</a>' if include_decided
              else '<a class="btn btn-sm" href="/queue?all=1">Show decided briefs</a>')
    body = f"""
<section class="page-head">
  <p class="eyebrow reveal">{icon('signoff', size=13)} Human sign-off queue</p>
  <h1 class="reveal">Awaiting human sign-off</h1>
  <p class="lede reveal">A brief carries no authority until a named inspector signs it. Ordered by
    urgency: briefs already in review first, then by the most severe finding, then by how many
    sentences the grounding gate dropped. The order is deterministic, so your place does not move.</p>
</section>
<section class="kpis">{cards}</section>
<section class="glass panel reveal" style="margin-top:20px">
  <div class="section-head" style="margin-bottom:10px"><h2 style="font-size:19px">Briefs</h2>{toggle}</div>
  {table}
</section>"""
    return shell("Sign-off queue", body, page="queue", pending=totals.get("awaiting review", 0))


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

#: The headline gauges: which metric, how to read it, and which way is good.
GAUGES = (
    ("source_link_resolution", "Source-link resolution", "citations that resolve to a record"),
    ("unsupported_content_rate", "Unsupported-content rate", "sentences blocked / generated"),
    ("missed_defect_rate", "Missed-defect rate", "annotated defects the detector missed"),
    ("correction_effort_edits_per_finding", "Correction effort", "edits per reviewed finding"),
    ("factual_fidelity", "Factual fidelity", "human-judged, sampled sentences"),
    ("source_link_accuracy_semantic", "Source-link accuracy", "human-judged: does the source support it"),
    ("contradiction_precision", "Contradiction precision", "human-judged, sampled flags"),
    ("predictive_lift_nbi_optimistic", "Predictive lift", "flagged vs control, later downgraded"),
)


def metrics(rows: list[dict], *, computed_at: str | None, pending: int = 0,
            running: bool = False) -> str:
    by_name = {r["name"]: r for r in rows}
    gauges = ""
    for name, label, what in GAUGES:
        row = by_name.get(name) or {"value": None, "status": "not computed"}
        value = row.get("value")
        shown = "n/a" if value is None else (
            f"{value * 100:.1f}%" if name not in ("correction_effort_edits_per_finding",)
            else f"{value:.2f}")
        colour = {"unsupported_content_rate": "var(--corroborate)",
                  "missed_defect_rate": "var(--conflict)"}.get(name, "var(--amber)")
        gauge_value = "" if value is None else str(min(1.0, max(0.0, float(value))))
        why = "" if value is not None else (
            f'<div class="why">{E((row.get("status") or "")[:180])}</div>')
        gauges += f"""
      <div class="gauge glass tilt reveal{' na' if value is None else ''}">
        <svg viewBox="0 0 100 100" aria-hidden="true"><circle class="track" cx="50" cy="50" r="40"/>
          <circle class="fill" cx="50" cy="50" r="40" stroke="{colour}" data-value="{gauge_value}"/></svg>
        <div><div class="value">{shown}</div><div class="label">{E(label)}</div>
          <div class="why">{E(what)}</div>{why}</div>
      </div>"""
    table = "".join(f"""
        <tr><td class="mono">{E(r['name'])}</td>
          <td class="num">{'n/a' if r['value'] is None else (f"{r['value']:,}" if isinstance(r['value'], int) else f"{r['value']:.4f}")}</td>
          <td class="muted">{E(r['ground_truth'] or '')}{
            '' if r['value'] is not None or r['status'] == 'ok' else f'<div class="faint" style="font-size:12px;margin-top:4px">{E(r["status"])}</div>'}</td></tr>"""
                    for r in rows)
    computed = sum(1 for r in rows if r["value"] is not None)
    body = f"""
<section class="page-head">
  <p class="eyebrow reveal">{icon('measure', size=13)} Evaluation</p>
  <h1 class="reveal">{computed} of {len(rows)} metrics computed.</h1>
  <p class="lede reveal">Measured against real published data, never synthetic ground truth. A metric
    that needs a human judgement shows n/a with the reason, and is never scored by the system itself.</p>
  <div class="row reveal" style="margin-top:18px">
    <span class="faint mono" style="font-size:12.5px">computed {E(computed_at or 'never')}</span>
    <button class="btn btn-sm" id="recompute" type="button"{' disabled' if running else ''}>
      {'Recomputing...' if running else 'Recompute now'}</button>
  </div>
</section>
<section class="gauges">{gauges}</section>
<section class="glass panel reveal" style="margin-top:20px">
  <h2 style="font-size:19px;margin-bottom:12px">Full table</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>metric</th><th class="num">value</th><th>ground truth</th></tr></thead>
    <tbody>{table}</tbody></table></div>
</section>"""
    return shell("Evaluation", body, page="metrics", pending=pending)
