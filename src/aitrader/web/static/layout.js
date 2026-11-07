// Every page: long explanations fold to a few lines with "Read more", and card grids are packed
// into columns (each card goes under the shortest column) so the page fills without gaps.
(function () {
  "use strict";
  var KEY = "aitrader-more:" + location.pathname, LONG = 220, GAP = 12;

  function load() { try { return JSON.parse(sessionStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } }
  function save(o) { try { sessionStorage.setItem(KEY, JSON.stringify(o)); } catch (e) { /* private mode */ } }
  function len(el) { return el.textContent.replace(/\s+/g, " ").trim().length; }
  function prose(el) {  // plain explanation text: not an alert, not a form, not inside a table or list
    return el && el.tagName === "P" && !/\b(good|bad|warn)\b/.test(el.className) &&
      !el.querySelector("form,input,button,select,textarea,canvas,svg") && !el.id &&
      !el.closest("table,ul,ol,.more-body,.tick,header,nav");
  }

  function readMore() {
    var open = load(), n = 0, seen = [];
    var ps = document.querySelectorAll("main p");
    for (var i = 0; i < ps.length; i++) {
      var p = ps[i];
      if (seen.indexOf(p) >= 0 || !prose(p)) continue;
      var run = [p], e = p.nextElementSibling;
      while (prose(e)) { run.push(e); e = e.nextElementSibling; }
      seen = seen.concat(run);
      // a short status line in front stays visible; only the explanation after it folds
      if (run.length > 1 && !/\bmuted\b/.test(run[0].className) && len(run[0]) < 160) run.shift();
      var total = 0;
      for (var j = 0; j < run.length; j++) total += len(run[j]);
      if (total < LONG) continue;
      fold(run, "m" + (n++), !!open["m" + (n - 1)]);
    }
  }

  function fold(run, id, isOpen) {
    var wrap = document.createElement("div"), body = document.createElement("div"),
        btn = document.createElement("button");
    wrap.className = "more"; body.className = "more-body";
    btn.type = "button"; btn.className = "more-btn";
    run[0].parentNode.insertBefore(wrap, run[0]);
    for (var i = 0; i < run.length; i++) body.appendChild(run[i]);
    wrap.appendChild(body); wrap.appendChild(btn);
    function set(o) {
      wrap.classList.toggle("closed", !o);
      btn.textContent = o ? "Show less" : "Read more";
      btn.setAttribute("aria-expanded", o ? "true" : "false");
    }
    set(isOpen);
    btn.addEventListener("click", function () {
      var o = wrap.classList.contains("closed"), s = load();
      set(o);
      if (o) s[id] = 1; else delete s[id];
      save(s);
    });
  }

  function pack() {
    var grids = document.querySelectorAll("main .grid");
    for (var g = 0; g < grids.length; g++) {
      var grid = grids[g];
      if (!grid._items) grid._items = Array.prototype.slice.call(grid.children);
      var items = grid._items, min = grid.classList.contains("pack") ? 340 : 300;
      var cols = Math.min(items.length, Math.max(1, Math.floor((grid.clientWidth + GAP) / (min + GAP))));
      if (grid._cols === cols) continue;
      grid._cols = cols;
      for (var i = 0; i < items.length; i++) grid.appendChild(items[i]);
      var old = grid.querySelectorAll(":scope > .mcol");
      for (var k = 0; k < old.length; k++) grid.removeChild(old[k]);
      if (cols < 2) { grid.classList.remove("masonry"); continue; }
      grid.classList.add("masonry");
      var cs = [];
      for (var c = 0; c < cols; c++) { cs.push(document.createElement("div")); cs[c].className = "mcol"; grid.appendChild(cs[c]); }
      for (var m = 0; m < items.length; m++) {
        var low = cs[0];
        for (var c2 = 1; c2 < cs.length; c2++) if (cs[c2].offsetHeight < low.offsetHeight) low = cs[c2];
        low.appendChild(items[m]);
      }
    }
  }

  function start() {
    readMore();
    pack();
    var t;
    window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(pack, 150); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
