/* The map: where the pictures were taken.

   Leaflet (vendored, see static/vendor/leaflet) draws the tiles; everything on
   top of them comes from /api/map/clusters, which groups the located photos in
   the visible box into a grid of cells sized for the zoom level and returns one
   marker per cell: how many, where their middle is, the box they span, and the
   newest one's thumbnail. The browser never holds the whole library's
   coordinates, which is what lets this stay fast at 25,000 photos.

   A marker is that thumbnail, round, with the count on it. A click zooms to
   the cell's box; a single photo, or a cell that will not come apart at the
   deepest zoom, opens instead. The bar under the map says how many photos are
   in view and turns the view into a `bbox:` filter for the grid.

   The current search applies here too: the map of `is:video` is where the
   videos were taken, and the map of "beach" is where the beaches were.

   The tile server's attribution is drawn by this module rather than by
   Leaflet's own control, which would set it as markup: it arrives from the
   server's config, and nothing from the server becomes markup here. */

window.App = window.App || {};

App.map = (() => {
  const DEBOUNCE = 250;
  const VIEW_KEY = "map.view";

  let el = null;
  let canvas = null;
  let barEl = null;
  let noteEl = null;
  let emptyEl = null;
  let map = null;
  let layer = null;
  let timer = null;
  let seq = 0;
  let total = 0;
  let fitted = false;
  let pendingFocus = null;

  let mini = null;          // the viewer's small map
  let miniMarker = null;

  const available = () => Boolean(window.L && App.state.map && App.state.map.tile_url);
  const maxZoom = () => Number(App.state.map.max_zoom) || 19;

  /* The attribution, from a string of markup to safe nodes: text stays text,
     and a link survives only as a link to http(s). DOMParser documents are
     inert, so nothing in the string runs or loads while it is read. */
  function attribution() {
    const raw = (App.state.map && App.state.map.attribution) || "";
    const doc = new DOMParser().parseFromString(`<div>${raw}</div>`, "text/html");
    const out = App.el("span", { class: "map-attrib" });
    const walk = (node, into) => {
      node.childNodes.forEach((child) => {
        if (child.nodeType === Node.TEXT_NODE) { into.append(child.textContent); return; }
        if (child.nodeType !== Node.ELEMENT_NODE) return;
        const href = child.tagName === "A" ? child.getAttribute("href") || "" : "";
        if (/^https?:\/\//i.test(href)) {
          const a = App.el("a", { href, target: "_blank", rel: "noopener noreferrer" });
          walk(child, a);
          into.append(a);
        } else walk(child, into);
      });
    };
    walk(doc.body.firstChild || doc.body, out);
    return out;
  }

  function tiles() {
    return L.tileLayer(App.state.map.tile_url, { maxZoom: maxZoom(), className: "map-tiles" });
  }

  // --- the big map --------------------------------------------------------------

  function build() {
    el = document.getElementById("map-view");
    canvas = App.el("div", { class: "map-canvas" });
    noteEl = App.el("span", { class: "map-note" });
    emptyEl = App.el("div", { class: "map-empty", hidden: true });
    barEl = App.el("div", { class: "map-bar" },
      noteEl,
      App.el("button", { class: "btn", type: "button", text: "Show in grid", title: "Show the photos in this area in the grid", onclick: () => showInGrid() }),
      App.el("span", { class: "grow" }),
      attribution(),
    );
    el.replaceChildren(canvas, barEl);
    canvas.append(emptyEl);

    const saved = App.store.get(VIEW_KEY, null);
    map = L.map(canvas, {
      zoomControl: true,
      attributionControl: false,
      worldCopyJump: true,
      minZoom: 2,
      maxZoom: maxZoom(),
    });
    tiles().addTo(map);
    layer = L.layerGroup().addTo(map);
    if (saved && Number.isFinite(saved.lat) && Number.isFinite(saved.lon)) {
      map.setView([saved.lat, saved.lon], saved.zoom || 5);
      fitted = true;
    } else {
      map.setView([30, 10], 2);
    }
    map.on("moveend", () => {
      const c = map.getCenter();
      App.store.set(VIEW_KEY, { lat: c.lat, lon: c.lng, zoom: map.getZoom() });
      schedule();
    });
  }

  /* The visible box, in the terms the API takes: longitudes wrapped into
     -180..180 (Leaflet's run on past them as the world repeats), the whole
     world when the view is wider than it, and w > e when the box crosses the
     antimeridian, which the server handles. */
  function bbox() {
    const b = map.getBounds();
    const s = Math.max(-90, b.getSouth());
    const n = Math.min(90, b.getNorth());
    let w = b.getWest();
    let e = b.getEast();
    if (e - w >= 360) { w = -180; e = 180; } else {
      const wrap = (x) => ((((x + 180) % 360) + 360) % 360) - 180;
      w = wrap(w);
      e = wrap(e);
      if (e === -180) e = 180;
    }
    return [w, s, e, n];
  }

  const round4 = (x) => Math.round(x * 10000) / 10000;

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, DEBOUNCE);
  }

  async function refresh() {
    if (!map || el.hidden) return;
    const mine = ++seq;
    const box = bbox();
    const p = new URLSearchParams({ bbox: box.map((x) => x.toFixed(6)).join(","), zoom: String(map.getZoom()) });
    if (App.state.q) p.set("q", App.state.q);
    let payload;
    try {
      payload = await App.api.get(`/api/map/clusters?${p}`);
    } catch (err) {
      if (mine !== seq) return;
      noteEl.textContent = `The map could not be loaded: ${err.message}`;
      return;
    }
    if (mine !== seq) return;
    total = payload.total || 0;
    draw(payload.clusters || []);
    noteEl.textContent = `${App.fmt.plural(total, "photo")} in this area`;
    emptyEl.hidden = total > 0;
    emptyEl.textContent = App.state.q ? "No located photos match here" : "No located photos here";
    // First visit with nothing remembered: frame wherever the photos are.
    if (!fitted && payload.clusters && payload.clusters.length) {
      fitted = true;
      const pts = payload.clusters.map((c) => [c.lat, c.lon]);
      map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 12 });
    }
  }

  function markerFor(c) {
    const size = c.count > 1 ? Math.round(46 + Math.min(18, Math.log10(c.count) * 8)) : 40;
    const node = App.el("div", { class: "cl", title: c.count > 1 ? App.fmt.plural(c.count, "photo") : "Open it" },
      c.thumb ? App.el("img", { src: c.thumb, alt: "", draggable: "false", loading: "lazy" }) : null,
      c.count > 1 ? App.el("span", { class: "cl-n", text: c.count > 999 ? `${Math.round(c.count / 100) / 10}k` : String(c.count) }) : null,
    );
    const icon = L.divIcon({ className: "cl-icon", html: node, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
    const marker = L.marker([c.lat, c.lon], { icon, keyboard: false, riseOnHover: true });
    marker.on("click", () => clicked(c));
    return marker;
  }

  function draw(clusters) {
    layer.clearLayers();
    // Smallest last, so a single photo is never buried under a crowd.
    clusters.slice().sort((a, b) => b.count - a.count).forEach((c) => layer.addLayer(markerFor(c)));
  }

  function clicked(c) {
    if (c.count === 1 && c.id) { App.viewer.open(c.id); return; }
    const [w, s, e, n] = c.bbox || [c.lon, c.lat, c.lon, c.lat];
    const flat = Math.abs(e - w) < 1e-6 && Math.abs(n - s) < 1e-6;
    if (flat || map.getZoom() >= maxZoom()) { showInGrid([w, s, e, n]); return; }
    const before = map.getZoom();
    const bounds = L.latLngBounds([[s, w], [n, e]]);
    const zoom = Math.min(maxZoom(), map.getBoundsZoom(bounds.pad(0.15)));
    // A cell that would not come apart any further is as open as it gets.
    if (zoom <= before) { showInGrid([w, s, e, n]); return; }
    map.fitBounds(bounds, { padding: [50, 50], maxZoom: maxZoom() });
  }

  /* The view (or one cluster's box) as a `bbox:` filter, into the grid. */
  function showInGrid(box) {
    const b = (box || bbox()).map(round4);
    // A single point has no area; pad it to a few metres so it matches itself.
    if (b[0] === b[2]) { b[0] = round4(b[0] - 0.0001); b[2] = round4(b[2] + 0.0001); }
    if (b[1] === b[3]) { b[1] = round4(b[1] - 0.0001); b[3] = round4(b[3] + 0.0001); }
    App.shell.setQuery(App.query.set(App.state.q, "bbox", b.join(",")), { push: true, view: "grid" });
  }

  // --- showing and focusing -----------------------------------------------------

  function applyFocus() {
    if (!pendingFocus || !map) return;
    const { points, zoom } = pendingFocus;
    pendingFocus = null;
    fitted = true;
    if (points.length === 1) map.setView(points[0], zoom || 16);
    else map.fitBounds(L.latLngBounds(points), { padding: [60, 60], maxZoom: zoom || 16 });
  }

  function show() {
    if (!available()) return;
    if (!map) build();
    el.hidden = false;
    // Leaflet measured a hidden element as 0 by 0; tell it the real size
    // before it works out which tiles to fetch, and only then go anywhere.
    requestAnimationFrame(() => {
      map.invalidateSize();
      applyFocus();
      refresh();
    });
  }

  function hide() {
    if (el) el.hidden = true;
    clearTimeout(timer);
  }

  /* Put some photos in view: one at street level, several framed together.
     Applied on the next frame, after `show` has told Leaflet its size. */
  function focus(points, zoom) {
    if (!points || !points.length) return;
    pendingFocus = { points, zoom };
    if (map && !el.hidden) requestAnimationFrame(() => { map.invalidateSize(); applyFocus(); });
  }

  // --- the viewer's small map ----------------------------------------------------

  function miniMap(container, lat, lon) {
    if (!available()) return;
    if (!mini || mini.getContainer() !== container) {
      if (mini) mini.remove();
      mini = L.map(container, {
        zoomControl: false, attributionControl: false, dragging: true, scrollWheelZoom: false,
        doubleClickZoom: true, boxZoom: false, keyboard: false, minZoom: 2, maxZoom: maxZoom(),
      });
      tiles().addTo(mini);
      miniMarker = L.marker([lat, lon], {
        icon: L.divIcon({ className: "cl-icon", html: App.el("div", { class: "mini-dot" }), iconSize: [14, 14], iconAnchor: [7, 7] }),
        keyboard: false, interactive: false,
      }).addTo(mini);
    }
    miniMarker.setLatLng([lat, lon]);
    mini.setView([lat, lon], 13, { animate: false });
    requestAnimationFrame(() => mini.invalidateSize());
  }

  return {
    show, hide, focus, refresh: () => schedule(), mini: miniMap, available,
    isShown: () => Boolean(el && !el.hidden),
  };
})();
