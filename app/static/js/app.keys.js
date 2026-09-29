/* Keyboard. Every view-level thing has a key, and the cheat sheet in the
   sidebar is generated from the same table that binds them, so a shortcut
   cannot drift out of step with its own documentation.

   Most keys act on "the photo in question": the one open in the viewer, else
   the one the keyboard focus ring is on, else the one selected tile (see
   App.shell.target). So `c` copies the picture you are looking at, whether
   you are looking at it full-screen or in the grid. */

window.App = window.App || {};

App.keys = (() => {
  const target = () => App.shell.target();

  // Order is the cheat sheet's order, and the sheet shows the first nine until
  // it is opened, so the everyday ones lead: find it, move to it, get it out.
  const BINDINGS = [
    { key: "/", label: "Search", run: () => App.search.focus() },
    { key: "←↑↓→", label: "Move", grid: true,
      match: (e) => ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key),
      run: (e) => App.grid.key(e) },
    { key: "Enter", label: "Open", grid: true, run: (e) => App.grid.key(e) },
    { key: "c", label: "Copy", run: () => App.drag.copy(target()) },
    { key: "d", label: "Download", run: () => App.drag.download(target()) },
    { key: "s", label: "Similar", run: () => { const t = target(); if (t) App.shell.similar(t.id); } },
    { key: "M", label: "Map view", run: () => App.shell.setView(App.state.view === "map" ? "grid" : "map", { push: true }) },
    { key: "x", label: "Select", grid: true, run: (e) => App.grid.key(e) },
    { key: ".", label: "Sync", run: () => App.status.sync() },
    { key: "D", label: "Download original", run: () => App.drag.download(target(), true) },
    { key: "i", label: "Info", run: () => {
      if (App.viewer.isOpen()) { App.viewer.toggleInfo(); return; }
      const t = target();
      if (t) { App.store.set("viewer.info", true); App.viewer.open(t.id); }
    } },
    { key: "m", label: "Show on map", run: () => { const t = target(); if (t) App.shell.showOnMap([t], { near: true }); } },
    { key: "a", label: "Show in all photos", run: () => {
      if (App.viewer.isOpen()) App.viewer.showInAll(); else App.shell.showInAll(target());
    } },
    // A second press takes the mark back; see App.safe for which photos.
    { key: "n", label: "Not explicit (mark safe)", when: () => Boolean(App.state.nsfw && App.state.nsfw.enabled),
      run: () => App.safe.fromKeys() },
    // Only there when deleting is switched on; see App.trash for which photos.
    { key: "Del", label: "Delete", when: () => App.state.delete,
      match: (e) => e.key === "Delete", run: () => App.trash.fromKeys() },
    { key: "g p", label: "Photos", match: () => false },
    { key: "g f", label: "Favorites", match: () => false },
    { key: "g m", label: "Map", match: () => false },
    { key: "g v", label: "Videos", match: () => false },
    { key: "+ / -", label: "Tile size", match: (e) => ["+", "=", "-", "_"].includes(e.key),
      run: (e) => App.grid.setSize(e.key === "+" || e.key === "=" ? 1 : -1) },
    { key: "Home", label: "Newest", run: () => { App.viewer.close(); App.shell.setView("grid"); App.grid.newest(); } },
    { key: "Esc", label: "Close, clear", match: () => false },
    { key: "?", label: "Shortcuts", run: () => toggleSheet() },
  ];

  /* `g` then a letter: where to go. The sequence has its own table, shown in
     the hint while it is armed, because a mode you cannot see is a mode that
     feels like the keyboard has stopped working. */
  const GOTO = {
    p: { label: "photos", run: () => App.shell.setQuery("", { push: true, view: "grid" }) },
    f: { label: "favorites", run: () => App.shell.setQuery("is:favorite", { push: true, view: "grid" }) },
    m: { label: "map", run: () => App.shell.setView("map", { push: true }) },
    v: { label: "videos", run: () => App.shell.setQuery("is:video", { push: true, view: "grid" }) },
    l: { label: "live", run: () => App.shell.setQuery("is:live", { push: true, view: "grid" }) },
    s: { label: "screenshots", run: () => App.shell.setQuery("is:screenshot", { push: true, view: "grid" }) },
  };
  const WINDOW = 2000;
  let armed = null;

  function paintHint(on) {
    const hint = document.getElementById("key-hint");
    if (!hint) return;
    hint.hidden = !on;
    if (!on) return;
    const parts = [App.el("kbd", { text: "g" })];
    Object.entries(GOTO).forEach(([k, v], i) => {
      if (i) parts.push(App.el("span", { class: "muted", text: "·" }));
      parts.push(App.el("span", {}, App.el("b", { text: k }), ` ${v.label}`));
    });
    hint.replaceChildren(...parts);
  }

  function cancelSeq() {
    if (!armed) return;
    clearTimeout(armed);
    armed = null;
    paintHint(false);
  }

  function seqKey(e) {
    if (!armed) {
      if (e.key !== "g") return false;
      armed = setTimeout(cancelSeq, WINDOW);
      paintHint(true);
      return true;
    }
    const hit = GOTO[e.key];
    cancelSeq();
    if (!hit) return false;
    App.viewer.close();
    hit.run();
    return true;
  }

  function typing(t) {
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (!document.getElementById("login-overlay").hidden) return;
    // A confirmation is up: it owns the keyboard (its own handler has the
    // keys typed inside it); nothing reaches the grid or the viewer behind.
    if (App.confirm.isOpen()) {
      if (e.key === "Escape") { e.preventDefault(); App.confirm.cancel(); }
      return;
    }
    if (App.search.helpOpen()) {
      if (e.key === "Escape" || e.key === "?") { e.preventDefault(); App.search.closeHelp(); }
      return;
    }
    if (e.key === "Escape") {
      cancelSeq();
      if (document.body.classList.contains("drawer-open")) { App.shell.closeDrawer(); return; }
      if (App.viewer.key(e)) { e.preventDefault(); return; }
      if (typing(e.target)) { e.target.blur(); return; }
      if (App.grid.key(e)) e.preventDefault();
      return;
    }
    if (typing(e.target)) return;
    if (seqKey(e)) { e.preventDefault(); return; }
    // The viewer gets the arrows and Space first; the grid behind it must not
    // move its focus ring while it is covered.
    const viewing = App.viewer.isOpen();
    if (viewing && App.viewer.key(e)) { e.preventDefault(); return; }
    for (const b of BINDINGS) {
      if (b.when && !b.when()) continue;
      const hit = b.match ? b.match(e) : e.key === b.key;
      if (!hit) continue;
      if (b.grid && (viewing || App.state.view !== "grid")) return;
      e.preventDefault();
      b.run(e);
      return;
    }
  }

  /* --- the cheat sheet in the sidebar --------------------------------------

     Two states, because the box answers two questions. Away or on screen is
     "do I want a list of keys in my sidebar at all", and it is remembered
     between sessions; once the keys are learned the box is furniture. Inside
     it, "n more" is the second question: the rare bindings, on demand. `?` is
     the short way to say both. */

  const STORE_KEY = "shortcuts.collapsed";
  const VISIBLE = 9;
  let moreBtn = null;

  function applyCollapsed(state) {
    const box = document.getElementById("shortcut-box");
    if (!box) return;
    box.classList.toggle("collapsed", state);
    const head = box.querySelector(".shortcut-head");
    if (head) {
      head.setAttribute("aria-expanded", String(!state));
      head.title = state ? "Show the shortcuts (?)" : "Hide the shortcuts (?)";
    }
    App.store.set(STORE_KEY, state);
  }

  // The rows the sheet shows: a binding that is switched off is not a key.
  const listed = () => BINDINGS.filter((b) => !b.when || b.when());

  function setOpen(box, open) {
    box.classList.toggle("open", open);
    if (moreBtn) moreBtn.textContent = open ? "less" : `${listed().length - VISIBLE} more`;
  }

  /* What `?` does. Somebody asking for the shortcuts is not asking for nine
     of them: a folded or partial box opens with everything in it, and only a
     box already showing everything is put away. On a phone-sized window the
     sidebar is a drawer, so the drawer comes out with it. */
  function toggleSheet() {
    const box = document.getElementById("shortcut-box");
    if (!box) return;
    const full = !box.classList.contains("collapsed") && box.classList.contains("open");
    applyCollapsed(full);
    setOpen(box, !full);
    if (!full && window.matchMedia("(max-width: 900px)").matches) {
      document.body.classList.add("drawer-open");
      document.getElementById("scrim").hidden = false;
    }
  }

  function cheatSheet() {
    const box = document.getElementById("shortcut-box");
    if (!box) return;
    const rows = listed();
    moreBtn = App.el("button", {
      class: "shortcut-more", type: "button", text: `${rows.length - VISIBLE} more`,
      onclick: () => setOpen(box, !box.classList.contains("open")),
    });
    const head = App.el("button", {
      class: "shortcut-head", type: "button",
      onclick: () => applyCollapsed(!box.classList.contains("collapsed")),
    },
      App.el("span", { text: "Shortcuts" }),
      App.el("span", { class: "shortcut-glyph", html: App.icon("chevron", 14) }),
    );
    box.replaceChildren(head, App.el("div", { class: "shortcut-body" },
      ...rows.map((b) => App.el("div", { class: "shortcut-row" },
        App.el("kbd", { text: b.key }),
        App.el("span", { text: b.label }),
      )),
      rows.length > VISIBLE ? moreBtn : null,
      App.el("div", { class: "version-line", text: App.state.version ? `meerpic ${App.state.version}` : "" }),
    ));
    applyCollapsed(Boolean(App.store.get(STORE_KEY, false)));
  }

  function init() {
    document.addEventListener("keydown", onKey);
    cheatSheet();
  }

  return { init, BINDINGS };
})();
