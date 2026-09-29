/* The shell: the sidebar, the toolbar, which view is up, and the address bar.

   The address carries the whole of what is on screen: ?q= (the search bar's
   text), ?view=map, ?photo= (the open photo). Typing replaces the history
   entry, so a search does not leave thirty entries behind it; a deliberate
   move (a sidebar item, Find similar, Show in grid, opening a photo) pushes
   one, so Back goes back to where you were.

   The sidebar is a set of ready-made queries rather than modes: Videos writes
   `is:video` into the bar, a place writes `in:"Potsdam"`. Seeing the query
   appear is how the reader learns that the bar can say more. */

window.App = window.App || {};

App.shell = (() => {
  // `when` leaves a row out while it would lead nowhere.
  const NAV = [
    { q: "", label: "Photos", icon: "photo", count: "all", title: "Everything, newest first (g p)" },
    { q: "is:favorite", label: "Favorites", icon: "heart", count: "favorites", title: "is:favorite: hearted in iCloud Photos (g f)" },
    { q: "is:video", label: "Videos", icon: "video", count: "videos", title: "is:video (g v)" },
    { q: "is:live", label: "Live Photos", icon: "live", count: "live", title: "is:live (g l)" },
    { q: "is:screenshot", label: "Screenshots", icon: "screenshot", count: "screenshots", title: "is:screenshot (g s)" },
    { q: "is:whatsapp", label: "WhatsApp", icon: "chat", count: "whatsapp",
      title: "is:whatsapp: the WhatsApp album", when: (c) => Number(c.whatsapp) > 0 },
    { q: "is:saved", label: "Saved from apps", icon: "inbox", count: "saved",
      title: "is:saved: pictures an app or a browser saved, rather than a camera took" },
    { q: "is:nsfw", label: "Possibly NSFW", icon: "eyeOff", count: "nsfw",
      title: "is:nsfw: flagged by the image classifier, most certain first", when: () => Boolean(App.state.nsfw && App.state.nsfw.enabled) },
  ];

  // Titles for filters that are a place of their own but not a sidebar row.
  const TITLES = { "is:hidden": "Hidden", "is:deleted": "Recently Deleted", "is:edited": "Edited" };

  let places = [];
  let years = [];
  let albums = [];
  // An albums refresh asked for here or seen running: the link spins. `albumJob`
  // is the job this window started ("asking" while the request is out).
  let albumsBusy = false;
  let albumJob = null;
  let lastRefresh = 0;
  let lastTotal = null;
  let lastLatest = null;

  const located = (c) => c && c.lat !== null && c.lat !== undefined && c.lon !== null && c.lon !== undefined;

  // --- the address bar ------------------------------------------------------------

  function readUrl() {
    const p = new URLSearchParams(window.location.search);
    return {
      q: (p.get("q") || "").trim(),
      view: p.get("view") === "map" ? "map" : "grid",
      photo: Number(p.get("photo")) || null,
    };
  }

  function urlFor() {
    const p = new URLSearchParams();
    if (App.state.q) p.set("q", App.state.q);
    if (App.state.view === "map") p.set("view", "map");
    const id = App.viewer.currentId();
    if (id) p.set("photo", String(id));
    const s = p.toString();
    return `${window.location.pathname}${s ? `?${s}` : ""}`;
  }

  /* Write the address from the state. Returns whether it added an entry, so
     the viewer knows whether closing has one to undo. */
  function writeUrl(push) {
    const url = urlFor();
    const now = `${window.location.pathname}${window.location.search}`;
    if (url === now) return false;
    if (push) { history.pushState(null, "", url); return true; }
    history.replaceState(null, "", url);
    return false;
  }

  function onPop() {
    const u = readUrl();
    if (u.q !== App.state.q) setQuery(u.q, { fromHistory: true });
    if (u.view !== App.state.view) applyView(u.view);
    if (u.photo && u.photo !== App.viewer.currentId()) App.viewer.open(u.photo, { fromUrl: true });
    else if (!u.photo && App.viewer.isOpen()) App.viewer.close(true);
  }

  // --- the query and the view -----------------------------------------------------

  /* The one way the query changes. Everything that filters (the bar, the
     sidebar, the map, the viewer's buttons) comes through here. */
  function setQuery(q, opts = {}) {
    q = (q || "").trim();
    const changed = q !== App.state.q;
    if (App.viewer.isOpen() && !opts.fromHistory) App.viewer.close(true);
    App.state.q = q;
    App.search.set(q);
    if (opts.view && opts.view !== App.state.view) applyView(opts.view);
    else closeDrawer();
    // `reveal`: a photo the grid is to load its way to and ring (showInAll).
    if (opts.reveal) App.grid.reveal(opts.reveal);
    else if (changed || opts.force) App.grid.load();
    if (changed || opts.force) {
      App.timeline.load();
      if (App.map.isShown()) App.map.refresh();
    }
    renderNav();
    paintTitle();
    if (!opts.fromHistory) writeUrl(Boolean(opts.push) && (changed || Boolean(opts.view)));
  }

  function setView(view, opts = {}) {
    if (view === "map" && !App.map.available()) view = "grid";
    if (App.viewer.isOpen()) App.viewer.close(true);
    const changed = view !== App.state.view;
    applyView(view);
    if (changed) writeUrl(Boolean(opts.push));
  }

  /* The map lies over the grid rather than replacing it, so the grid keeps its
     scroll position, its loaded pages and its selection while the map is up;
     `inert` keeps the keyboard and the pointer out of it meanwhile. */
  function applyView(view) {
    App.state.view = view;
    const grid = document.getElementById("grid-view");
    grid.inert = view !== "grid";
    document.getElementById("stage").classList.toggle("on-map", view === "map");
    if (view === "map") App.map.show(); else App.map.hide();
    document.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    App.timeline.render();
    renderNav();
    paintTitle();
    closeDrawer();
    if (view === "grid") {
      App.grid.checkMore();
      grid.focus({ preventScroll: true });
    }
  }

  /* The photo the keys act on: the open one, else the keyboard's, else the
     one selected. */
  function target() {
    const open = App.viewer.current();
    if (open) return open;
    if (App.state.view !== "grid") return null;
    const focused = App.grid.focused();
    if (focused) return focused;
    const sel = App.grid.selection();
    return sel.length === 1 ? sel[0] : null;
  }

  function similar(id) {
    if (!id) return;
    setQuery(`similar:${id}`, { push: true, view: "grid" });
  }

  /* One photo in the plain timeline: out of the search, the album or the
     map it was found in, with the grid scrolled to it and the ring on it.
     Hidden and Recently Deleted are not in the timeline, so for those there
     is nowhere to go. */
  function showInAll(c) {
    if (!c) return;
    if (c.hidden || c.icloud_deleted) {
      App.toast(`It is in ${c.hidden ? "Hidden" : "Recently Deleted"}, which All photos leaves out`);
      return;
    }
    setQuery("", { push: true, view: "grid", reveal: c });
  }

  /* Photos on the map. One photo from the viewer (or `m`) asks "what else was
     taken here": the query becomes `near:` that spot, half a kilometre round,
     and the map goes to street level. A selection is framed as it is. */
  function showOnMap(cards, opts = {}) {
    if (!App.map.available()) return;
    const pts = (cards || []).filter(located).map((c) => [c.lat, c.lon]);
    if (!pts.length) { App.toast("That photo has no location"); return; }
    if (opts.near && pts.length === 1) {
      const [lat, lon] = pts[0];
      setQuery(`near:${lat.toFixed(5)},${lon.toFixed(5)},0.5`, { push: true, view: "map" });
      App.map.focus(pts, 16);
      return;
    }
    setView("map", { push: true });
    App.map.focus(pts);
  }

  // --- the toolbar ----------------------------------------------------------------

  function titleFor(q) {
    const nav = NAV.find((n) => n.q === q);
    if (nav) return nav.label;
    if (TITLES[q]) return TITLES[q];
    if (App.query.get(q, "similar")) return "Similar";
    if (App.query.get(q, "face")) return "Same face";
    const tokens = App.query.tokens(q);
    if (tokens.length === 1 && (tokens[0].key === "in" || tokens[0].key === "album")) return tokens[0].value;
    if (tokens.length === 1 && tokens[0].key === "year") return tokens[0].value;
    if (tokens.length === 1 && tokens[0].key === "near") return "Nearby";
    if (tokens.length === 1 && tokens[0].key === "bbox") return "Area";
    return App.query.words(q) ? "Search" : "Filtered";
  }

  function paintTitle() {
    const el = document.getElementById("view-title");
    const note = document.getElementById("count-note");
    if (App.state.view === "map") {
      el.textContent = App.state.q ? `Map · ${titleFor(App.state.q)}` : "Map";
      note.hidden = true;
      return;
    }
    el.textContent = titleFor(App.state.q);
    const info = App.grid.info();
    const n = info.q === App.state.q && info.total !== null && info.total !== undefined ? info.total : null;
    note.hidden = n === null || Boolean(info.error);
    if (n !== null) {
      note.textContent = info.mode === "score" ? App.fmt.plural(n, "match", "matches") : App.fmt.n(n);
      note.title = info.mode === "score" ? "Close enough to count, best first" : "Photos and videos";
    }
  }

  // --- the sidebar ------------------------------------------------------------------

  function navRow({ label, icon, count, title, active, run }) {
    return App.el("button", { class: `nav-row${active ? " active" : ""}`, type: "button", title: title || null, onclick: run },
      icon ? App.el("span", { class: "nav-icon", html: App.icon(icon, 16) }) : null,
      App.el("span", { class: "nav-name", text: label }),
      count !== null && count !== undefined ? App.el("span", { class: "nav-count", text: App.fmt.n(count) }) : null,
    );
  }

  function placeQuery(name) { return `in:${App.query.quote(name)}`; }

  /* The Albums heading's own control: read the albums (and the hearts, and
     Hidden, and Recently Deleted) from iCloud again, now. */
  function refreshLink() {
    return App.el("button", {
      class: `tree-link${albumsBusy ? " busy" : ""}`, type: "button", disabled: albumsBusy,
      title: albumsBusy ? "Reading the albums from iCloud" : "Read the albums, favorites and hidden photos from iCloud again",
      onclick: () => refreshAlbums(),
    },
      albumsBusy ? App.el("span", { class: "spinner tiny" }) : null,
      App.el("span", { text: albumsBusy ? "Refreshing" : "Refresh from iCloud" }));
  }

  function albumRows(nodes, counts, grid) {
    // WhatsApp has a row of its own above; the same list twice is noise.
    const whatsapp = Number(counts.whatsapp) > 0;
    const shown = albums.filter((a) => a.kind === "user" && a.count > 0
      && !(whatsapp && String(a.name).toLowerCase() === "whatsapp"));
    const sync = Boolean(App.state.sync && App.state.sync.enabled);
    if (!shown.length && !sync) return;
    nodes.push(App.el("div", { class: "tree-section" }, App.el("span", { text: "Albums" }), sync ? refreshLink() : null));
    shown.forEach((a) => {
      const q = `album:${App.query.quote(a.name)}`;
      const missing = Number(a.remote_count) - Number(a.count);
      nodes.push(navRow({
        label: a.name, icon: "album", count: a.count,
        title: `${a.name}\n${q}${missing > 0 ? `\n${App.fmt.plural(missing, "more item")} in iCloud than here` : ""}`,
        active: grid && App.state.q === q,
        run: () => setQuery(q, { push: true, view: "grid" }),
      }));
    });
    if (!shown.length) {
      nodes.push(App.el("div", { class: "nav-empty", text: albumsBusy ? "Reading the albums..." : "No albums read yet" }));
    }
  }

  function renderNav() {
    const tree = document.getElementById("nav-tree");
    if (!tree) return;
    const counts = App.state.counts || {};
    const grid = App.state.view === "grid";
    const nodes = NAV.filter((n) => !n.when || n.when(counts)).map((n) => navRow({
      label: n.label, icon: n.icon, title: n.title, count: counts[n.count],
      active: grid && App.state.q === n.q,
      run: () => setQuery(n.q, { push: true, view: "grid" }),
    }));
    if (App.map.available()) {
      nodes.push(navRow({
        label: "Map", icon: "map", count: counts.located, title: "Where they were taken (M)",
        active: !grid, run: () => setView("map", { push: true }),
      }));
    }

    albumRows(nodes, counts, grid);

    if (places.length) {
      nodes.push(App.el("div", { class: "tree-section", text: "Places" }));
      places.forEach((p) => {
        const q = placeQuery(p.label);
        nodes.push(navRow({
          label: p.label, icon: "pin", count: p.count, title: `${p.place}\n${q}`,
          active: grid && App.state.q === q,
          run: () => setQuery(q, { push: true, view: "grid" }),
        }));
      });
    }

    if (years.length) {
      nodes.push(App.el("div", { class: "tree-section", text: "Years" }));
      years.forEach((y) => nodes.push(navRow({
        label: y.year, icon: "calendar", count: y.count, title: `Go to ${y.year}`,
        active: false, run: () => jumpYear(y.year),
      })));
    }
    tree.replaceChildren(...nodes);
  }

  /* A year in the sidebar: a jump in a date-ordered grid, and in a relevance-
     ordered one (where "scroll to 2021" means nothing) a `year:` filter. */
  function jumpYear(year) {
    if (App.viewer.isOpen()) App.viewer.close(true);
    if (App.state.view !== "grid") setView("grid", { push: true });
    closeDrawer();
    if (App.grid.info().mode === "date") App.grid.jump(year);
    else setQuery(App.query.set(App.state.q, "year", year), { push: true });
  }

  async function loadSidebar() {
    const [p, t, a] = await Promise.all([
      App.api.get("/api/places?limit=8").catch(() => null),
      App.api.get("/api/timeline").catch(() => null),
      App.api.get("/api/albums").catch(() => null),
    ]);
    if (a) albums = a.albums || [];
    if (p) {
      const raw = p.places || [];
      const names = raw.map((x) => x.city || x.region || x.country || x.place);
      places = raw.map((x, i) => ({
        ...x, label: names.filter((n) => n === names[i]).length > 1 ? x.place : names[i],
      })).filter((x) => x.label);
    }
    if (t) {
      const byYear = new Map();
      (t.months || []).forEach((m) => {
        const y = m.month.slice(0, 4);
        byYear.set(y, (byYear.get(y) || 0) + m.count);
      });
      years = [...byYear.entries()].map(([year, count]) => ({ year, count }));
    }
    renderNav();
  }

  /* The library grew or shrank: the counts, places and years are refetched,
     at most every half minute however often the status poll notices. */
  async function refreshLibrary(force) {
    const now = Date.now();
    if (!force && now - lastRefresh < 30000) return;
    lastRefresh = now;
    try { await App.load.state(); } catch (e) { return; }
    await loadSidebar();
    App.timeline.load();
    paintTitle();
  }

  // --- albums, from iCloud ------------------------------------------------------------

  /* One refresh at a time: the server hands back the one already queued or
     running rather than starting another, as it does for a sync. */
  async function refreshAlbums() {
    if (albumsBusy) return;
    albumsBusy = true;
    albumJob = "asking";
    renderNav();
    try {
      const answer = await App.api.post("/api/albums/refresh");
      albumJob = answer && answer.job ? answer.job.id : null;
    } catch (err) {
      albumsBusy = false;
      albumJob = null;
      renderNav();
      App.toast(err.message, { error: true });
      return;
    }
    App.status.poll();
  }

  /* Spin while any albums job runs (the agent starts its own after every
     sync); when it ends, the albums, hearts and counts are read again. */
  function watchAlbums(s) {
    if (albumJob === "asking") return;
    const jobs = (s.jobs || []).filter((j) => j.kind === "albums");
    const running = jobs.some((j) => j.status === "queued" || j.status === "running");
    if (running) {
      if (!albumsBusy) { albumsBusy = true; renderNav(); }
      return;
    }
    if (!albumsBusy) return;
    const job = albumJob !== null ? jobs.find((j) => j.id === albumJob) : null;
    albumsBusy = false;
    albumJob = null;
    if (job && job.status === "failed") {
      App.toast(`The albums could not be read: ${job.error || job.message || "rclone failed"}`, { error: true, ms: 9000 });
    }
    // Hearts and Hidden may have changed under the tiles on screen.
    refreshLibrary(true);
    App.grid.refresh();
  }

  function onStatus(s) {
    App.grid.onStatus(s);
    watchAlbums(s);
    const total = s.index ? s.index.total : null;
    const latest = s.latest ? s.latest.id : null;
    if ((lastTotal !== null && total !== lastTotal) || (lastLatest !== null && latest !== lastLatest)) refreshLibrary();
    lastTotal = total;
    lastLatest = latest;
  }

  // --- the narrow-layout drawer -----------------------------------------------------

  function closeDrawer() {
    document.body.classList.remove("drawer-open");
    document.getElementById("scrim").hidden = true;
  }

  function toggleDrawer() {
    const open = document.body.classList.toggle("drawer-open");
    // `hidden` comes off first so the fade has something to fade.
    document.getElementById("scrim").hidden = !open;
  }

  // --- start ----------------------------------------------------------------------

  async function init() {
    App.paintIcons();
    await App.load.state();
    document.getElementById("brand").title = App.state.version ? `meerpic ${App.state.version}` : "meerpic";

    const u = readUrl();
    App.state.q = u.q;
    App.state.view = u.view === "map" && App.map.available() ? "map" : "grid";
    // No tile server configured, no map: the switch would lead nowhere.
    document.getElementById("view-switch").hidden = !App.map.available();

    App.search.init();
    App.search.set(App.state.q);
    App.grid.init();
    App.timeline.init();
    App.viewer.init();
    App.status.init();
    App.trash.init();

    document.getElementById("btn-theme").onclick = () => {
      const m = App.theme.cycle();
      App.toast(m === "system" ? "Theme: follow the system" : `Theme: ${m}`);
    };
    document.getElementById("btn-menu").onclick = toggleDrawer;
    document.getElementById("scrim").onclick = closeDrawer;
    document.getElementById("btn-smaller").onclick = () => App.grid.setSize(-1);
    document.getElementById("btn-larger").onclick = () => App.grid.setSize(1);
    document.querySelectorAll("[data-view]").forEach((b) => {
      b.onclick = () => setView(b.dataset.view, { push: true });
    });

    applyView(App.state.view);
    // The address may have asked for something this server cannot show (a
    // map without a tile server); say what is actually on screen.
    writeUrl(false);
    renderNav();
    App.grid.load();
    App.timeline.load();
    loadSidebar();
    App.keys.init();

    App.bus.on("grid", () => paintTitle());
    App.bus.on("status", onStatus);
    App.bus.on("library-changed", () => refreshLibrary());

    // Nothing polls while the window is behind another one; on the way back
    // the status is asked for again rather than believed.
    App.power.init();
    App.power.whenSuspended(() => App.status.stop());
    App.power.whenResumed(() => { App.status.start(); refreshLibrary(true); });
    App.status.start();

    window.addEventListener("popstate", onPop);
    if (u.photo) App.viewer.open(u.photo, { fromUrl: true });
    watchSearchModel();
  }

  /* The text model loads in the background after the server starts, and
     until it has, word searches find nothing. Asked again every few seconds
     until it is ready; then a search that was waiting for it runs again. */
  function watchSearchModel() {
    if (!App.state.search || App.state.search.ready !== false) return;
    const timer = setInterval(async () => {
      try { await App.load.state(); } catch (e) { return; }
      if (App.state.search.ready === false) return;
      clearInterval(timer);
      if (App.query.words(App.state.q)) setQuery(App.state.q, { force: true });
    }, 5000);
  }

  return {
    init, setQuery, setView, writeUrl, target, similar, showInAll, showOnMap, closeDrawer, renderNav, paintTitle,
    refresh: (force) => refreshLibrary(force),
  };
})();
