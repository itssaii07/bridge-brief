"""HTML rendering for the review UI.

Plain server-rendered HTML with a small amount of inline JavaScript for the
click-a-sentence-to-highlight-its-evidence behaviour. No build step, no
framework, no CDN.

Three things the UI is required to make visible, and does, on every page that
could mislead without them:

* ``blocked_unsupported`` — displayed as a headline counter on the brief, not
  buried. It is a feature, not a debug metric.
* **Evidence tier and confidence** on every finding, so a single-source claim
  can never be read as corroborated.
* **Image provenance** — every image is tagged ``inspection upload`` or
  ``reference corpus`` in the markup itself.

There is no control anywhere in this UI that clears a structure or schedules
work. Sign-off marks a *brief* as reviewed by a named person.
"""

from __future__ import annotations

import html
import json

from .icons import icon

SCRIPT = """
document.addEventListener('click', function (event) {
  var sentence = event.target.closest('.sentence');
  if (!sentence) return;
  document.querySelectorAll('.sentence.active').forEach(function (n) {
    n.classList.remove('active');
  });
  document.querySelectorAll('.artifact.highlight').forEach(function (n) {
    n.classList.remove('highlight');
  });
  sentence.classList.add('active');
  var cited = JSON.parse(sentence.dataset.citations || '[]');
  var first = null;
  cited.forEach(function (id) {
    var card = document.getElementById('artifact-' + id);
    if (card) { card.classList.add('highlight'); if (!first) first = card; }
  });
  if (first) first.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
});
"""

E = html.escape


def page(title: str, body: str, *, subtitle: str = "", pending: int = 0) -> str:
    from .webapp import shell

    head = f'<p class="eyebrow" style="margin-top:28px">{E(subtitle)}</p>' if subtitle else ""
    return shell(title, f'<div class="legacy">{head}{body}<script>{SCRIPT}</script></div>',
                 page="records", pending=pending)


def empty_state(message: str, commands: list[str]) -> str:
    steps = "".join(f"<div><code>{E(c)}</code></div>" for c in commands)
    return (f'<div class="panel"><h2>Nothing to show</h2>'
            f'<div class="empty">{E(message)}<br><br>{steps}</div></div>')


def structure_list(rows: list[dict], totals: dict, *, pending: int = 0) -> str:
    if not rows:
        return page("Inspection Brief Review", empty_state(
            "No structures have been ingested. The database is empty, which is the "
            "expected state before the datasets are placed on disk.",
            ["python -m src.ingest.nbi --year 2023 --year 2025",
             "python -m src.ingest.nbe --year 2023 --year 2025",
             "python -m src.analysis.contradictions --year 2023",
             "python -m src.catalog"],
        ), subtitle="no data loaded")

    counters = "".join(
        f'<div class="counter{" blocked" if key == "blocked_unsupported" else ""}">'
        f'<div class="n">{value:,}</div><div class="k">{E(key.replace("_", " "))}</div></div>'
        for key, value in totals.items()
    )
    # The queue is a named deliverable, so it gets a counter here rather than
    # only a nav link: a reviewer should see work waiting without looking for it.
    queue_note = (
        f'<div class="notice"><b>{pending:,}</b> brief(s) awaiting human sign-off. '
        '<a href="/queue">Open the sign-off queue</a>. No brief can be '
        'exported before a named human signs it off.</div>' if pending else "")
    body = [f'<div class="counters">{counters}</div>', queue_note,
            '<div class="panel" style="margin-top:18px"><h2>Structures</h2><table>',
            "<tr><th>structure</th><th>state</th><th>contradictions</th>"
            "<th>single source</th><th>photos</th><th>briefs</th></tr>"]
    for row in rows:
        body.append(
            f'<tr><td><a href="/structure/{E(row["struct_norm"])}">{E(row["struct_norm"])}</a></td>'
            f'<td>{E(row.get("state_abbr") or "-")}</td>'
            f'<td>{row["contradictions"]:,}</td><td>{row["single_source"]:,}</td>'
            f'<td>{row["photos"]:,}</td><td>{row["briefs"]:,}</td></tr>')
    body.append("</table></div>")
    return page("Records", "".join(body),
                subtitle=f"{len(rows):,} structure(s) with findings", pending=pending)


