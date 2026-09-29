/* The scrubber: the years down the right edge of the grid.

   A photo library is a long scroll with a date on every screen of it, and
   "the summer before last" is a place you want to go to, not scroll to. The
   strip is the library's months newest at the top, each given room by the
   square root of how many photos it holds: plain proportion would make a
   quiet year a hairline between two busy ones, and equal shares would make
   the busy months impossible to aim within. Years are labelled where they
   start; hover says which month is under the pointer; a click (or a drag and
   release) goes there. While the grid scrolls, a line says where it is.

   It only makes sense for a listing in date order, so it steps aside for word
   and similarity searches. The months come from /api/timeline with the same
   filters as the grid, which the server applies minus the words. */

window.App = window.App || {};

App.timeline = (() => {
  const YEAR_GAP = 7;       // px between one year's months and the next's
  const LABEL_GAP = 15;     // closer year labels than this are left out

  let el = null;
  let stage = null;
  let months = [];
  let spans = [];           // { month, count, top, h }
  let seq = 0;
  let nowEl = null;
  let hoverEl = null;
  let bubble = null;
  let dragging = false;
  let hovering = false;
  let current = null;       // YYYY-MM the grid is showing
  let bubbleTimer = null;

  // Only once the grid has answered for the current query: until then its
  // mode is the previous query's, and the strip would flash up beside a
  // relevance-ordered result for a frame.
  function wanted() {
    const info = App.grid.info();
    return App.state.view === "grid" && info.q === App.state.q && info.mode === "date"
      && !info.error && months.length > 1;
  }

  async function load() {
    const mine = ++seq;
    const q = App.state.q;
    try {
      const payload = await App.api.get(`/api/timeline${q ? `?q=${encodeURIComponent(q)}` : ""}`);
      if (mine !== seq) return;
      months = (payload.months || []).filter((m) => m.count > 0);
    } catch (e) {
      if (mine !== seq) return;
      months = [];
    }
    render();
  }

  function render() {
    const show = wanted();
    const was = !el.hidden;
    el.hidden = !show;
    stage.classList.toggle("scrubbing", show);
    if (was !== show) App.grid.fitWidth();
    if (!show) return;
    const H = el.clientHeight;
    if (!H) return;
    const years = new Set(months.map((m) => m.month.slice(0, 4))).size;
    const avail = Math.max(10, H - YEAR_GAP * (years - 1));
    const weights = months.map((m) => Math.sqrt(m.count));
    const sum = weights.reduce((s, w) => s + w, 0) || 1;

    spans = [];
    let y = 0;
    let lastYear = null;
    months.forEach((m, i) => {
      const year = m.month.slice(0, 4);
      if (lastYear !== null && year !== lastYear) y += YEAR_GAP;
      const h = (weights[i] / sum) * avail;
      spans.push({ month: m.month, count: m.count, top: y, h, year, first: year !== lastYear });
      y += h;
      lastYear = year;
    });

    const nodes = [];
    let lastLabel = -Infinity;
    let lastDot = -Infinity;
    spans.forEach((s) => {
      if (s.first && s.top - lastLabel >= LABEL_GAP) {
        nodes.push(App.el("span", { class: "scrub-year", style: `top:${s.top}px`, text: s.year }));
        lastLabel = s.top;
        lastDot = s.top + 8;
        return;
      }
      const mid = s.top + s.h / 2;
      // A dot per month, where there is room for one to mean something.
      if (mid - lastDot >= 5 && mid - lastLabel >= 12) {
        nodes.push(App.el("span", { class: "scrub-dot", style: `top:${mid - 1.5}px` }));
        lastDot = mid;
      }
    });
    nowEl = App.el("span", { class: "scrub-now", hidden: current === null });
    hoverEl = App.el("span", { class: "scrub-hover", hidden: true });
    bubble = App.el("span", { class: "scrub-bubble", hidden: true });
    el.replaceChildren(...nodes, nowEl, hoverEl, bubble);
    paintNow(false);
  }

  function spanAt(y) {
    if (!spans.length) return null;
    let lo = 0;
    let hi = spans.length - 1;
    let hit = spans[0];
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (spans[mid].top <= y) { hit = spans[mid]; lo = mid + 1; } else hi = mid - 1;
    }
    return hit;
  }

  function label(s) { return `${App.fmt.month(s.month)} · ${App.fmt.n(s.count)}`; }

  function showBubble(y, text) {
    if (!bubble) return;
    bubble.textContent = text;
    bubble.style.top = `${Math.max(10, Math.min(el.clientHeight - 10, y))}px`;
    bubble.hidden = false;
  }

  function paintNow(announce) {
    if (!nowEl) return;
    const s = current ? spans.find((sp) => sp.month === current) : null;
    nowEl.hidden = !s;
    if (!s) return;
    const y = s.top + Math.min(s.h / 2, 6);
    nowEl.style.top = `${y}px`;
    if (announce && !hovering && !dragging) {
      showBubble(y, App.fmt.month(s.month));
      clearTimeout(bubbleTimer);
      bubbleTimer = setTimeout(() => { if (!hovering && !dragging && bubble) bubble.hidden = true; }, 900);
    }
  }

  function pointerY(e) { return e.clientY - el.getBoundingClientRect().top; }

  function hover(e) {
    const y = pointerY(e);
    const s = spanAt(y);
    if (!s || !hoverEl) return null;
    hoverEl.style.top = `${y}px`;
    hoverEl.hidden = false;
    showBubble(y, label(s));
    return s;
  }

  function init() {
    el = document.getElementById("scrubber");
    stage = document.getElementById("stage");

    el.addEventListener("pointerenter", () => { hovering = true; });
    el.addEventListener("pointermove", (e) => { hover(e); });
    el.addEventListener("pointerleave", () => {
      hovering = false;
      if (dragging) return;
      if (hoverEl) hoverEl.hidden = true;
      if (bubble) bubble.hidden = true;
    });
    // A drag down the strip previews months and goes on release: jumping at
    // every month on the way would start a dozen listings for one gesture.
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      dragging = true;
      el.setPointerCapture(e.pointerId);
      hover(e);
    });
    el.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      const s = hover(e);
      if (!hovering) { if (hoverEl) hoverEl.hidden = true; if (bubble) bubble.hidden = true; }
      if (s) App.grid.jump(s.month);
    });
    el.addEventListener("pointercancel", () => { dragging = false; });

    App.bus.on("grid-position", ({ day, quiet }) => {
      const month = day ? day.slice(0, 7) : null;
      if (month === current) return;
      current = month;
      paintNow(!quiet);
    });
    App.bus.on("grid", () => render());

    let raf = 0;
    new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(render);
    }).observe(el.parentElement);
  }

  return { init, load, render };
})();
