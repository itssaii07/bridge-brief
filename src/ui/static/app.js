/* Bridge Brief - client behaviour.

   No framework and no build step, to match the rest of the project. Every page
   declares itself with <body data-page="...">, embeds its data as JSON in
   #page-data, and this file wires only what that page needs.

   Rendering rule: anything that came from the server or the user is inserted
   with textContent or through esc(), never as raw HTML. */

(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ------------------------------------------------------------------ utils

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function fmt(n, digits) {
    if (n == null || isNaN(n)) return "n/a";
    return digits == null ? Number(n).toLocaleString() : Number(n).toFixed(digits);
  }
  function pct(n, digits) { return n == null ? "n/a" : (n * 100).toFixed(digits == null ? 1 : digits) + "%"; }
  function pageData() {
    var node = document.getElementById("page-data");
    try { return node ? JSON.parse(node.textContent) : {}; } catch (e) { return {}; }
  }
  var toastTimer;
  function toast(message, kind) {
    var node = $("#toast");
    if (!node) return;
    node.className = "toast glass on" + (kind ? " alert-" + kind : "");
    node.textContent = message;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.className = "toast glass"; }, 5200);
  }
  function api(url, options) {
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error(body.error || ("Request failed (" + response.status + ")"));
        return body;
      });
    });
  }
  var ICON = {
    check: '<svg width="14" height="14" viewBox="0 0 256 256" fill="currentColor"><path d="M229.66,77.66l-128,128a8,8,0,0,1-11.32,0l-56-56a8,8,0,0,1,11.32-11.32L96,188.69,218.34,66.34a8,8,0,0,1,11.32,11.32Z"/></svg>',
    minus: '<svg width="14" height="14" viewBox="0 0 256 256" fill="currentColor"><path d="M224,128a8,8,0,0,1-8,8H40a8,8,0,0,1,0-16H216A8,8,0,0,1,224,128Z"/></svg>',
    cross: '<svg width="12" height="12" viewBox="0 0 256 256" fill="currentColor"><path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/></svg>'
  };

  // ------------------------------------------------------------------ tilt

  function bindTilt(root) {
    if (reduceMotion || window.matchMedia("(pointer: coarse)").matches) return;
    $$(".tilt", root).forEach(function (card) {
      if (card.dataset.tiltBound) return;
      card.dataset.tiltBound = "1";
      var max = parseFloat(card.dataset.tilt || "8");
      card.addEventListener("pointermove", function (event) {
        var r = card.getBoundingClientRect();
        var px = (event.clientX - r.left) / r.width, py = (event.clientY - r.top) / r.height;
        card.classList.add("tilting");
        card.style.setProperty("--ry", ((px - 0.5) * max * 2).toFixed(2) + "deg");
        card.style.setProperty("--rx", ((0.5 - py) * max * 2).toFixed(2) + "deg");
        card.style.setProperty("--gx", (px * 100).toFixed(1) + "%");
        card.style.setProperty("--gy", (py * 100).toFixed(1) + "%");
      });
      card.addEventListener("pointerleave", function () {
        card.classList.remove("tilting");
        card.style.setProperty("--rx", "0deg");
        card.style.setProperty("--ry", "0deg");
      });
    });
  }

  // ------------------------------------------------------------------ reveal

  function bindReveal() {
    var nodes = $$(".reveal");
    if (reduceMotion || !("IntersectionObserver" in window)) return;   // content already visible
    var root = document.documentElement;
    // Anything already on screen is shown immediately rather than faded in, so
    // the first paint is never an empty page.
    var vh = window.innerHeight;
    nodes.forEach(function (n) { if (n.getBoundingClientRect().top < vh) n.classList.add("in"); });
    root.classList.add("js-anim");
    var alive = false;
    var io = new IntersectionObserver(function (entries) {
      alive = true;
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add("in"); io.unobserve(entry.target); }
      });
    }, { threshold: 0, rootMargin: "0px 0px -6% 0px" });
    nodes.forEach(function (n, i) { n.style.transitionDelay = Math.min(i % 6, 5) * 60 + "ms"; io.observe(n); });
    // An animation that fails must never cost the reader the content.
    // An observer that works reports every target once, straight away. If none
    // has arrived (a throttled background tab, say), stop hiding anything.
    setTimeout(function () { if (!alive) root.classList.remove("js-anim"); }, 1500);
  }

  // ------------------------------------------------------------------ 3D bridge
  //
  // A Warren-truss span drawn in real 3D: every node is a point in space,
  // rotated and perspective-projected each frame. A cyan inspection sweep runs
  // along the span, and the nodes it passes glow as it reads them.

  function bridge3d(canvas) {
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    var nodes = [], edges = [];
    function node(x, y, z, kind) { nodes.push({ x: x, y: y, z: z, kind: kind || "n" }); return nodes.length - 1; }
    function edge(a, b, kind) { edges.push([a, b, kind || "member"]); }

    var panels = 8, span = 12, height = 1.8, half = 1.15, pitch = span / panels;
    var bottom = [[], []], top = [[], []];
    [-half, half].forEach(function (z, side) {
      for (var i = 0; i <= panels; i++) bottom[side].push(node(-span / 2 + i * pitch, 0, z, "chord"));
      for (var j = 0; j < panels; j++) top[side].push(node(-span / 2 + (j + 0.5) * pitch, height, z, "chord"));
      for (var k = 0; k < panels; k++) {
        edge(bottom[side][k], bottom[side][k + 1], "chord");
        edge(bottom[side][k], top[side][k], "diag");
        edge(top[side][k], bottom[side][k + 1], "diag");
        if (k < panels - 1) edge(top[side][k], top[side][k + 1], "chord");
      }
    });
    for (var i = 0; i <= panels; i++) edge(bottom[0][i], bottom[1][i], "floor");
    for (var j = 0; j < panels; j++) edge(top[0][j], top[1][j], "brace");
    for (var d = 0; d < panels - 1; d++) edge(top[0][d], top[1][d + 1], "brace");
    // Deck stringers, abutments and a pier line down to the water.
    var deck = [];
    [-0.55, 0, 0.55].forEach(function (z) {
      var a = node(-span / 2, -0.05, z, "deck"), b = node(span / 2, -0.05, z, "deck");
      edge(a, b, "deck"); deck.push(a, b);
    });
    [-span / 2, span / 2].forEach(function (x) {
      [-half, half].forEach(function (z) {
        var a = node(x, 0, z, "pier"), b = node(x, -2.4, z * 1.2, "pier");
        edge(a, b, "pier");
      });
    });
    // Nodes the sweep flags, standing in for proposed regions. Unlabelled on
    // purpose: this is an illustration, and it carries no data.
    var flagged = [bottom[0][3], top[1][5], bottom[1][6]];

    var dpr = Math.min(window.devicePixelRatio || 1, 2), w = 0, h = 0;
    function resize() {
      var r = canvas.getBoundingClientRect();
      w = r.width; h = r.height;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    window.addEventListener("resize", resize);

    var mx = 0, my = 0, tx = 0, ty = 0;
    canvas.parentElement.addEventListener("pointermove", function (e) {
      var r = canvas.getBoundingClientRect();
      tx = ((e.clientX - r.left) / r.width - 0.5) * 2;
      ty = ((e.clientY - r.top) / r.height - 0.5) * 2;
    });
    canvas.parentElement.addEventListener("pointerleave", function () { tx = 0; ty = 0; });

    function project(p, yaw, pitchAngle) {
      var cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitchAngle), sp = Math.sin(pitchAngle);
      var x = p.x * cy - p.z * sy, z = p.x * sy + p.z * cy;
      var y = p.y * cp - z * sp; z = p.y * sp + z * cp;
      var dist = 16, f = Math.min(w * 0.95, h * 1.25) * 1.3;
      var s = f / (z + dist);
      return { x: w / 2 + x * s, y: h * 0.47 - y * s, z: z, s: s };
    }

    var start = performance.now(), visible = true, raf = 0;
    function frame(now) {
      var t = (now - start) / 1000;
      mx += (tx - mx) * 0.06; my += (ty - my) * 0.06;
      var yaw = (reduceMotion ? 0.55 : 0.55 + Math.sin(t * 0.18) * 0.5) + mx * 0.35;
      var pitchAngle = -0.32 - my * 0.18;
      var sweep = reduceMotion ? 1.5 : ((t * 0.16) % 1.3) * span - span * 0.65;
      ctx.clearRect(0, 0, w, h);
      var pts = nodes.map(function (p) { return project(p, yaw, pitchAngle); });

      // Water line reflection glow under the span.
      var g = ctx.createRadialGradient(w / 2, h * 0.8, 10, w / 2, h * 0.8, w * 0.55);
      g.addColorStop(0, "rgba(98,230,255,0.10)"); g.addColorStop(1, "rgba(98,230,255,0)");
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);

      edges.slice().sort(function (a, b) {
        return (pts[b[0]].z + pts[b[1]].z) - (pts[a[0]].z + pts[a[1]].z);
      }).forEach(function (e) {
        var a = pts[e[0]], b = pts[e[1]];
        var depth = Math.max(0.18, Math.min(1, 1.15 - ((a.z + b.z) / 2 + 6) / 16));
        var alpha = { chord: 0.95, diag: 0.7, floor: 0.4, brace: 0.35, deck: 0.5, pier: 0.45 }[e[2]] * depth;
        var mid = (nodes[e[0]].x + nodes[e[1]].x) / 2;
        var near = Math.max(0, 1 - Math.abs(mid - sweep) / 1.4);
        ctx.strokeStyle = near > 0.05
          ? "rgba(" + Math.round(98 + 157 * near) + "," + Math.round(230 - 49 * near) + "," + Math.round(255 - 184 * near) + "," + Math.min(1, alpha + near * 0.5) + ")"
          : "rgba(98,230,255," + alpha + ")";
        ctx.lineWidth = (e[2] === "chord" ? 2.1 : 1.3) * Math.max(0.6, a.s / 45);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      });

      nodes.forEach(function (n, i) {
        if (n.kind !== "chord") return;
        var p = pts[i], r = Math.max(1.2, p.s / 26);
        ctx.fillStyle = "rgba(220,248,255,0.85)";
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
      });

      // The sweep plane itself.
      var s1 = project({ x: sweep, y: height + 0.4, z: -half - 0.3 }, yaw, pitchAngle);
      var s2 = project({ x: sweep, y: -0.3, z: half + 0.3 }, yaw, pitchAngle);
      var s3 = project({ x: sweep, y: height + 0.4, z: half + 0.3 }, yaw, pitchAngle);
      var s4 = project({ x: sweep, y: -0.3, z: -half - 0.3 }, yaw, pitchAngle);
      ctx.fillStyle = "rgba(98,230,255,0.07)";
      ctx.strokeStyle = "rgba(98,230,255,0.35)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(s1.x, s1.y); ctx.lineTo(s3.x, s3.y); ctx.lineTo(s2.x, s2.y);
      ctx.lineTo(s4.x, s4.y); ctx.closePath(); ctx.fill(); ctx.stroke();

      flagged.forEach(function (idx, k) {
        var p = pts[idx], passed = nodes[idx].x < sweep;
        var pulse = reduceMotion ? 0.6 : 0.5 + 0.5 * Math.sin(t * 3 + k * 1.7);
        var r = Math.max(3, p.s / 11) * (passed ? 1 + pulse * 0.6 : 0.7);
        ctx.save();
        ctx.shadowColor = "rgba(255,181,71,0.9)"; ctx.shadowBlur = passed ? 22 : 6;
        ctx.fillStyle = passed ? "rgba(255,181,71,0.95)" : "rgba(255,181,71,0.35)";
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
        if (passed) {
          ctx.strokeStyle = "rgba(255,181,71," + (0.5 - pulse * 0.4) + ")";
          ctx.lineWidth = 1.2;
          ctx.beginPath(); ctx.arc(p.x, p.y, r + 6 + pulse * 8, 0, Math.PI * 2); ctx.stroke();
        }
      });

      if (!reduceMotion && visible) raf = requestAnimationFrame(frame);
    }
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        visible = entries[0].isIntersecting;
        if (visible && !reduceMotion) { cancelAnimationFrame(raf); raf = requestAnimationFrame(frame); }
      }).observe(canvas);
    }
    raf = requestAnimationFrame(frame);
  }

  // ------------------------------------------------------------------ inspect page

  function inspectPage(data) {
    var chosen = null, files = [];
    var search = $("#structure-search"), results = $("#structure-results");
    var chosenBox = $("#structure-chosen"), drop = $("#drop"), picker = $("#photo-input");
    var thumbs = $("#thumbs"), go = $("#generate"), progress = $("#progress");
    var STAGES = [
      ["anchor", "Anchor to the federal record"],
      ["preserve", "Preserve the originals"],
      ["detect", "Propose defect regions"],
      ["records", "Cross-check condition records"],
      ["draft", "Draft through the grounding gate"],
      ["complete", "Ready for human review"]
    ];

    function refreshButton() {
      go.disabled = !(chosen && files.length);
      $("#step-1").classList.toggle("done", !!chosen);
      $("#step-2").classList.toggle("done", files.length > 0);
    }

    function renderResults(rows) {
      if (!rows.length) {
        results.innerHTML = '<p class="faint" style="margin:6px 2px">No structure matches. Try a structure number such as AL012757, or a road or river name.</p>';
        return;
      }
      results.innerHTML = rows.map(function (r) {
        var where = [r.facility, r.feature_crossed].filter(Boolean).join(" over ");
        var badges = "";
        if (r.conflicts) badges += '<span class="chip chip-conflicting">' + r.conflicts + " conflict" + (r.conflicts > 1 ? "s" : "") + "</span>";
        badges += r.has_elements ? '<span class="chip">element data</span>' : '<span class="chip">rating only</span>';
        return '<button type="button" class="result" data-key="' + esc(r.struct_norm) + '">' +
          '<div><div class="key">' + esc(r.struct_norm) + ' <span class="faint">' + esc(r.state_abbr || "") + "</span></div>" +
          '<div class="where">' + esc(where || "no facility recorded") + (r.year_built ? " &middot; built " + esc(r.year_built) : "") + "</div></div>" +
          '<div class="row">' + badges + "</div></button>";
      }).join("");
    }

    function choose(key) {
      api("/api/structure/" + encodeURIComponent(key)).then(function (detail) {
        chosen = detail.structure.struct_norm;
        $$(".result", results).forEach(function (b) { b.classList.toggle("selected", b.dataset.key === chosen); });
        var s = detail.structure;
        var latest = detail.latest_year;
        var ratings = detail.ratings.filter(function (r) { return r.year === latest; });
        var byComponent = {};
        ratings.forEach(function (r) { byComponent[r.component] = r.rating; });
        chosenBox.hidden = false;
        chosenBox.innerHTML =
          '<div class="row"><strong class="mono" style="font-size:17px">' + esc(chosen) + '</strong>' +
          '<span class="chip">' + esc(s.state_abbr || "") + "</span>" +
          (detail.conflicts ? '<span class="chip chip-conflicting">' + detail.conflicts + " recorded conflict(s)</span>" : "") +
          '<span class="spacer"></span><span class="faint mono" style="font-size:12px">latest record ' + esc(latest) + "</span></div>" +
          '<div class="muted" style="font-size:13.5px">' + esc([s.facility, s.feature_crossed].filter(Boolean).join(" over ") || "No facility recorded") +
          (s.year_built ? ", built " + esc(s.year_built) : "") + "</div>" +
          '<div class="ratings">' + ["deck", "superstructure", "substructure", "culvert"].map(function (c) {
            var v = byComponent[c];
            return '<div class="rating"><span class="num">' + (v == null ? "N" : esc(v)) + "</span><small>" + esc(c) + "</small></div>";
          }).join("") + "</div>" +
          '<div class="faint" style="font-size:12px">National Bridge Inventory condition ratings, 0 to 9. N means not applicable to this structure.' +
          (detail.has_elements ? " Element condition-state data is on record, so the two sources can be cross-checked." :
            " No element condition-state data is published for this structure, so the rating cannot be cross-checked.") + "</div>";
        refreshButton();
      }).catch(function (e) { toast(e.message, "warn"); });
    }

    var timer;
    search.addEventListener("input", function () {
      clearTimeout(timer);
      var q = search.value.trim();
      if (q.length < 2) { results.innerHTML = ""; return; }
      timer = setTimeout(function () {
        results.innerHTML = '<p class="faint" style="margin:6px 2px">Searching 632,140 structures...</p>';
        api("/api/structures?q=" + encodeURIComponent(q)).then(function (body) { renderResults(body.results); })
          .catch(function (e) { results.innerHTML = '<p class="alert">' + esc(e.message) + "</p>"; });
      }, 320);
    });
    results.addEventListener("click", function (e) {
      var b = e.target.closest(".result"); if (b) choose(b.dataset.key);
    });
    $$(".pick").forEach(function (b) {
      b.addEventListener("click", function () { search.value = b.dataset.key; choose(b.dataset.key); });
    });
    if (data.preselect) { search.value = data.preselect; choose(data.preselect); }

    function addFiles(list) {
      Array.prototype.forEach.call(list, function (file) {
        if (!/^image\//.test(file.type) && !/\.(jpe?g|png|webp|tiff?|bmp)$/i.test(file.name)) {
          toast(file.name + " is not an image and was skipped.", "warn"); return;
        }
        if (file.size > 40 * 1024 * 1024) { toast(file.name + " is over 40 MB and was skipped.", "warn"); return; }
        if (files.some(function (f) { return f.name === file.name && f.size === file.size; })) return;
        files.push(file);
      });
      renderThumbs(); refreshButton();
    }
    function renderThumbs() {
      thumbs.innerHTML = "";
      files.forEach(function (file, i) {
        var cell = document.createElement("div"); cell.className = "thumb";
        var img = document.createElement("img"); img.alt = file.name; img.src = URL.createObjectURL(file);
        img.onload = function () { URL.revokeObjectURL(img.src); };
        var rm = document.createElement("button"); rm.type = "button"; rm.innerHTML = ICON.cross;
        rm.setAttribute("aria-label", "Remove " + file.name);
        rm.addEventListener("click", function () { files.splice(i, 1); renderThumbs(); refreshButton(); });
        var label = document.createElement("small"); label.textContent = file.name;
        cell.appendChild(img); cell.appendChild(rm); cell.appendChild(label); thumbs.appendChild(cell);
      });
      $("#photo-count").textContent = files.length ? files.length + " photograph(s) ready" : "";
    }
    drop.addEventListener("click", function () { picker.click(); });
    drop.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); } });
    picker.addEventListener("change", function () { addFiles(picker.files); picker.value = ""; });
    ["dragenter", "dragover"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("over"); }); });
    ["dragleave", "drop"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("over"); }); });
    drop.addEventListener("drop", function (e) { addFiles(e.dataTransfer.files); });

    function stageNode(key) { return $('.pstage[data-stage="' + key + '"]', progress); }
    function resetProgress() {
      progress.hidden = false;
      $("#progress-list").innerHTML = STAGES.map(function (s) {
        return '<div class="pstage" data-stage="' + s[0] + '"><span class="pdot"></span><div><strong>' + esc(s[1]) + '</strong><div class="msgs"></div></div></div>';
      }).join("");
      $("#progress-result").innerHTML = "";
    }
    function applyEvent(ev) {
      if (ev.stage === "error") {
        $$(".pstage.running", progress).forEach(function (n) { n.className = "pstage error"; n.querySelector(".pdot").innerHTML = "!"; });
        $("#progress-result").innerHTML = '<div class="alert">' + esc(ev.message) + "</div>";
        return;
      }
      var node = stageNode(ev.stage); if (!node) return;
      var msgs = node.querySelector(".msgs");
      if (ev.message) {
        var line = document.createElement("div");
        if (ev.status === "warning") line.className = "warn";
        line.textContent = ev.message; msgs.appendChild(line);
      }
      if (ev.status === "running") node.className = "pstage running";
      else if (ev.status === "warning") { if (!node.classList.contains("done")) node.className = "pstage running"; }
      else if (ev.status === "done") { node.className = "pstage done"; node.querySelector(".pdot").innerHTML = ICON.check; }
      if (ev.stage === "complete") {
        var id = ev.data.brief_id;
        $("#progress-result").innerHTML = '<div class="alert alert-ok">Brief ' + esc(id) + ' drafted in ' + esc(ev.data.seconds) +
          's. Opening the report...</div><div class="row" style="margin-top:12px"><a class="btn btn-primary" href="/report/' + encodeURIComponent(id) + '">Open the report</a></div>';
        setTimeout(function () { window.location.href = "/report/" + encodeURIComponent(id); }, 1400);
      }
    }

    go.addEventListener("click", function () {
      if (!chosen || !files.length) return;
      var form = new FormData();
      form.append("structure", chosen);
      form.append("detector", (($('input[name="detector"]:checked') || {}).value) || "auto");
      files.forEach(function (f) { form.append("photos", f, f.name); });
      go.disabled = true; resetProgress();
      progress.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
      fetch("/api/inspect", { method: "POST", body: form }).then(function (response) {
        if (!response.body || !response.body.getReader) {
          return response.text().then(function (text) { text.split("\n").filter(Boolean).forEach(function (l) { applyEvent(JSON.parse(l)); }); });
        }
        var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = "";
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) { if (buffer.trim()) applyEvent(JSON.parse(buffer)); return; }
            buffer += decoder.decode(chunk.value, { stream: true });
            var lines = buffer.split("\n"); buffer = lines.pop();
            lines.filter(Boolean).forEach(function (l) { applyEvent(JSON.parse(l)); });
            return pump();
          });
        }
        return pump();
      }).catch(function (e) { applyEvent({ stage: "error", message: e.message }); })
        .then(function () { go.disabled = !(chosen && files.length); });
    });
    refreshButton();
  }

  // ------------------------------------------------------------------ report page

  function reportPage(initial) {
    var report = initial, selected = null, imageIndex = 0, showRegions = true;
    var reviewerInput = $("#reviewer");
    try { reviewerInput.value = localStorage.getItem("bb-reviewer") || ""; } catch (e) { /* storage blocked */ }
    reviewerInput.addEventListener("change", function () {
      try { localStorage.setItem("bb-reviewer", reviewerInput.value.trim()); } catch (e) { /* ignore */ }
    });
    var tip = $("#tooltip");

    function reviewer() {
      var name = reviewerInput.value.trim();
      if (!name) { reviewerInput.focus(); toast("Enter your name first. Review actions are never anonymous.", "warn"); }
      return name;
    }
    function frozen() { return report.brief.status === "signed_off" || report.brief.status === "rejected"; }

    // ---- photo viewer
    function renderViewer() {
      var box = $("#viewer");
      var images = report.images;
      if (!images.length) {
        box.innerHTML = '<div class="empty-viewer"><div class="drop-icon" style="margin:auto">' +
          '<svg width="26" height="26" viewBox="0 0 256 256" fill="currentColor"><path d="M208,56H180.28L166.65,35.56A8,8,0,0,0,160,32H96a8,8,0,0,0-6.65,3.56L75.71,56H48A24,24,0,0,0,24,80V192a24,24,0,0,0,24,24H208a24,24,0,0,0,24-24V80A24,24,0,0,0,208,56Zm8,136a8,8,0,0,1-8,8H48a8,8,0,0,1-8-8V80a8,8,0,0,1,8-8H80a8,8,0,0,0,6.66-3.56L100.28,48h55.43l13.63,20.44A8,8,0,0,0,176,72h32a8,8,0,0,1,8,8ZM128,88a44,44,0,1,0,44,44A44.05,44.05,0,0,0,128,88Zm0,72a28,28,0,1,1,28-28A28,28,0,0,1,128,160Z"/></svg></div>' +
          "<strong>No inspection photographs for this structure yet</strong><span>This brief rests on the federal records alone. Upload photographs to add image regions.</span>" +
          '<a class="btn btn-sm" href="/inspect?structure=' + encodeURIComponent(report.brief.struct_norm) + '">Upload photographs</a></div>';
        return;
      }
      if (imageIndex >= images.length) imageIndex = 0;
      var img = images[imageIndex], W = img.width || 1000, H = img.height || 750;
      var regions = showRegions ? img.regions : [];
      box.innerHTML =
        '<div class="viewer-stage"><img src="/original/' + encodeURIComponent(img.artifact_id) + '" alt="Inspection photograph ' + esc(img.filename) + '" width="' + W + '" height="' + H + '">' +
        '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" aria-label="Proposed defect regions">' +
        regions.map(function (r) {
          var label = "r" + r.region_index + (r.defect_class && r.defect_class !== "defect" ? " " + r.defect_class.replace("_", " ") : "");
          var fs = Math.max(14, Math.round(W / 70)), tw = label.length * fs * 0.62 + 12;
          return '<g class="region" data-aid="' + esc(r.artifact_id) + '"><rect x="' + r.x + '" y="' + r.y + '" width="' + r.w + '" height="' + r.h + '" rx="4"/>' +
            '<rect class="tagbg" x="' + r.x + '" y="' + Math.max(0, r.y - fs - 8) + '" width="' + tw + '" height="' + (fs + 8) + '" rx="3"/>' +
            '<text x="' + (r.x + 6) + '" y="' + Math.max(fs, r.y - 7) + '" style="font-size:' + fs + 'px">' + esc(label) + "</text></g>";
        }).join("") + "</svg></div>" +
        '<div class="viewer-bar"><span class="row"><span class="chip chip-upload">inspection upload</span>' +
        '<span class="mono">' + esc(img.artifact_id) + "</span></span>" +
        '<span class="row"><span class="mono faint" title="SHA-256 of the stored original">sha256 ' + esc((img.sha256 || "").slice(0, 12)) + "</span>" +
        '<a class="btn btn-sm" href="/original/' + encodeURIComponent(img.artifact_id) + '" target="_blank" rel="noopener">Open original</a>' +
        '<button class="btn btn-sm" id="toggle-regions" type="button">' + (showRegions ? "Hide" : "Show") + " regions (" + img.regions.length + ")</button></span></div>" +
        (images.length > 1 ? '<div class="strip">' + images.map(function (im, i) {
          return '<button type="button" class="' + (i === imageIndex ? "on" : "") + '" data-i="' + i + '" aria-label="Photograph ' + (i + 1) + '"><img src="/original/' + encodeURIComponent(im.artifact_id) + '" alt=""></button>';
        }).join("") + "</div>" : "");
      $("#toggle-regions").addEventListener("click", function () { showRegions = !showRegions; renderViewer(); applySelection(); });
      $$(".strip button", box).forEach(function (b) { b.addEventListener("click", function () { imageIndex = +b.dataset.i; renderViewer(); applySelection(); }); });
      $$(".region", box).forEach(function (g) {
        var r = img.regions.filter(function (x) { return x.artifact_id === g.dataset.aid; })[0];
        g.addEventListener("pointermove", function (e) {
          tip.innerHTML = "<b>" + esc(r.artifact_id) + "</b><br>" + esc((r.defect_class || "defect").replace("_", " ")) +
            ", confidence " + fmt(r.confidence, 2) + "<br>" + esc(r.w + " x " + r.h + " px at (" + r.x + ", " + r.y + ")") +
            (r.description ? "<br><br>" + esc(r.description) : "") +
            '<br><span class="faint">' + esc(r.detector_name || "") + ". Automated proposal, unconfirmed.</span>";
          tip.style.left = Math.min(window.innerWidth - 340, e.clientX + 16) + "px";
          tip.style.top = (e.clientY + 16) + "px"; tip.classList.add("on");
        });
        g.addEventListener("pointerleave", function () { tip.classList.remove("on"); });
        g.addEventListener("click", function () {
          var citing = report.sentences.filter(function (s) { return s.citations.indexOf(r.artifact_id) >= 0; })[0];
          select(citing ? citing.sentence_id : null, r.artifact_id);
        });
      });
    }

    // ---- sentences
    var SECTION_NAMES = { summary: "Summary", contradictions: "Where the records disagree", condition: "Condition record",
      imagery: "Image regions", evidence_gaps: "Missing evidence" };
    function renderSentences() {
      var groups = {}, order = [];
      report.sentences.forEach(function (s) {
        if (!groups[s.section]) { groups[s.section] = []; order.push(s.section); }
        groups[s.section].push(s);
      });
      $("#brief").innerHTML = order.map(function (section) {
        return '<div class="brief-section"><h3>' + esc(SECTION_NAMES[section] || section) + "</h3>" +
          groups[section].map(sentenceHtml).join("") + "</div>";
      }).join("") || '<p class="faint">This brief has no rendered sentences.</p>';
      $$(".sentence").forEach(function (node) {
        node.addEventListener("click", function (e) {
          if (e.target.closest("button, textarea, input, .aid")) return;
          select(node.dataset.sid);
        });
      });
      $$(".sentence .aid").forEach(function (chip) {
        chip.addEventListener("click", function () { select(chip.closest(".sentence").dataset.sid, chip.dataset.aid); });
      });
      $$("[data-act]").forEach(function (b) { b.addEventListener("click", function () { act(b); }); });
      $$(".more-cites").forEach(function (b) {
        b.addEventListener("click", function () {
          b.outerHTML = b.dataset.more.split(" ").map(function (c) { return '<span class="aid" data-aid="' + esc(c) + '">' + esc(c) + "</span>"; }).join("");
        });
      });
    }
    // A summary sentence can cite twenty-odd records. Show the first few and
    // fold the rest behind a count, so the sentence stays readable and nothing
    // is hidden: one click shows them all, and the evidence panel lists every one.
    function citeChips(ids) {
      var chip = function (c) { return '<span class="aid" data-aid="' + esc(c) + '">' + esc(c) + "</span>"; };
      if (ids.length <= 4) return ids.map(chip).join("");
      return ids.slice(0, 4).map(chip).join("") +
        '<button type="button" class="chip more-cites" data-more="' + esc(ids.slice(4).join(" ")) + '">+' + (ids.length - 4) + " more</button>";
    }
    function sentenceHtml(s) {
      var text = s.text.replace(/\s*(\[[^\]]+\])+\s*$/, "").replace(/\[([A-Z]{3,6}-[^\]]+)\]/g, "");
      var state = s.finding_id ? report.states[s.finding_id] : null;
      var tier = s.evidence_tier ? '<span class="chip chip-' + esc(s.evidence_tier) + '">' + esc(s.evidence_tier.replace("_", " ")) + "</span>" : "";
      var conf = s.confidence != null ? '<span class="row" style="gap:7px"><span class="meter"><i style="width:' + (s.confidence * 100).toFixed(0) + '%"></i></span>' +
        '<span class="mono">confidence ' + fmt(s.confidence, 2) + "</span></span>" : '<span class="faint">no confidence score: a statement of record</span>';
      var review = "";
      if (!frozen()) {
        review = '<div class="review">' +
          (state ? '<span class="state">' + esc(state.action) + "d by " + esc(state.reviewer) + "</span>" : "") +
          '<button class="btn btn-sm btn-ok" data-act="approve" data-sid="' + esc(s.sentence_id) + '" data-fid="' + esc(s.finding_id || "") + '">' + ICON.check + " Approve</button>" +
          '<button class="btn btn-sm" data-act="edit" data-sid="' + esc(s.sentence_id) + '" data-fid="' + esc(s.finding_id || "") + '">Edit</button>' +
          '<button class="btn btn-sm btn-danger" data-act="reject" data-sid="' + esc(s.sentence_id) + '" data-fid="' + esc(s.finding_id || "") + '">' + ICON.cross + " Reject</button></div>";
      } else if (state) {
        review = '<div class="review"><span class="state">' + esc(state.action) + "d by " + esc(state.reviewer) + "</span></div>";
      }
      return '<div class="sentence" data-sid="' + esc(s.sentence_id) + '">' +
        '<div class="sentence-meta">' + tier + conf + "</div>" +
        '<div class="sentence-text">' + esc(text.trim()) + "</div>" +
        '<div class="sentence-cites">' + citeChips(s.citations) + "</div>" +
        review + "</div>";
    }

    // ---- selection: sentence -> evidence, and back to the photograph
    function select(sid, focusAid) {
      selected = { sid: sid, aid: focusAid || null };
      var sentence = report.sentences.filter(function (s) { return s.sentence_id === sid; })[0];
      if (sentence) {
        var region = null;
        sentence.citations.some(function (c) {
          return report.images.some(function (im, i) {
            if (im.artifact_id === c || im.regions.some(function (r) { return r.artifact_id === c && (region = r); })) {
              if (i !== imageIndex) { imageIndex = i; renderViewer(); }
              return true;
            }
            return false;
          });
        });
      } else if (focusAid) {
        report.images.forEach(function (im, i) {
          if (im.regions.some(function (r) { return r.artifact_id === focusAid; }) && i !== imageIndex) { imageIndex = i; renderViewer(); }
        });
      }
      renderEvidence(sentence, focusAid);
      applySelection();
    }
    function applySelection() {
      $$(".sentence").forEach(function (n) { n.classList.toggle("selected", !!selected && n.dataset.sid === selected.sid); });
      var sentence = selected && report.sentences.filter(function (s) { return s.sentence_id === selected.sid; })[0];
      var lit = sentence ? sentence.citations : (selected && selected.aid ? [selected.aid] : []);
      $$(".region").forEach(function (g) { g.classList.toggle("lit", lit.indexOf(g.dataset.aid) >= 0); });
      $$(".aid").forEach(function (a) { a.classList.toggle("lit", !!selected && a.dataset.aid === selected.aid); });
    }
    function renderEvidence(sentence, focusAid) {
      var panel = $("#evidence-body");
      if (!sentence && !focusAid) { renderStreams(); return; }
      var ids = sentence ? sentence.citations : [focusAid];
      $("#evidence-title").textContent = "Cited evidence";
      $("#evidence-hint").textContent = sentence
        ? "Every artifact this sentence cites, resolved to its original source. Select another sentence, or press Escape to see the evidence streams."
        : "The selected region, resolved to its source photograph.";
      panel.innerHTML = ids.map(function (id) {
        var a = report.artifacts[id];
        var region = null, image = null;
        report.images.forEach(function (im) {
          if (im.artifact_id === id) image = im;
          im.regions.forEach(function (r) { if (r.artifact_id === id) { region = r; image = im; } });
        });
        var extra = "";
        if (image) extra = '<div class="row" style="margin-top:8px"><a class="btn btn-sm" href="/original/' + encodeURIComponent(image.artifact_id) + '" target="_blank" rel="noopener">Open original photograph</a></div>';
        return '<div class="record' + (id === focusAid ? " lit" : "") + '"><div class="row"><span class="aid" data-aid="' + esc(id) + '">' + esc(id) + "</span>" +
          (a ? '<span class="chip">' + esc(a.kind) + "</span>" : '<span class="chip chip-conflicting">unresolved</span>') + "</div>" +
          '<div class="summary">' + esc(a ? a.summary : "This identifier does not resolve to a stored record.") + "</div>" +
          (region && region.description ? '<div class="summary muted">' + esc(region.description) + "</div>" : "") +
          (a ? '<div class="src">' + esc(a.source_path || "") + (a.source_locator ? "  [" + esc(a.source_locator) + "]" : "") + "</div>" : "") +
          extra + "</div>";
      }).join("");
    }
    function renderStreams() {
      $("#evidence-title").textContent = "Evidence streams";
      $("#evidence-hint").textContent = "What this brief rests on, and what it does not. Select any sentence to trace it to its source.";
      $("#evidence-body").innerHTML = '<div class="streams">' + report.streams.map(function (s) {
        return '<div class="stream ' + (s.present ? "on" : "off") + '"><span class="dot">' + (s.present ? ICON.check : ICON.minus) + "</span>" +
          "<div><strong>" + esc(s.label) + "</strong><span>" + esc(s.status) + "</span></div></div>";
      }).join("") + "</div>" +
        (report.blocked.length ? '<h3 style="margin:18px 0 8px;font-size:14px">Blocked by the grounding gate</h3>' + report.blocked.map(function (b) {
          return '<div class="record"><div class="summary">' + esc(b.text) + '</div><div class="src">' + esc(b.reason) + "</div></div>";
        }).join("") : "");
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { selected = null; renderStreams(); applySelection(); }
    });

    // ---- review actions
    function act(button) {
      var action = button.dataset.act, sid = button.dataset.sid, fid = button.dataset.fid || null;
      var who = reviewer(); if (!who) return;
      if (action === "edit") {
        var node = button.closest(".sentence");
        if (node.querySelector(".editor")) return;
        var sentence = report.sentences.filter(function (s) { return s.sentence_id === sid; })[0];
        var cites = (sentence.text.match(/\[[^\]]+\]/g) || []).join(" ");
        var prose = sentence.text.replace(/\s*(\[[^\]]+\])+\s*$/, "").trim();
        var box = document.createElement("div"); box.className = "editor";
        box.innerHTML = '<textarea class="input" aria-label="Replacement wording"></textarea>' +
          '<div class="faint" style="font-size:12px">Citations are kept automatically. The original wording is preserved in the audit trail.</div>' +
          '<div class="row"><button class="btn btn-sm btn-primary" type="button">Save edit</button><button class="btn btn-sm" type="button">Cancel</button></div>';
        box.querySelector("textarea").value = prose;
        node.appendChild(box);
        box.querySelector(".btn-primary").addEventListener("click", function () {
          var text = box.querySelector("textarea").value.trim();
          if (!text) return;
          if (!/\[[A-Z]{3,6}-[^\]]+\]/.test(text)) text = text + " " + cites;
          send({ action: "edit", sentence_id: sid, finding_id: fid, text_after: text, reviewer: who });
        });
        box.querySelector(".btn:not(.btn-primary)").addEventListener("click", function () { box.remove(); });
        return;
      }
      send({ action: action, sentence_id: sid, finding_id: fid, reviewer: who });
    }
    function send(body) {
      api("/api/report/" + encodeURIComponent(report.brief.brief_id) + "/action", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
      }).then(function (fresh) { report = fresh; renderAll(); toast("Recorded: " + body.action + " by " + body.reviewer + ".", "ok"); })
        .catch(function (e) { toast(e.message, "warn"); });
    }

    // ---- sign-off and publication
    function renderSignoff() {
      var status = report.brief.status;
      $("#status-chip").className = "chip chip-" + status;
      $("#status-chip").textContent = status.replace("_", " ");
      var banner = $("#banner");
      if (status === "signed_off") {
        banner.className = "banner signed";
        banner.innerHTML = ICON.check + "<span>Signed off by <b>" + esc(report.signoff.reviewer) + "</b> on " + esc(report.signoff.signed_at) +
          ". This records a named human's review. It is still not a safety clearance or a maintenance order.</span>";
      } else if (status === "rejected") {
        banner.className = "banner";
        banner.innerHTML = "<span>Rejected by <b>" + esc(report.signoff.reviewer) + "</b>. This brief must not be published.</span>";
      } else {
        banner.className = "banner";
        banner.innerHTML = "<span><b>Draft.</b> Assembled automatically from the evidence below. It carries no authority, is not a safety clearance and is not a maintenance order, until a named inspector signs it off.</span>";
      }
      $("#signoff-form").hidden = frozen();
      $("#trail").innerHTML = report.trail.length ? report.trail.map(function (t) {
        if (t.type === "signoff") return "<li><b>" + esc(t.reviewer) + "</b> " + esc(t.decision.replace("_", " ")) + " the brief" + (t.note ? ": " + esc(t.note) : "") + '<br><span class="faint mono">' + esc(t.signed_at) + "</span></li>";
        return "<li><b>" + esc(t.reviewer) + "</b> " + esc(t.action) + (t.action === "edit" ? " (original wording kept)" : "") + (t.note ? ": " + esc(t.note) : "") + '<br><span class="faint mono">' + esc(t.acted_at) + "</span></li>";
      }).join("") : '<li class="faint">No review actions yet.</li>';
      $("#publish").classList.toggle("btn-primary", report.publishable);
    }
    function decide(decision) {
      var who = reviewer(); if (!who) return;
      api("/api/report/" + encodeURIComponent(report.brief.brief_id) + "/signoff", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reviewer: who, decision: decision, note: $("#signoff-note").value.trim() || null })
      }).then(function (fresh) { report = fresh; renderAll(); toast(decision === "signed_off" ? "Signed off. The brief can now be published." : "Brief rejected.", "ok"); })
        .catch(function (e) { toast(e.message, "warn"); });
    }
    $("#signoff-yes").addEventListener("click", function () { decide("signed_off"); });
    $("#signoff-no").addEventListener("click", function () { decide("rejected"); });
    $("#publish").addEventListener("click", function () {
      fetch("/api/report/" + encodeURIComponent(report.brief.brief_id) + "/export?format=markdown").then(function (response) {
        if (!response.ok) return response.json().then(function (b) { throw new Error(b.error || "Publication refused."); });
        return response.blob().then(function (blob) {
          var a = document.createElement("a"); a.href = URL.createObjectURL(blob);
          a.download = report.brief.brief_id + ".md"; document.body.appendChild(a); a.click();
          setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
          toast("Published: the signed brief was downloaded with its evidence table.", "ok");
        });
      }).catch(function (e) { toast(e.message, "warn"); });
    });

    function renderKpis() {
      var st = report.stats;
      $("#kpi-findings").textContent = fmt(st.conflicting + st.corroborated + st.single_source);
      $("#kpi-findings-note").textContent = st.conflicting + " conflicting, " + st.corroborated + " corroborated, " + st.single_source + " single source";
      $("#kpi-regions").textContent = fmt(st.regions);
      $("#kpi-regions-note").textContent = "across " + st.photos + " photograph(s)";
      $("#kpi-confidence").textContent = st.mean_confidence == null ? "n/a" : fmt(st.mean_confidence, 2);
      $("#kpi-links").textContent = st.citations ? pct(st.resolved / st.citations, 0) : "n/a";
      $("#kpi-links-note").textContent = st.resolved + " of " + st.citations + " citations resolve";
      $("#kpi-blocked").textContent = fmt(st.blocked);
      $("#kpi-blocked-note").textContent = "of " + st.generated + " generated sentences";
      $("#kpi-missing").textContent = fmt(st.missing_streams);
    }
    function renderAll() {
      renderKpis(); renderViewer(); renderSentences(); renderSignoff();
      if (selected) select(selected.sid, selected.aid); else renderStreams();
    }
    renderAll();
  }

  // ------------------------------------------------------------------ metrics page

  function metricsPage() {
    $$(".gauge .fill").forEach(function (circle) {
      var value = parseFloat(circle.dataset.value);
      var length = 2 * Math.PI * 40;
      circle.style.strokeDasharray = length;
      circle.style.strokeDashoffset = length;
      if (!isNaN(value)) requestAnimationFrame(function () { circle.style.strokeDashoffset = length * (1 - Math.max(0, Math.min(1, value))); });
    });
    var button = $("#recompute");
    if (!button) return;
    function poll() {
      api("/api/metrics/status").then(function (s) {
        if (s.running) { setTimeout(poll, 2500); return; }
        if (s.error) { toast("Recompute failed: " + s.error, "warn"); button.disabled = false; return; }
        window.location.reload();
      });
    }
    button.addEventListener("click", function () {
      button.disabled = true; button.textContent = "Recomputing, about a minute...";
      api("/api/metrics/recompute", { method: "POST" }).then(poll).catch(function (e) { toast(e.message, "warn"); button.disabled = false; });
    });
  }

  // ------------------------------------------------------------------ boot

  document.addEventListener("DOMContentLoaded", function () {
    var page = document.body.dataset.page, data = pageData();
    bindTilt(document); bindReveal();
    var canvas = $("#bridge"); if (canvas) bridge3d(canvas);
    if (page === "inspect") inspectPage(data);
    if (page === "report") reportPage(data);
    if (page === "metrics") metricsPage(data);
  });
})();
