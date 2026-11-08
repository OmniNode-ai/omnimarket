/* The floor's shared details panel and searchable, paged list (OMN-23018).
 *
 * One component for the first page and the Better or worse page. It draws nothing until something is opened.
 *
 *   FloorPanel.mount(el, items, {id})   a paged list (25 rows) into any element, with a search box when it holds more than
 *                                        25 items. An item is {title, sub, group, href, at, count, tag, tone, opens, find}.
 *   FloorPanel.reset(id)                forget a list's search and page (a page does this when its filters change).
 *   FloorPanel.configure({resolve})     resolve(key) returns the panel spec for a key, or null. A spec is {key, title,
 *                                        kicker, summary, tone, facts:[{label,value,at}], sources:[text], total, lead
 *                                        (html the page already escaped), sections:[{heading, note, items, empty}]}.
 *   FloorPanel.open(spec) / close() / update() / sync()
 *   Any element with data-open="<key>" opens that key when tapped (Enter or Space on a role="button").
 *
 * The panel shows its lines and, when the spec names a number (total), the sum of its lines' counts next to it: a panel
 * whose lines do not add up to its number says so in red. A list that cannot be searched or paged is never drawn longer
 * than 25 rows. The component reads nothing but the spec it is given.
 */
(function (root) {
  "use strict";
  var PAGE = 25, SEARCH_OVER = 25;
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  };
  var agoText = function (s) {
    return s == null ? "never" : s < 120 ? s + " s ago" : s < 7200 ? Math.floor(s / 60) + " min ago" : s < 172800 ? Math.floor(s / 3600) + " h ago" : Math.floor(s / 86400) + " d ago";
  };
  var sinceIso = function (iso) {
    var t = Date.parse(iso);
    return isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 1000));
  };
  var safeHref = function (u) { return /^https?:\/\//.test(String(u || "")) ? u : ""; };

  // ---- the list ------------------------------------------------------------------------------------------------------
  var listState = {};   // list id -> {q, page}: kept across redraws of the same panel
  function itemHTML(it) {
    var tone = it.tone ? " fp-t " + esc(it.tone) : "";
    var title = esc(it.title);
    var head;
    if (it.opens) head = '<button type="button" class="fp-open' + tone + '" data-open="' + esc(it.opens) + '">' + title + "</button>";
    else if (safeHref(it.href)) head = '<a class="' + tone.trim() + '" href="' + esc(it.href) + '" target="_blank" rel="noopener noreferrer">' + title + "</a>";
    else head = '<span class="t' + tone + '">' + title + "</span>";
    var chip = it.tag ? '<span class="fp-chip">' + esc(it.tag) + "</span>" : "";
    var right = "";
    if (it.count !== 1 && it.count != null) right += '<span class="fp-cnt">' + (it.count > 0 ? "×" + esc(it.count) : "−" + esc(-it.count)) + "</span> ";
    if (it.at) { var s = sinceIso(it.at); if (s != null) right += '<span title="' + esc(it.at) + '">' + esc(agoText(s)) + "</span>"; }
    return '<li class="fp-it"><div><span class="t">' + head + chip + "</span>" + (it.sub ? '<span class="s">' + esc(it.sub) + "</span>" : "") + "</div>" + (right ? '<div class="r">' + right + "</div>" : "") + "</li>";
  }
  var hay = function (it) { return [it.title, it.sub, it.group, it.tag, it.find].join(" ").toLowerCase(); };   // find: text the search matches and the line does not draw
  function listBody(items, st) {
    var q = st.q.trim().toLowerCase();
    var rows = q ? items.filter(function (it) { return hay(it).indexOf(q) >= 0; }) : items;
    var pages = Math.max(1, Math.ceil(rows.length / PAGE));
    if (st.page >= pages) st.page = pages - 1;
    var from = st.page * PAGE, slice = rows.slice(from, from + PAGE), h = "", last = null;
    for (var i = 0; i < slice.length; i++) {
      var g = slice[i].group || "";
      if (g && g !== last) h += '<li class="fp-grp" role="presentation">' + esc(g) + "</li>";
      last = g || last;
      h += itemHTML(slice[i]);
    }
    if (!slice.length) h = '<li class="fp-empty">' + (q ? "No line matches “" + esc(st.q.trim()) + "”." : "None.") + "</li>";
    var pager = "";
    if (rows.length > PAGE) {
      pager = '<div class="fp-pg"><button type="button" data-pg="-1"' + (st.page === 0 ? " disabled" : "") + '>Previous</button><span>' + (from + 1) + "–" + (from + slice.length) + " of " + rows.length + (q ? " matching" : "") + '</span><button type="button" data-pg="1"' + (st.page >= pages - 1 ? " disabled" : "") + ">Next</button></div>";
    } else if (q) pager = '<div class="fp-pg"><span>' + rows.length + " of " + items.length + " match</span></div>";
    return '<ul class="fp-list">' + h + "</ul>" + pager;
  }
  function mount(el, items, opts) {
    opts = opts || {};
    var id = opts.id || "list", st = listState[id] || (listState[id] = {q: "", page: 0});
    var searchable = items.length > (opts.searchOver || SEARCH_OVER);
    el.classList.add("fp-listbox");
    el.innerHTML = (searchable ? '<input class="fp-q" type="search" placeholder="Search ' + items.length + ' lines" aria-label="Search the lines" value="' + esc(st.q) + '">' : "") + '<div class="fp-body"></div>';
    var body = el.querySelector(".fp-body");
    var paint = function () { body.innerHTML = listBody(items, st); };
    paint();
    var q = el.querySelector(".fp-q");
    if (q) q.addEventListener("input", function () { st.q = q.value; st.page = 0; paint(); });
    // One click handler per host: a page that mounts into the same element on every redraw must not stack them.
    if (el.__fpClick) el.removeEventListener("click", el.__fpClick);
    el.__fpClick = function (e) {
      var b = e.target.closest ? e.target.closest("button[data-pg]") : null;
      if (!b || b.disabled) return;
      st.page += Number(b.getAttribute("data-pg"));
      paint();
      var top = el.getBoundingClientRect().top;
      if (top < 0 && el.scrollIntoView) el.scrollIntoView({block: "start"});
    };
    el.addEventListener("click", el.__fpClick);
    return {rows: items.length, state: st};
  }

  // ---- the panel -----------------------------------------------------------------------------------------------------
  var cfg = {resolve: function () { return null; }};
  var back = null, panel = null, openKey = null, origin = null, spec = null;
  function ensure() {
    if (panel) return;
    back = document.createElement("div"); back.id = "fp-back";
    panel = document.createElement("aside");
    panel.id = "fp-panel"; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-labelledby", "fp-title"); panel.setAttribute("aria-hidden", "true");
    panel.innerHTML = '<div class="fp-h"><div><div class="fp-kicker" id="fp-kicker"></div><h2 id="fp-title"></h2></div><button id="fp-close" class="fp-x" type="button" aria-label="Close the details">Close</button></div><div id="fp-body"></div>';
    document.body.append(back, panel);
    back.addEventListener("click", close);
    panel.addEventListener("click", function (e) { if (e.target.id === "fp-close") close(); });
  }
  var sumLines = function (s) {
    var n = 0;
    (s.sections || []).forEach(function (sec) { (sec.items || []).forEach(function (it) { n += it.count == null ? 1 : it.count; }); });
    return n;
  };
  function body(s) {
    var h = "";
    if (s.tone) h += '<span class="fp-pill ' + esc(s.tone) + '">' + esc(s.pill || ({red: "Red", amber: "Amber", green: "Green", grey: "Quiet"}[s.tone] || s.tone)) + "</span>";
    if (s.summary) h += "<p style=\"margin-top:8px\">" + esc(s.summary) + "</p>";
    if (s.lead) h += s.lead;
    if ((s.facts || []).length) {
      h += '<div class="fp-facts">' + s.facts.map(function (f) {
        var age = f.at ? sinceIso(f.at) : null;
        var v = (f.value || "") + (age != null ? (f.value ? " · " : "") + agoText(age) : "");
        return "<div><span>" + esc(f.label) + "</span><b" + (f.at ? ' title="' + esc(f.at) + '"' : "") + ">" + esc(v) + "</b></div>";
      }).join("") + "</div>";
    }
    if ((s.sources || []).length) h += "<h3>Where it comes from</h3><ul class=\"fp-src\">" + s.sources.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul>";
    if (s.total != null) {
      var n = sumLines(s), ok = n === s.total;
      h += '<p class="fp-sum' + (ok ? "" : " bad") + '" data-fp-sum="' + (ok ? "ok" : "bad") + '">' + (ok ? "The lines below add up to the number: " + n + "." : "The lines below add up to " + n + ", not the number " + s.total + ". Do not trust this list.") + "</p>";
    }
    (s.sections || []).forEach(function (sec, i) {
      var n = (sec.items || []).length;
      h += "<h3>" + esc(sec.heading) + (n ? " (" + n + ")" : "") + "</h3>";
      if (sec.note) h += '<p class="fp-note">' + esc(sec.note) + "</p>";
      h += n ? '<div data-fp-list="' + i + '"></div>' : '<p class="fp-empty">' + esc(sec.empty || "None.") + "</p>";
    });
    return h;
  }
  function render() {
    var s = spec;
    document.getElementById("fp-kicker").textContent = s.kicker || "";
    document.getElementById("fp-title").textContent = s.title || "";
    var box = document.getElementById("fp-body");
    var active = document.activeElement, keep = null;
    if (active && active.classList && active.classList.contains("fp-q")) {
      var host = active.closest("[data-fp-list]");
      keep = {sec: host ? host.getAttribute("data-fp-list") : null, at: active.selectionStart};
    }
    var top = panel.scrollTop;
    box.innerHTML = body(s);
    (s.sections || []).forEach(function (sec, i) {
      var host = box.querySelector('[data-fp-list="' + i + '"]');
      if (host) mount(host, sec.items, {id: s.key + "|" + i});
    });
    panel.scrollTop = top;
    if (keep && keep.sec != null) {
      var again = box.querySelector('[data-fp-list="' + keep.sec + '"] .fp-q');
      if (again) { again.focus({preventScroll: true}); try { again.setSelectionRange(keep.at, keep.at); } catch (e) { /* not supported */ } }
    }
  }
  var hashOf = function (key) { var i = key.indexOf(":"); return "#" + (i < 0 ? key : key.slice(0, i) + "-" + encodeURIComponent(key.slice(i + 1))); };
  var keyOf = function (hash) {
    var m = /^#([a-z]+)-(.+)$/.exec(hash || "");
    if (!m) return null;
    try { return m[1] + ":" + decodeURIComponent(m[2]); } catch (e) { return null; }
  };
  function show(s) {
    ensure();
    var first = openKey == null;
    if (openKey !== s.key) Object.keys(listState).forEach(function (k) { if (k.indexOf(s.key + "|") !== 0) delete listState[k]; });
    spec = s; openKey = s.key;
    render();
    back.classList.add("open"); panel.classList.add("open"); panel.setAttribute("aria-hidden", "false");
    if (first) document.getElementById("fp-close").focus({preventScroll: true});
  }
  function hide() {
    if (openKey == null) return;
    openKey = null; spec = null;
    back.classList.remove("open"); panel.classList.remove("open"); panel.setAttribute("aria-hidden", "true");
    var again = origin && origin.key ? document.querySelector('[data-open="' + origin.key.replace(/"/g, '\\"') + '"]') : null;
    var target = (origin && origin.el && origin.el.isConnected) ? origin.el : again;
    if (target && target.focus) { root.FloorPanel.restoring = true; target.focus({preventScroll: true}); root.FloorPanel.restoring = false; }   // a page's focus tooltip stays quiet while this runs
    origin = null;
  }
  function open(s, o) {
    o = o || {};
    if (!s) return;
    if (openKey == null) origin = {el: o.from || document.activeElement, key: s.key};
    var depth = (history.state && history.state.fp && openKey != null ? history.state.depth : -1) + 1;
    if (o.hash !== false && location.hash !== hashOf(s.key)) history.pushState({fp: s.key, depth: depth}, "", hashOf(s.key));
    show(s);
  }
  function close() {
    if (openKey == null) return;
    var depth = history.state && history.state.fp ? history.state.depth : 0;
    if (depth > 0 || (history.state && history.state.fp)) history.go(-(depth + 1));   // every panel opened from this page, undone
    else { history.replaceState(null, "", location.pathname + location.search); hide(); }
  }
  function sync() {
    var key = keyOf(location.hash), s = key ? cfg.resolve(key) : null;
    if (s) show(s); else hide();
  }
  function update() {
    if (openKey == null) return;
    var s = cfg.resolve(openKey);
    if (s) { spec = s; render(); } else hide();
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target.closest("[data-open]") : null;
    if (!t || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    var s = cfg.resolve(t.getAttribute("data-open"));
    if (!s) return;
    e.preventDefault();
    open(s, {from: t});
  });
  document.addEventListener("keydown", function (e) {
    if (openKey == null) {
      var t = e.target;
      if ((e.key === "Enter" || e.key === " ") && t && t.getAttribute && t.getAttribute("role") === "button" && t.hasAttribute("data-open")) { e.preventDefault(); t.click(); }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab") return;
    var fs = Array.prototype.slice.call(panel.querySelectorAll("button:not([disabled]), a[href], input, [tabindex]:not([tabindex='-1'])"));
    if (!fs.length) return;
    var first = fs[0], last = fs[fs.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  window.addEventListener("popstate", sync);
  window.addEventListener("hashchange", sync);

  root.FloorPanel = {
    mount: mount, reset: function (id) { delete listState[id]; }, bodyHTML: body, open: open, close: close, update: update, sync: sync,
    configure: function (c) { cfg = Object.assign({}, cfg, c); },
    isOpen: function () { return openKey != null; },
    key: function () { return openKey; },
    PAGE: PAGE, SEARCH_OVER: SEARCH_OVER, restoring: false
  };
})(window);