def structure_view(struct: dict, findings: list[dict], briefs: list[dict],
                   images: list[dict], years: list[int]) -> str:
    title = f"Structure {struct['struct_norm']}"
    body = [f'<div class="panel"><h2>{E(title)}</h2><table>']
    for key in ("struct_raw", "state_abbr", "facility", "feature_crossed", "year_built"):
        if struct.get(key):
            body.append(f'<tr><th>{E(key.replace("_", " "))}</th><td>{E(str(struct[key]))}</td></tr>')
    body.append("</table></div>")

    body.append('<div class="panel"><h2>Findings</h2>')
    if not findings:
        body.append('<div class="empty">No findings for this structure. '
                    'Run the contradiction engine.</div>')
    else:
        body.append("<table><tr><th>year</th><th>component</th><th>kind</th><th>tier</th>"
                    "<th>severity</th><th>confidence</th></tr>")
        for row in findings:
            severity = "-" if row["severity"] is None else f"{row['severity']:.2f}"
            body.append(
                f'<tr><td>{row["year"]}</td><td>{E(row["component"] or "-")}</td>'
                f'<td>{E(row["kind"])}</td>'
                f'<td><span class="tag {E(row["evidence_tier"])}">'
                f'{E(row["evidence_tier"].replace("_", " "))}</span></td>'
                f'<td>{severity}</td><td>{row["confidence"]:.2f}</td></tr>')
        body.append("</table>")
    body.append("</div>")

    body.append('<div class="panel"><h2>Briefs</h2>')
    if briefs:
        body.append("<table><tr><th>brief</th><th>year</th><th>version</th><th>status</th>"
                    "<th>sentences</th><th>blocked_unsupported</th></tr>")
        for row in briefs:
            body.append(
                f'<tr><td><a href="/brief/{E(row["brief_id"])}">{E(row["brief_id"])}</a></td>'
                f'<td>{row["year"]}</td><td>{row["version"]}</td><td>{E(row["status"])}</td>'
                f'<td>{row["total_sentences"]}</td><td>{row["blocked_unsupported"]}</td></tr>')
        body.append("</table>")
    else:
        body.append('<div class="empty">No brief has been generated for this structure yet.</div>')

    for year in years:
        body.append(
            f'<form class="inline" method="post" action="/structure/{E(struct["struct_norm"])}/generate">'
            f'<input type="hidden" name="year" value="{year}">'
            f'<button class="primary" type="submit">Generate {year} draft brief</button></form> ')
    body.append("</div>")

    body.append('<div class="panel"><h2>Imagery</h2>')
    if not images:
        body.append('<div class="empty">No photographs have been supplied for this structure.</div>')
    else:
        for image in images:
            body.append(
                f'<div class="artifact"><span class="id">{E(image["artifact_id"])}</span> '
                f'<span class="tag {E(image["provenance"])}">'
                f'{E(image["provenance"].replace("_", " "))}</span>'
                f'<div class="src">{E(image["stored_path"])}</div></div>')
    body.append("</div>")
    return page(title, "".join(body), subtitle="structure detail")


