/* The grid: every photo, newest first, as justified rows under day headings.

   Justified rather than square: a square crop cuts the top off every portrait
   and the sides off every panorama, and a photo library is looked at to find
   *the* picture, which is a matter of seeing it whole. Each row is scaled so
   its pictures, at their own aspect ratios, exactly fill the width; the row
   height hovers around a target the reader sets with + and -.

   Loading is a keyset walk down the timeline, a page at a time as the bottom of
   the grid comes near. A jump from the scrubber starts the walk at that month
   instead, and from there it goes both ways: the newer photos are fetched a
   page at a time as the top comes near, and laid in above without moving the
   picture on screen. Home goes back to the newest.

   Word, similarity and face searches come back in relevance order. There, day
   headings would chop the list into one-picture days, so the grid is one flat
   run and each tile says its date on hover. `sort:date` puts the headings back.

   The tiles are the drag sources for the program's first job (see
   app.drag.js), so they carry `draggable` and nothing inside them does. */

window.App = window.App || {};

App.grid = (() => {
  const PAGE = 150;
  const SIZE = { min: 120, max: 360, step: 30, fallback: 200 };
  // A panorama at its true ratio is a sliver row; a 1:4 screenshot is a
  // column. Clamped, and cropped by object-fit where it exceeds the clamp.
  const ASPECT = { min: 0.4, max: 3 };

  let view = null;          // the scroller
  let body = null;
  let head = null;
  let topEl = null;         // above the first day while there are newer photos to fetch
  let sentinel = null;
  let endEl = null;
  let pill = null;
  let selbar = null;

  let items = [];           // every card loaded, in order
  let pos = new Map();      // id -> index in items
  let tiles = new Map();    // id -> tile element (reused across relayouts)
  let sections = [];        // runs of one day (or one flat run), in order
  let secOf = new Map();    // id -> section

  let next = null;          // the cursor for the next page, null at the end
  let prev = null;          // the cursor for the page above, null at the top
  let mode = "date";
  let total = null;
  let query = null;
  let similarTo = null;
  let face = null;          // a face search's face: { id, url, photo_id }
  // YYYY-MM after a scrubber jump, a day or "undated" after a reveal; null
  // again once the walk up has reached the newest, when it is the plain listing.
  let from = null;
  let dated = true;         // drawn with day headings
  let loadedQ = "";
  let loading = false;
  let error = null;
  let seq = 0;
  let pending = null;       // the page being fetched, shared by every caller
  let pendingUp = null;     // the same for the page above
  let upError = null;       // the page above failed; the top offers to try again
  let width = 0;
  let headH = 40;

  const selected = new Set();
  let anchor = null;        // the last tile a selection click landed on
  let focusId = null;

  let refreshing = false;
  let lastThumbs = null;

  // --- sizes -----------------------------------------------------------------

  function gap() {
    return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--tile-gap")) || 4;
  }

  /* The row height the justifier aims at. A phone gets a smaller one than
     the setting says: 200 px rows on a 390 px screen are two pictures a row,
     which is a slideshow, not an overview. */
  function target() {
    const s = Number(App.state.prefs.size) || SIZE.fallback;
    return width && width < 600 ? Math.round(s * 0.6) : s;
  }

  function aspect(card) {
    const a = card.w && card.h ? card.w / card.h : 1;
    return Math.min(ASPECT.max, Math.max(ASPECT.min, a));
  }

  /* Widths for a row that must fill `W` exactly. Floored, with the remainder
     given to the last picture, so rounding never pushes a row past the edge. */
  function fill(row, W, g) {
    const sum = row.reduce((s, r) => s + r.a, 0);
    const h = (W - g * (row.length - 1)) / sum;
    const widths = row.map((r) => Math.floor(r.a * h));
    const used = widths.slice(0, -1).reduce((s, w) => s + w, 0);
    widths[widths.length - 1] = Math.max(1, Math.floor(W - g * (row.length - 1) - used));
    return { cards: row.map((r) => r.card), widths, h: Math.round(h * 100) / 100 };
  }

  /* The justifier. Greedy, with one look back: when a picture overflows the
     row, the row closes either with it (and shrinks) or without it (and
     grows), whichever lands nearer the target height. Without the look back a
     wide picture arriving last squashes a whole row to a strip. */
  function justify(cards, W, H, g) {
    const rows = [];
    let row = [];
    let sum = 0;
    for (const card of cards) {
      const a = aspect(card);
      row.push({ card, a });
      sum += a;
      if (sum * H + g * (row.length - 1) < W) continue;
      let at = row.length;
      if (row.length > 1) {
        const hWith = (W - g * (row.length - 1)) / sum;
        const hWithout = (W - g * (row.length - 2)) / (sum - a);
        if (Math.abs(Math.log(hWithout / H)) < Math.abs(Math.log(hWith / H))) at = row.length - 1;
      }
      rows.push(fill(row.slice(0, at), W, g));
      row = row.slice(at);
      sum = row.reduce((s, r) => s + r.a, 0);
    }
    // The last row keeps the target height rather than stretching three
    // pictures across the whole width.
    if (row.length) {
      rows.push({ cards: row.map((r) => r.card), widths: row.map((r) => Math.floor(r.a * H)), h: H });
    }
    return rows;
  }

  // --- tiles -----------------------------------------------------------------

  function tileFor(card) {
    let t = tiles.get(card.id);
    if (!t) {
      t = App.el("div", { class: "tile", draggable: "true", dataset: { id: String(card.id) } });
      paintTile(t, card);
      tiles.set(card.id, t);
    }
    return t;
  }

  /* Drawn blurred until hovered: flagged by the classifier, and the reader
     asked for that (state.nsfw.blur). */
  const veiled = (card) => Boolean(App.state.nsfw && App.state.nsfw.blur && card.nsfw_flag);

  function paintTile(t, card) {
    App.hover.release(t);
    const kids = [];
    if (card.thumb) {
      kids.push(App.el("img", { src: card.thumb, alt: "", loading: "lazy", decoding: "async", draggable: "false" }));
    } else {
      // Not made yet. The kind of thing it is, where the picture will be; the
      // status poll brings the real one in once the agent has made it.
      kids.push(App.el("span", { class: "tile-ph", html: App.icon(card.kind === "video" ? "video" : "photo", 26) }));
    }
    if (card.kind === "video") {
      kids.push(App.el("span", { class: "badge video" },
        App.el("span", { html: App.icon("play", 12) }),
        card.duration ? App.fmt.duration(card.duration) : ""));
    } else if (card.live) {
      kids.push(App.el("span", { class: "badge live", title: "Live Photo" },
        App.el("span", { html: App.icon("live", 14) }), "LIVE"));
    }
    // Bottom left, where Photos puts it: the heart set on the phone.
    if (card.favorite) kids.push(App.el("span", { class: "badge fav", html: App.icon("heartFill", 14) }));
    if (veiled(card)) kids.push(App.el("span", { class: "tile-veil", html: App.icon("eyeOff", 22) }));
    kids.push(App.el("button", {
      class: "tile-check", type: "button", tabindex: "-1", "aria-label": "Select",
      title: "Select (x)", html: App.icon("select", 22),
    }));
    if (!dated) kids.push(App.el("span", { class: "tile-date", text: card.taken ? App.fmt.short(card.taken) : "No date" }));
    t.replaceChildren(...kids);
    const bits = [card.name];
    if (card.taken) bits.push(App.fmt.dateTime(card.taken));
    if (card.place) bits.push(card.place);
    if (veiled(card)) bits.push("Possibly explicit: shown on hover");
    t.title = bits.join(" · ");
    t.classList.toggle("selected", selected.has(card.id));
    t.classList.toggle("focused", focusId === card.id);
    t.classList.toggle("veiled", veiled(card));
    t.classList.toggle("fav", Boolean(card.favorite));
  }

  // --- sections --------------------------------------------------------------

  function makeSection(key, atTop = false) {
    const sec = { key, day: dated && key !== "undated" ? key : null, items: [], height: 0 };
    sec.el = App.el("section", { class: dated ? "day" : "day flat", dataset: { key } });
    if (dated) {
      sec.check = App.el("button", {
        class: "day-check", type: "button", title: "Select this day",
        html: App.icon("select", 18), dataset: { action: "day" },
      });
      sec.city = App.el("span", { class: "day-city" });
      sec.head = App.el("header", { class: "day-head" },
        sec.check,
        App.el("span", { class: "day-label", text: key === "undated" ? "No date" : App.fmt.day(key) }),
        sec.city);
      sec.el.append(sec.head);
    }
    sec.rows = App.el("div", { class: "rows" });
    sec.el.append(sec.rows);
    if (atTop) {
      body.prepend(sec.el);
      sections.unshift(sec);
    } else {
      body.append(sec.el);
      sections.push(sec);
    }
    return sec;
  }

  /* The town most of the day's pictures were taken in: the part of the
     heading that says what the day *was*. */
  function paintCity(sec) {
    if (!sec.city) return;
    const counts = new Map();
    sec.items.forEach((c) => { if (c.city) counts.set(c.city, (counts.get(c.city) || 0) + 1); });
    let best = "";
    let n = 0;
    counts.forEach((v, k) => { if (v > n) { best = k; n = v; } });
    sec.city.textContent = best;
  }

  function layoutSection(sec) {
    const g = gap();
    const rows = justify(sec.items, width, target(), g);
    const frag = document.createDocumentFragment();
    let height = 0;
    rows.forEach((row) => {
      const el = App.el("div", { class: "row", style: `height:${row.h}px` });
      row.cards.forEach((card, i) => {
        const t = tileFor(card);
        t.style.width = `${row.widths[i]}px`;
        t.style.height = `${row.h}px`;
        el.append(t);
      });
      frag.append(el);
      height += row.h;
    });
    height += g * Math.max(0, rows.length - 1);
    sec.rows.replaceChildren(frag);
    if (sec.head && sec.head.offsetHeight) headH = sec.head.offsetHeight;
    sec.height = height + (sec.head ? headH : 0);
    // What an off-screen day is assumed to measure; see `.day` in the CSS.
    sec.el.style.containIntrinsicSize = `auto ${Math.round(sec.height)}px`;
    paintCity(sec);
  }

  function append(cards) {
    const dirty = new Set();
    for (const card of cards) {
      // A page boundary in relevance order is an offset, and a photo indexed
      // in between shifts it by one; never draw the same picture twice.
      if (pos.has(card.id)) continue;
      pos.set(card.id, items.length);
      items.push(card);
      const key = dated ? (card.day || "undated") : "all";
      let sec = sections[sections.length - 1];
      if (!sec || sec.key !== key) sec = makeSection(key);
      sec.items.push(card);
      secOf.set(card.id, sec);
      dirty.add(sec);
    }
    if (!width) width = body.clientWidth;
    dirty.forEach(layoutSection);
    paintDayChecks();
  }

  /* A page from above: newest first like every page, and all of it newer
     than what is drawn. Laid in from its oldest card up, so each one meets
     the day it belongs to at the top of the grid. The caller keeps the
     scroll position (see above()). */
  function prepend(cards) {
    const fresh = cards.filter((c) => !pos.has(c.id));
    if (!fresh.length) return;
    const dirty = new Set();
    for (let i = fresh.length - 1; i >= 0; i--) {
      const card = fresh[i];
      const key = dated ? (card.day || "undated") : "all";
      let sec = sections[0];
      if (!sec || sec.key !== key) sec = makeSection(key, true);
      sec.items.unshift(card);
      secOf.set(card.id, sec);
      dirty.add(sec);
    }
    items = fresh.concat(items);
    pos = new Map(items.map((c, i) => [c.id, i]));
    if (!width) width = body.clientWidth;
    dirty.forEach(layoutSection);
    paintDayChecks();
  }

  function clear() {
    App.hover.stop();
    items = [];
    pos = new Map();
    tiles = new Map();
    sections = [];
    secOf = new Map();
    body.replaceChildren();
    selected.clear();
    anchor = null;
    focusId = null;
    paintSelbar();
  }

  /* Take cards out of the drawn grid: deleted ones, which should be gone the
     moment the delete is confirmed rather than when the agent gets to it.
     The days they were in are justified again, an emptied day goes, and the
     keyboard's ring moves on to the next picture. Returns how many went. */
  function remove(ids) {
    const gone = new Set(ids);
    if (![...gone].some((id) => pos.has(id))) return 0;
    App.hover.stop();
    let focusNext = null;
    if (focusId !== null && gone.has(focusId)) {
      const i = pos.get(focusId);
      focusNext = items.slice(i + 1).find((c) => !gone.has(c.id))
        || items.slice(0, i).reverse().find((c) => !gone.has(c.id)) || null;
    }
    const dirty = new Set();
    let n = 0;
    items = items.filter((c) => {
      if (!gone.has(c.id)) return true;
      n++;
      const sec = secOf.get(c.id);
      if (sec) {
        sec.items = sec.items.filter((x) => x.id !== c.id);
        dirty.add(sec);
      }
      secOf.delete(c.id);
      const t = tiles.get(c.id);
      if (t) t.remove();
      tiles.delete(c.id);
      selected.delete(c.id);
      return false;
    });
    pos = new Map(items.map((c, i) => [c.id, i]));
    sections = sections.filter((sec) => {
      if (sec.items.length) return true;
      sec.el.remove();
      dirty.delete(sec);
      return false;
    });
    dirty.forEach(layoutSection);
    if (anchor !== null && gone.has(anchor)) anchor = null;
    if (focusId !== null && gone.has(focusId)) {
      focusId = null;
      if (focusNext) setFocus(focusNext.id, false);
    }
    if (total !== null && total !== undefined) total = Math.max(0, total - n);
    paintSelbar();
    paintEnd();
    App.shell.paintTitle();
    checkMore();
    return n;
  }

  // --- keeping the reader's place across a relayout ---------------------------

  function firstVisible() {
    const top = view.getBoundingClientRect().top + (dated ? headH : 0);
    const sec = sectionAt(view.scrollTop);
    if (!sec) return null;
    const start = sections.indexOf(sec);
    for (let k = start; k < Math.min(sections.length, start + 4); k++) {
      for (const card of sections[k].items) {
        const t = tiles.get(card.id);
        if (t && t.getBoundingClientRect().bottom > top) return card.id;
      }
    }
    return sec.items.length ? sec.items[0].id : null;
  }

  function capture() {
    const id = firstVisible();
    if (id === null) return null;
    return { id, offset: tiles.get(id).getBoundingClientRect().top - view.getBoundingClientRect().top };
  }

  function restore(mark) {
    if (!mark) return;
    const t = tiles.get(mark.id);
    if (!t) return;
    const now = t.getBoundingClientRect().top - view.getBoundingClientRect().top;
    view.scrollTop += now - mark.offset;
  }

  /* Relayout if the width the rows were justified for is no longer the
     width there is. The ResizeObserver calls it, and so does anything that
     changes the width itself (the scrubber appearing): a scrubber that came
     and went within one frame is invisible to the observer, which compares
     frame to frame, while the rows were laid out in between. */
  function fitWidth() {
    const w = body.clientWidth;
    if (w && Math.abs(w - width) > 1 && sections.length) { relayout(); checkMore(); }
  }

  function relayout(only) {
    const mark = capture();
    width = body.clientWidth;
    (only || sections).forEach(layoutSection);
    restore(mark);
  }

  /* The section at a scroll offset, by binary search on where each one
     starts. Sections are laid out in order, so offsetTop is monotonic. */
  function sectionAt(y) {
    let lo = 0;
    let hi = sections.length - 1;
    let hit = null;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (sections[mid].el.offsetTop <= y + 1) { hit = sections[mid]; lo = mid + 1; } else hi = mid - 1;
    }
    return hit || sections[0] || null;
  }

  // --- loading ---------------------------------------------------------------

  function params(extra) {
    const p = new URLSearchParams({ limit: String(PAGE) });
    if (loadedQ) p.set("q", loadedQ);
    Object.entries(extra || {}).forEach(([k, v]) => { if (v !== null && v !== undefined) p.set(k, String(v)); });
    return p;
  }

  /* The first page for the current query, replacing whatever is drawn. The old
     grid stays up until the new page arrives, so typing does not flash an
     empty screen between keystrokes. */
  async function load(opts = {}) {
    const mine = ++seq;
    const q = App.state.q;
    loading = true;
    pending = null;
    pendingUp = null;
    upError = null;
    paintEnd();
    let payload;
    try {
      const p = new URLSearchParams({ limit: String(PAGE) });
      if (q) p.set("q", q);
      if (opts.from) p.set("from", opts.from);
      payload = await App.api.get(`/api/photos?${p}`);
    } catch (err) {
      if (mine !== seq) return;
      loading = false;
      error = err;
      clear();
      next = null;
      prev = null;
      paintHead();
      paintTop();
      paintEnd();
      App.bus.emit("grid", info());
      return;
    }
    if (mine !== seq) return;
    loading = false;
    error = null;
    loadedQ = q;
    mode = payload.mode || "date";
    total = payload.total;
    query = payload.query || null;
    similarTo = payload.similar_to || null;
    face = payload.face || null;
    next = payload.next || null;
    prev = payload.prev || null;
    // A jump that landed at the top anyway is the plain listing.
    from = prev ? opts.from || null : null;
    dated = mode === "date" || App.query.get(q, "sort") === "date";
    const keepFocus = opts.keepFocus ? focusId : null;
    clear();
    view.scrollTop = 0;
    pill.hidden = true;
    width = body.clientWidth;
    paintHead();
    paintTop();
    append(payload.items || []);
    // The day jumped to at the top of the screen, the spinner for the days
    // above it just out of sight: the walk up starts at once, and scrolling
    // up finds them there.
    if (prev) view.scrollTop = body.offsetTop;
    if (keepFocus !== null && pos.has(keepFocus)) setFocus(keepFocus, false);
    paintEnd();
    App.bus.emit("grid", info());
    App.bus.emit("grid-position", { day: sections[0] ? sections[0].day : null, quiet: true });
    checkMore();
  }

  /* The next page. One request at a time; a second caller (the viewer
     stepping past the end while the scroll also asks) shares the first. */
  function more() {
    if (pending) return pending;
    if (!next || loading) return Promise.resolve(false);
    const mine = seq;
    const cursor = next;
    pending = (async () => {
      paintEnd(true);
      try {
        const payload = await App.api.get(`/api/photos?${params({ cursor })}`);
        if (mine !== seq) return false;
        next = payload.next || null;
        append(payload.items || []);
        App.bus.emit("grid-more", info());
        return true;
      } catch (err) {
        if (mine === seq) error = err;
        return false;
      } finally {
        if (mine === seq) {
          pending = null;
          paintEnd();
          setTimeout(checkMore, 0);
        }
      }
    })();
    return pending;
  }

  /* The page above, after a jump: the newer photos, laid in over the top
     with the picture at the top of the screen kept where it is. One request
     at a time, like more(), and independent of it. At the newest the listing
     is the plain one again, so `from` goes. */
  function above() {
    if (pendingUp) return pendingUp;
    if (!prev || loading || upError) return Promise.resolve(false);
    const mine = seq;
    const cursor = prev;
    pendingUp = (async () => {
      try {
        const payload = await App.api.get(`/api/photos?${params({ before: cursor })}`);
        if (mine !== seq) return false;
        const mark = capture();
        prev = payload.prev || null;
        if (!prev) from = null;
        prepend(payload.items || []);
        paintTop();
        restore(mark);
        App.bus.emit("grid-more", info());
        return true;
      } catch (err) {
        if (mine === seq) { upError = err; paintTop(); }
        return false;
      } finally {
        if (mine === seq) {
          pendingUp = null;
          setTimeout(checkMore, 0);
        }
      }
    })();
    return pendingUp;
  }

  /* IntersectionObserver only reports *changes*; a page too short to push the
     sentinel out of range would never ask for the next one. The same at the
     top, where a jump starts with the edge already in range. */
  function checkMore() {
    if (loading || view.offsetParent === null || error) return;
    const v = view.getBoundingClientRect();
    if (next && !pending && sentinel.getBoundingClientRect().top < v.bottom + 2000) more();
    if (prev && !pendingUp && !upError && topEl.getBoundingClientRect().bottom > v.top - 2000) above();
  }

  function info() {
    return { mode, total, query, similarTo, face, from, dated, error, count: items.length, q: loadedQ };
  }

  // --- the head and the foot -------------------------------------------------

  /* Above the first day after a jump, while there are newer photos still to
     fetch: where they will appear, or the way to ask again if they could not
     be. One height for both, so swapping them does not move the grid. */
  function paintTop() {
    topEl.hidden = !prev;
    if (!prev) { topEl.replaceChildren(); return; }
    if (upError) {
      topEl.replaceChildren(App.el("button", {
        class: "btn", text: "Load newer photos", title: upError.message,
        onclick: () => { upError = null; paintTop(); above(); },
      }));
      return;
    }
    topEl.replaceChildren(App.el("span", { class: "spinner" }));
  }

  function paintHead() {
    const nodes = [];
    if (similarTo || face) {
      const src = similarTo;
      const n = total === null || total === undefined ? items.length : total;
      // A face search shows the face it looks for, round, where the similar
      // banner shows the whole picture: that picture may hold several people.
      // Either one opens the picture the search started from.
      const pic = face ? face.url : src.thumb;
      const cls = [face ? "face" : null, src && veiled(src) ? "veiled" : null].filter(Boolean).join(" ");
      nodes.push(App.el("div", { class: "grid-banner" },
        pic ? App.el("img", {
          src: pic, alt: "", title: face ? "Open the picture this face is from" : "Open it", class: cls || null,
          onclick: () => (src ? App.viewer.open(src.id, { list: [src] }) : App.viewer.open(face.photo_id)),
        }) : null,
        App.el("div", { class: "banner-text" },
          App.el("span", { class: "banner-title", text: face ? "Pictures with this face" : `Similar to ${src.name}` }),
          App.el("span", { class: "banner-sub", text:
            `${App.fmt.plural(n, "photo")}${n >= 2000 ? " (the closest 2,000)" : ""}${src && src.taken ? ` · ${App.fmt.short(src.taken)}` : ""}` }),
        ),
        App.el("button", { class: "btn", text: "Back to all", onclick: () => App.shell.setQuery("", { push: true }) }),
      ));
    }
    head.replaceChildren(...nodes);
  }

  function emptyState() {
    const q = loadedQ;
    if (!q && !(App.state.counts && App.state.counts.all)) {
      return App.el("div", { class: "grid-empty" },
        App.el("img", { src: "/static/img/logo.png", alt: "" }),
        App.el("h2", { text: "No photos yet" }),
        App.el("p", { text: "The agent reads the library folder and indexes it newest first; pictures appear here as their thumbnails are made. The sidebar says how far it has got." }),
      );
    }
    const words = query && query.text;
    const lines = [];
    if (words && App.state.search && App.state.search.ready === false) {
      lines.push("The search model is still loading, so word searches find nothing yet. Filters work already.");
    } else if (words) {
      lines.push("No picture is close enough to those words. Try fewer or plainer ones, or a filter: the (i) beside the bar lists them.");
    } else {
      lines.push("Nothing matches these filters.");
    }
    return App.el("div", { class: "grid-empty" },
      App.el("h2", { text: "Nothing here" }),
      ...lines.map((t) => App.el("p", { text: t })),
      App.el("button", { class: "btn", text: "Show all photos", onclick: () => App.shell.setQuery("", { push: true }) }),
    );
  }

  function paintEnd(busy) {
    if (!endEl) return;
    if (error) {
      endEl.replaceChildren(App.el("div", { class: "grid-empty" },
        App.el("h2", { text: "The photos could not be loaded" }),
        App.el("p", { text: error.message }),
        // A page that failed half way down is asked for again; only a first
        // page that failed starts the listing over.
        App.el("button", { class: "btn", text: "Try again", onclick: () => {
          error = null;
          if (items.length && next) more(); else load({ from });
        } }),
      ));
      return;
    }
    if (loading || busy || pending) {
      endEl.replaceChildren(App.el("span", { class: "spinner" }));
      return;
    }
    if (!items.length) { endEl.replaceChildren(emptyState()); return; }
    if (next) { endEl.replaceChildren(); return; }
    endEl.replaceChildren(App.el("span", {
      text: mode === "score" && !dated ? "That is every close match." : "That is all of them.",
    }));
  }

  // --- selection -------------------------------------------------------------

  function card(id) { const i = pos.get(id); return i === undefined ? null : items[i]; }

  function selectedCards() { return items.filter((c) => selected.has(c.id)); }

  function paintOne(id) {
    const t = tiles.get(id);
    if (t) t.classList.toggle("selected", selected.has(id));
  }

  function paintDayChecks() {
    if (!dated) return;
    sections.forEach((sec) => {
      if (!sec.check) return;
      const all = sec.items.length > 0 && selected.size > 0 && sec.items.every((c) => selected.has(c.id));
      sec.check.classList.toggle("on", all);
      sec.check.title = all ? "Unselect this day" : "Select this day";
    });
  }

  function selButton(icon, label, run, disabled, cls = "") {
    return App.el("button", {
      class: `sel-btn ${cls}`.trim(), type: "button", title: label, disabled: Boolean(disabled), onclick: run,
    }, App.el("span", { html: App.icon(icon, 17) }), App.el("span", { class: "sel-label", text: label }));
  }

  function paintSelbar() {
    if (!selbar) return;
    view.classList.toggle("selecting", selected.size > 0);
    paintDayChecks();
    if (!selected.size) { selbar.hidden = true; selbar.replaceChildren(); return; }
    const cards = selectedCards();
    const one = cards.length === 1 ? cards[0] : null;
    const located = cards.filter((c) => c.lat !== null && c.lat !== undefined && c.lon !== null && c.lon !== undefined);
    // The browser's replaceChildren writes a null out as the text "null"
    // (App.el skips them, this does not): the buttons that are not offered go.
    selbar.replaceChildren(...[
      App.el("span", { class: "sel-count", text: `${App.fmt.n(cards.length)} selected` }),
      selButton("download", "Download", () => App.drag.downloadMany(cards)),
      selButton("copy", "Copy", () => App.drag.copy(one), !one),
      selButton("similar", "Similar", () => App.shell.similar(one.id), !one),
      App.state.map.tile_url ? selButton("map", "Show on map", () => App.shell.showOnMap(located), !located.length) : null,
      cards.some((c) => c.nsfw_flag)
        ? selButton("safe", "Mark safe", () => App.safe.mark(selectedCards().filter((c) => c.nsfw_flag), true)
          .then((answer) => { if (answer) clearSelection(); }))
        : null,
      App.state.delete ? selButton("trash", "Delete", () => App.trash.ask(selectedCards()), false, "danger") : null,
      selButton("close", "Clear", () => clearSelection()),
    ].filter(Boolean));
    selbar.hidden = false;
  }

  function toggle(id, on) {
    const want = on === undefined ? !selected.has(id) : on;
    if (want) selected.add(id); else selected.delete(id);
    anchor = id;
    paintOne(id);
    paintSelbar();
  }

  function selectRange(a, b) {
    const i = pos.get(a);
    const j = pos.get(b);
    if (i === undefined || j === undefined) { toggle(b, true); return; }
    for (let k = Math.min(i, j); k <= Math.max(i, j); k++) {
      selected.add(items[k].id);
      paintOne(items[k].id);
    }
    anchor = b;
    paintSelbar();
  }

  function selectDay(sec) {
    const all = sec.items.every((c) => selected.has(c.id));
    sec.items.forEach((c) => { if (all) selected.delete(c.id); else selected.add(c.id); paintOne(c.id); });
    paintSelbar();
  }

  function clearSelection() {
    if (!selected.size) return false;
    const was = [...selected];
    selected.clear();
    was.forEach(paintOne);
    anchor = null;
    paintSelbar();
    return true;
  }

  /* What a drag of `id` carries: the whole selection if the tile is part of
     it, otherwise just that tile. The same rule every file manager uses. */
  function cardsFor(id) {
    if (selected.has(id)) return selectedCards();
    const c = card(id);
    return c ? [c] : [];
  }

  // --- the keyboard's position ------------------------------------------------

  function ensureVisible(t) {
    const v = view.getBoundingClientRect();
    const r = t.getBoundingClientRect();
    const top = v.top + (dated ? headH : 0) + 4;
    if (r.top < top) view.scrollTop -= top - r.top;
    else if (r.bottom > v.bottom - 8) view.scrollTop += r.bottom - v.bottom + 8;
  }

  function setFocus(id, scroll = true) {
    if (focusId !== null) { const old = tiles.get(focusId); if (old) old.classList.remove("focused"); }
    focusId = id === undefined ? null : id;
    if (focusId === null) return;
    const t = tiles.get(focusId);
    if (!t) return;
    t.classList.add("focused");
    if (scroll) ensureVisible(t);
  }

  /* Up and down go to the picture in the next row nearest the one you are on,
     measured from the rows themselves: a justified grid has no columns. */
  function rowStep(t, dir) {
    let row = dir > 0 ? t.parentElement.nextElementSibling : t.parentElement.previousElementSibling;
    if (!row) {
      const sec = secOf.get(Number(t.dataset.id));
      const k = sections.indexOf(sec) + dir;
      if (k < 0 || k >= sections.length) return null;
      row = dir > 0 ? sections[k].rows.firstElementChild : sections[k].rows.lastElementChild;
    }
    if (!row) return null;
    const x = t.offsetLeft + t.offsetWidth / 2;
    let best = null;
    let dist = Infinity;
    for (const cand of row.children) {
      const d = Math.abs(cand.offsetLeft + cand.offsetWidth / 2 - x);
      if (d < dist) { dist = d; best = cand; }
    }
    return best ? Number(best.dataset.id) : null;
  }

  function move(dir) {
    if (!items.length) return;
    if (focusId === null || !pos.has(focusId)) { setFocus(firstVisible() ?? items[0].id); return; }
    const i = pos.get(focusId);
    let id = null;
    if (dir === "left") {
      id = i > 0 ? items[i - 1].id : null;
      if (id === null && prev) {
        const was = focusId;
        above().then(() => { const j = pos.get(was); if (focusId === was && j > 0) setFocus(items[j - 1].id); });
        return;
      }
    } else if (dir === "right") {
      id = i < items.length - 1 ? items[i + 1].id : null;
      if (id === null && next) { more().then(() => { if (pos.get(focusId) === i && items[i + 1]) setFocus(items[i + 1].id); }); return; }
    } else {
      const t = tiles.get(focusId);
      if (t) id = rowStep(t, dir === "down" ? 1 : -1);
    }
    if (id !== null) setFocus(id);
  }

  function key(e) {
    switch (e.key) {
      case "ArrowLeft": move("left"); return true;
      case "ArrowRight": move("right"); return true;
      case "ArrowUp": move("up"); return true;
      case "ArrowDown": move("down"); return true;
      case "Enter":
        if (focusId !== null) { App.viewer.open(focusId); return true; }
        return false;
      case "x":
        if (focusId !== null) { toggle(focusId); return true; }
        return false;
      case "Escape":
        if (clearSelection()) return true;
        if (focusId !== null) { setFocus(null); return true; }
        return false;
      default:
        return false;
    }
  }

  // --- the timeline's jumps -----------------------------------------------------

  /* A month ("2024-07", from the scrubber) or a year ("2024", from the
     sidebar). Scrolled to when it is already loaded, otherwise the listing
     restarts there: the listing is a walk down the timeline, so starting it at
     that month is the same as paging there, without fetching everything in
     between. The first day loaded, with newer ones still to come above it,
     may be only the end of the month, so that one restarts too. */
  function jump(prefix) {
    const sec = sections.find((s) => s.day && s.day.startsWith(prefix));
    if (sec && !loading && !(prev && sec === sections[0])) {
      view.scrollTop = sec.el.offsetTop;
      return;
    }
    if (mode !== "date") return;
    load({ from: prefix.length === 4 ? `${prefix}-12` : prefix });
  }

  // A day of 6,000 photos is the most "Show in all photos" pages through.
  const REVEAL_PAGES = 40;

  function centre(id) {
    const t = tiles.get(id);
    if (!t) return;
    const v = view.getBoundingClientRect();
    const r = t.getBoundingClientRect();
    view.scrollTop += r.top + r.height / 2 - (v.top + v.height / 2);
  }

  /* The viewer's "Show in all photos": one photo in the listing the shell
     has just set, scrolled to the middle of the screen and ringed. Already
     loaded, it is only scrolled to; otherwise the listing restarts at its
     day, the way the scrubber jumps to a month, and pages down to it. */
  async function reveal(c) {
    if (loading || loadedQ !== App.state.q || !pos.has(c.id)) {
      const started = load({ from: c.day || "undated" });
      const mine = seq;
      await started;
      for (let n = 0; n < REVEAL_PAGES && mine === seq && !pos.has(c.id) && next && !error; n++) {
        if (!(await more())) break;
      }
      if (mine !== seq) return;
    }
    if (!pos.has(c.id)) {
      if (!error) App.toast("That photo is not in All photos");
      return;
    }
    setFocus(c.id, false);
    centre(c.id);
    // Days off screen are drawn at an estimated height until they come into
    // view; once this one has been drawn for real, centred again.
    requestAnimationFrame(() => centre(c.id));
    const t = tiles.get(c.id);
    t.classList.remove("revealed");
    void t.offsetWidth;   // restart the pulse when it is shown twice running
    t.classList.add("revealed");
    t.addEventListener("animationend", () => t.classList.remove("revealed"), { once: true });
  }

  /* Home: the newest photo, top left. */
  function newest() {
    if (from) { load(); return; }
    view.scrollTop = 0;
    if (items.length && focusId !== null) setFocus(items[0].id, false);
  }

  function setSize(delta) {
    const now = Number(App.state.prefs.size) || SIZE.fallback;
    const size = Math.min(SIZE.max, Math.max(SIZE.min, now + delta * SIZE.step));
    if (size === now) return;
    App.state.prefs.size = size;
    App.load.prefs();
    relayout();
    checkMore();
  }

  // --- what the status poll says -----------------------------------------------

  /* The listing's own copy of its first pages, asked for again, and every
     changed card patched in place: a thumbnail that now exists, or dimensions
     the metadata stage corrected. The scroll position is kept. */
  async function refreshCards() {
    if (refreshing || !items.length) return;
    refreshing = true;
    const mine = seq;
    try {
      const p = new URLSearchParams({ limit: String(Math.min(500, items.length)) });
      if (loadedQ) p.set("q", loadedQ);
      if (from) p.set("from", from);
      const payload = await App.api.get(`/api/photos?${p}`);
      if (mine !== seq) return;
      const dirty = new Set();
      for (const fresh of payload.items || []) {
        const old = card(fresh.id);
        if (!old) continue;
        const resized = old.w !== fresh.w || old.h !== fresh.h;
        const same = ["thumb", "duration", "live", "favorite", "nsfw_flag", "nsfw_safe", "story", "preview"]
          .every((k) => old[k] === fresh[k]);
        if (same && !resized) continue;
        // Mutated rather than replaced: the sections hold these objects.
        Object.assign(old, fresh);
        const t = tiles.get(fresh.id);
        if (t) paintTile(t, old);
        if (resized && secOf.get(fresh.id)) dirty.add(secOf.get(fresh.id));
      }
      if (dirty.size) relayout([...dirty]);
    } catch (e) {
      /* the next poll tries again */
    } finally {
      refreshing = false;
    }
  }

  function atTop() { return view.scrollTop < 40; }

  /* The first page again, for photos that arrived while the grid sat at the
     top. At most every few seconds: during a sync the newest photo changes
     on every poll, and rebuilding the first screen that often is a flicker,
     not news. */
  const AUTO_GAP = 8000;
  let lastAuto = 0;
  let autoTimer = null;

  function autoReload() {
    const wait = lastAuto + AUTO_GAP - Date.now();
    if (wait > 0) {
      if (!autoTimer) {
        autoTimer = setTimeout(() => {
          autoTimer = null;
          if (atTop() && !loadedQ && !from && !selected.size && !App.viewer.isOpen()) autoReload();
        }, wait);
      }
      return;
    }
    lastAuto = Date.now();
    load({ keepFocus: true });
    App.bus.emit("library-changed");
  }

  function onStatus(status) {
    if (!status) return;
    // Placeholders: retried once the thumbnail backlog has visibly moved.
    const thumbs = status.index ? status.index.thumbs : null;
    if (lastThumbs !== null && thumbs !== null && thumbs < lastThumbs && items.some((c) => !c.thumb)) refreshCards();
    lastThumbs = thumbs;

    // New photos at the top. Only for the plain timeline: a search has its
    // own order, and a jump is somewhere else on purpose.
    const latest = status.latest;
    if (!latest || loadedQ || from || loading || error) return;
    if (items.length && latest.id === items[0].id) { pill.hidden = true; return; }
    if (!items.length && !latest.id) return;
    const busy = selected.size > 0 || App.viewer.isOpen() || App.state.view !== "grid";
    if (atTop() && !busy) {
      autoReload();
    } else {
      pill.hidden = App.state.view !== "grid";
    }
  }

  // --- events ----------------------------------------------------------------

  function onClick(e) {
    const dayBtn = e.target.closest(".day-check");
    if (dayBtn) {
      const sec = sections.find((s) => s.check === dayBtn);
      if (sec) selectDay(sec);
      return;
    }
    const t = e.target.closest(".tile");
    if (!t) return;
    const id = Number(t.dataset.id);
    if (e.target.closest(".tile-check")) {
      if (e.shiftKey && anchor !== null) selectRange(anchor, id); else toggle(id);
      return;
    }
    if (e.shiftKey && anchor !== null) { selectRange(anchor, id); return; }
    if (e.ctrlKey || e.metaKey) { toggle(id); return; }
    // While something is selected a click adds to it, as in every photo app:
    // opening the viewer mid-selection loses the thread.
    if (selected.size) { toggle(id); return; }
    setFocus(id, false);
    App.viewer.open(id);
  }

  function init() {
    view = document.getElementById("grid-view");
    body = document.getElementById("grid-body");
    head = document.getElementById("grid-head");
    topEl = document.getElementById("grid-top");
    sentinel = document.getElementById("grid-sentinel");
    endEl = document.getElementById("grid-end");
    pill = document.getElementById("new-pill");
    selbar = document.getElementById("selbar");

    body.addEventListener("click", onClick);
    // The desktop shell has to have the file on disk before a drag starts;
    // a pointerdown is the earliest honest moment to ask for it.
    body.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || !window.meerpicDesktop) return;
      const t = e.target.closest(".tile");
      if (t && !e.target.closest(".tile-check")) App.drag.prepare(cardsFor(Number(t.dataset.id)));
    });
    body.addEventListener("dragstart", (e) => {
      const t = e.target.closest(".tile");
      if (!t) return;
      App.hover.stop();
      App.drag.start(e, cardsFor(Number(t.dataset.id)));
    });
    // Video tiles play, or scrub their storyboard, under a resting mouse.
    App.hover.attach(body, (t) => card(Number(t.dataset.id)));

    pill.onclick = () => { pill.hidden = true; load(); App.bus.emit("library-changed"); };

    new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) more();
    }, { root: view, rootMargin: "0px 0px 2000px 0px" }).observe(sentinel);
    new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) above();
    }, { root: view, rootMargin: "2000px 0px 0px 0px" }).observe(topEl);

    // Width changes (the window, the drawer breakpoint, the viewer's info
    // panel never: it is an overlay) relayout every day, keeping the picture
    // at the top of the screen where it was.
    let raf = 0;
    new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(fitWidth);
    }).observe(view);

    let ticking = false;
    view.addEventListener("scroll", () => {
      // The tile under a still pointer changes as the page moves.
      App.hover.stop();
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        const sec = sectionAt(view.scrollTop);
        App.bus.emit("grid-position", { day: sec ? sec.day : null, quiet: false });
        if (atTop() && !pill.hidden) { pill.hidden = true; load(); }
      });
    }, { passive: true });
  }

  return {
    init, load, more, above, jump, reveal, newest, setSize, key, onStatus, relayout, checkMore, fitWidth, remove,
    refresh: () => refreshCards(),
    items: () => items,
    card,
    indexOf: (id) => (pos.has(id) ? pos.get(id) : -1),
    hasMore: () => Boolean(next),
    hasAbove: () => Boolean(prev),
    focused: () => (focusId === null ? null : card(focusId)),
    setFocus,
    selection: selectedCards,
    clearSelection,
    info,
    isDated: () => dated,
  };
})();