def brief_view(brief: dict, sentences: list[dict], artifacts: list[dict],
               blocked: list[dict], states: dict, trail_rows: list[dict],
               images: list[dict]) -> str:
    title = f"{brief['brief_id']}"
    rate = (brief["blocked_unsupported"] / brief["total_sentences"]
            if brief["total_sentences"] else None)

    head = [
        '<div class="notice">This is a DRAFT assembled from published records. It is not a '
        'safety clearance and not a maintenance order. It carries no authority until a named '
        'reviewer has signed it off below.</div>',
        '<div class="counters">',
        f'<div class="counter"><div class="n">{brief["total_sentences"]}</div>'
        '<div class="k">sentences generated</div></div>',
        f'<div class="counter"><div class="n">{len(sentences)}</div>'
        '<div class="k">rendered</div></div>',
        f'<div class="counter blocked"><div class="n">{brief["blocked_unsupported"]}</div>'
        '<div class="k">blocked_unsupported</div></div>',
        f'<div class="counter"><div class="n">'
        f'{"n/a" if rate is None else f"{rate:.0%}"}</div>'
        '<div class="k">unsupported rate</div></div>',
        f'<div class="counter"><div class="n">{E(brief["status"])}</div>'
        '<div class="k">status</div></div>',
        "</div>",
    ]

    left = ['<div class="panel" style="margin-top:18px"><h2>Brief</h2>']
    if not sentences:
        left.append('<div class="empty">No sentence in this brief passed the grounding gate. '
                    'Every drafted sentence lacked a resolvable artifact citation.</div>')
    current_section = None
    for row in sentences:
        if row["section"] != current_section:
            current_section = row["section"]
            left.append(f'<div class="section-title">{E(current_section.replace("_", " "))}</div>')
        citations = row["citations"]
        tier = row["evidence_tier"] or ""
        confidence = "-" if row["confidence"] is None else f"{row['confidence']:.2f}"
        state = states.get(row["finding_id"] or "", {})
        state_tag = (f'<span class="tag">{E(state["action"])} by {E(state["reviewer"])}</span>'
                     if state else "")
        left.append(
            f'<div class="sentence" data-citations=\'{html.escape(json.dumps(citations))}\'>'
            f'<div class="meta">'
            + (f'<span class="tag {E(tier)}">{E(tier.replace("_", " "))}</span>' if tier else "")
            + f'<span class="tag">confidence {confidence}</span>{state_tag}</div>'
            f'<div>{E(row["text"])}</div>'
            f'<div class="cite">{E(" ".join(citations))}</div>'
            + _action_form(brief["brief_id"], row)
            + "</div>")
    left.append("</div>")

    if blocked:
        left.append('<div class="panel"><h2>Blocked as unsupported</h2>'
                    '<div class="empty" style="padding:0 0 10px">These sentences were dropped '
                    'before render because they carried no citation that resolves to a stored '
                    'artifact. They are kept here so the gate is auditable.</div>')
        for row in blocked:
            left.append(f'<div class="blocked-item"><b>{E(row["reason"])}</b><br>'
                        f'{E(row["text"])}</div>')
        left.append("</div>")

    right = ['<div class="panel" style="margin-top:18px"><h2>Evidence</h2>'
             '<div class="empty" style="padding:0 0 10px">Click a sentence to highlight the '
             'artifacts it cites.</div>']
    if not artifacts:
        right.append('<div class="empty">No artifacts are cited in this brief.</div>')
    for artifact in artifacts:
        right.append(
            f'<div class="artifact" id="artifact-{E(artifact["artifact_id"])}">'
            f'<span class="id">{E(artifact["artifact_id"])}</span>'
            f'<div>{E(artifact.get("summary") or "")}</div>'
            f'<div class="src">{E(artifact.get("source_path") or "")}'
            + (f' [{E(artifact["source_locator"])}]' if artifact.get("source_locator") else "")
            + "</div></div>")
    right.append("</div>")

    if images:
        right.append('<div class="panel"><h2>Imagery provenance</h2>')
        for image in images:
            right.append(
                f'<div class="artifact"><span class="id">{E(image["artifact_id"])}</span> '
                f'<span class="tag {E(image["provenance"])}">'
                f'{E(image["provenance"].replace("_", " "))}</span></div>')
        right.append("</div>")

    right.append(_signoff_panel(brief))
    right.append(_trail_panel(trail_rows))

    body = "".join(head) + '<div class="layout"><div>' + "".join(left) + \
           "</div><div>" + "".join(right) + "</div></div>"
    return page(title, body, subtitle=f"structure {brief['struct_norm']}, {brief['year']}")


def _action_form(brief_id: str, sentence: dict) -> str:
    finding = sentence["finding_id"] or ""
    return (
        f'<form method="post" action="/brief/{E(brief_id)}/action" style="margin-top:8px">'
        f'<input type="hidden" name="sentence_id" value="{E(sentence["sentence_id"])}">'
        f'<input type="hidden" name="finding_id" value="{E(finding)}">'
        '<input type="text" name="reviewer" placeholder="your name" required>'
        '<input type="text" name="text_after" placeholder="replacement text (for edit)">'
        '<button name="action" value="approve">approve</button> '
        '<button name="action" value="edit">edit</button> '
        '<button name="action" value="reject">reject</button>'
        "</form>")


def _signoff_panel(brief: dict) -> str:
    if brief["status"] in ("signed_off", "rejected"):
        return (f'<div class="panel"><h2>Sign-off</h2><div class="empty">'
                f'This version is <b>{E(brief["status"])}</b>. Generate a new version to '
                "continue reviewing.</div></div>")
    return (
        f'<div class="panel"><h2>Sign-off</h2>'
        '<div class="empty" style="padding:0 0 10px">Sign-off records that a named person '
        'reviewed this brief version. It does not clear the structure or authorise work.</div>'
        f'<form method="post" action="/brief/{E(brief["brief_id"])}/signoff">'
        '<input type="text" name="reviewer" placeholder="your name" required> '
        '<input type="text" name="note" placeholder="note (optional)"> '
        '<button class="primary" name="decision" value="signed_off">sign off</button> '
        '<button name="decision" value="rejected">reject brief</button>'
        "</form></div>")


def _trail_panel(rows: list[dict]) -> str:
    out = ['<div class="panel"><h2>Sign-off trail</h2>']
    if not rows:
        out.append('<div class="empty">No review actions recorded yet.</div>')
    else:
        out.append('<ul class="trail">')
        for row in rows:
            when = row.get("acted_at") or row.get("signed_at") or ""
            if row["type"] == "signoff":
                out.append(f'<li><b>{E(row["decision"])}</b> of version {row["version"]} by '
                           f'<b>{E(row["reviewer"])}</b>, {E(when)}'
                           + (f'<br>{E(row["note"])}' if row.get("note") else "") + "</li>")
            else:
                target = row.get("sentence_id") or row.get("finding_id") or ""
                out.append(f'<li><b>{E(row["action"])}</b> by <b>{E(row["reviewer"])}</b> '
                           f'on {E(target)}, {E(when)}</li>')
        out.append("</ul>")
    out.append("</div>")
    return "".join(out)


def signoff_queue_view(rows: list[dict], totals: dict, *, include_decided: bool) -> str:
    """The human sign-off queue — the problem statement's named deliverable.

    Ordered by review urgency rather than recency, so the most severe unreviewed
    brief is first rather than last. The ordering rule is stated on the page,
    because a queue whose order a reviewer cannot predict is one they will not
    trust.
    """
    if not rows:
        message = ("No briefs are awaiting sign-off."
                   if not include_decided else "No briefs exist yet.")
        return page("Sign-off queue", empty_state(
            message,
            ["python -m src.analysis.contradictions --year 2023",
             "python -m src.generate.brief --structure <flagged structure> --year 2023"],
        ), subtitle="sign-off queue")

    counters = "".join(
        f'<div class="counter{" blocked" if key == "awaiting review" else ""}">'
        f'<div class="n">{value:,}</div><div class="k">{E(key)}</div></div>'
        for key, value in totals.items())

    toggle = ("/queue" if include_decided else "/queue?all=1")
    toggle_label = ("hide decided briefs" if include_decided
                    else "show briefs already decided")

    body = [f'<div class="counters">{counters}</div>',
            '<div class="notice">A brief carries no authority until a named human '
            'signs it off. Nothing here is a safety clearance or a maintenance '
            'order, and no brief can be exported before sign-off.</div>',
            '<div class="panel"><h2>Awaiting human sign-off</h2>',
            '<div class="empty" style="padding:0 0 12px">Ordered by urgency: briefs '
            'already in review first, then by highest finding severity, then by how '
            'many sentences the grounding gate had to drop. The order is '
            'deterministic, so your place in the queue will not move between page '
            f'loads. <a href="{toggle}">{E(toggle_label)}</a></div>',
            "<table>",
            "<tr><th>brief</th><th>structure</th><th>status</th>"
            "<th>severity</th><th>conflicting</th><th>sentences</th>"
            "<th>blocked</th><th>findings reviewed</th><th>last reviewer</th></tr>"]
    for row in rows:
        severity = row.get("max_severity")
        reviewed = f'{row["findings_reviewed"]:,}/{row["findings_total"]:,}'
        blocked = row["blocked_unsupported"]
        # Built outside the f-string: 3.10 f-strings cannot contain a backslash.
        blocked_style = ' style="color:var(--warn)"' if blocked else ""
        severity_text = "-" if severity is None else f"{severity:.2f}"
        body.append(
            f'<tr><td><a href="/brief/{E(row["brief_id"])}">{E(row["brief_id"])}</a></td>'
            f'<td><a href="/structure/{E(row["struct_norm"])}">{E(row["struct_norm"])}</a>'
            f' <span class="tag">{E(row.get("state_abbr") or "-")}</span></td>'
            f'<td><span class="tag {E(row["status"])}">{E(row["status"].replace("_", " "))}'
            '</span></td>'
            f'<td>{severity_text}</td>'
            f'<td>{row["conflicting"]:,}</td>'
            f'<td>{row["sentences"]:,}</td>'
            f'<td{blocked_style}>{blocked:,}</td>'
            f'<td>{reviewed}</td>'
            f'<td>{E(row.get("last_reviewer") or "-")}</td></tr>')
    body.append("</table></div>")
    return page("Sign-off queue", "".join(body),
                subtitle=f"{len(rows):,} brief(s) listed")
