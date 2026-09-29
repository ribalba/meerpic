/* The viewer: one photo, the whole window, and the way through the rest.

   It opens on the thumbnail, which is already in the browser's cache from the
   grid, and swaps in the full-size picture when that has arrived, so the
   picture is on screen at once and only gets sharper. The frame is sized from
   the photo's own dimensions rather than from whichever image is currently in
   it; otherwise the swap would jump from a 400 px thumbnail's box to the real
   one's.

   Left and right walk the grid's own list (the current search, in its order),
   fetching the next page at the end of it, and the neighbours' full-size
   images are fetched ahead so the walk does not wait.

   Videos play the preview the agent makes (H.264 in an mp4, which every
   browser plays; an iPhone's HEVC MOV is not something Firefox or most
   Chromium builds will). A video without one asks for it and polls until it is
   there. Live Photos are the same trick on the still's companion clip, played
   muted over the still while the LIVE button is hovered or Space is held.
   While a video's playable copy is being made, its storyboard (the agent's
   strip of frames) stands in as a filmstrip, so there is something of the
   clip to see besides a spinner.

   A picture the classifier flagged opens blurred behind a Show button when
   the settings ask for that; once shown it stays shown for the session.

   The info panel shows the faces the agent found in a still. Pointing at one
   outlines it on the photo; clicking it lists the other pictures with that
   face in them (`face:`).

   The address carries ?photo=<id>, so reload and the back button work. */

window.App = window.App || {};

App.viewer = (() => {
  const POLL = 2000;
  const POLL_LIMIT = 90;    // three minutes of asking before saying so
  const IDLE = 2600;

  let root = null;
  let bar = null;
  let stageEl = null;
  let infoEl = null;
  let frame = null;
  let imgEl = null;
  let motionEl = null;
  let videoEl = null;
  let waitEl = null;
  let prevBtn = null;
  let nextBtn = null;
  let liveBtn = null;
  let mapBtn = null;
  let infoBtn = null;
  let dateEl = null;
  let placeEl = null;
  let posEl = null;
  let miniEl = null;
  let deleteBtn = null;
  let safeBtn = null;
  let veilEl = null;
  let filmEl = null;
  let stillEl = null;
  let faceBox = null;

  let list = null;          // an explicit list, or null to walk the grid's
  let card = null;
  let detail = null;
  let token = 0;            // bumped per photo; late answers compare against it
  let pushed = false;       // whether opening added a history entry to undo
  let infoOpen = false;
  let liveWanted = false;
  let livePreparing = false;
  let idleTimer = null;
  // After "Show original": the edit (and the list it was walked in) that
  // "Back to the edit" returns to. `id` is the original's.
  let returnTo = null;
  let strip = null;         // the open video's storyboard, once loaded
  let stillK = 0;           // the storyboard frame on show

  const details = new Map();
  // Flagged pictures the reader chose to see, this session.
  const revealed = new Set();

  // --- data --------------------------------------------------------------------

  function fetchDetail(id, fresh) {
    if (!fresh && details.has(id)) return details.get(id);
    const p = App.api.get(`/api/photos/${id}`);
    details.set(id, p);
    p.catch(() => details.delete(id));
    // A few hundred at most: the cache is for walking back and forth, not a
    // second copy of the library.
    if (details.size > 300) details.delete(details.keys().next().value);
    return p;
  }

  const current = () => (detail && card && detail.id === card.id ? Object.assign({}, card, detail) : card);
  const items = () => list || App.grid.items();
  const index = () => (card ? items().findIndex((c) => c.id === card.id) : -1);
  const located = (c) => c && c.lat !== null && c.lat !== undefined && c.lon !== null && c.lon !== undefined;

  // --- building ------------------------------------------------------------------

  function button(icon, label, title, run, cls = "") {
    return App.el("button", { class: `vw-btn ${cls}`.trim(), type: "button", title, "aria-label": label, onclick: run },
      App.el("span", { html: App.icon(icon, 19) }));
  }

  function build() {
    root = document.getElementById("viewer");
    bar = document.getElementById("vw-bar");
    stageEl = document.getElementById("vw-stage");
    infoEl = document.getElementById("vw-info");

    dateEl = App.el("span", { class: "vw-date" });
    placeEl = App.el("span", { class: "vw-place" });
    posEl = App.el("span", { class: "vw-pos" });
    liveBtn = App.el("button", { class: "vw-btn vw-live", type: "button", title: "Play the Live Photo (hover, or hold Space)", text: "LIVE", hidden: true });
    mapBtn = button("map", "Show on map", "Show on map (m)", () => showOnMap(), "optional");
    infoBtn = button("info", "Info", "Info (i)", () => toggleInfo());
    deleteBtn = button("trash", "Delete", "Delete here, into the trash (Del)", () => remove());
    deleteBtn.hidden = !App.state.delete;
    safeBtn = button("safe", "Mark as safe", "Not explicit: mark as safe (n)", () => markSafe(), "optional");
    safeBtn.hidden = true;
    bar.replaceChildren(
      button("close", "Close", "Close (Esc)", () => close()),
      App.el("div", { class: "vw-title" }, dateEl, placeEl),
      posEl,
      liveBtn,
      button("similar", "Similar", "Find similar (s)", () => { if (card) App.shell.similar(card.id); }, "optional"),
      button("inGrid", "Show in all photos", "Show in all photos (a)", () => showInAll(), "optional"),
      mapBtn,
      button("copy", "Copy", "Copy (c)", () => App.drag.copy(current())),
      button("download", "Download", "Download (d); original: shift+d", () => App.drag.download(current())),
      safeBtn,
      deleteBtn,
      infoBtn,
    );

    imgEl = App.el("img", { alt: "", draggable: "true" });
    motionEl = App.el("video", { class: "vw-motion", muted: true, playsinline: true, preload: "auto" });
    motionEl.muted = true;
    frame = App.el("div", { class: "vw-frame" });
    waitEl = App.el("div", { class: "vw-wait", hidden: true });
    prevBtn = App.el("button", { class: "vw-nav prev", type: "button", title: "Previous (←)", "aria-label": "Previous", html: App.icon("left", 26), onclick: () => step(-1) });
    nextBtn = App.el("button", { class: "vw-nav next", type: "button", title: "Next (→)", "aria-label": "Next", html: App.icon("right", 26), onclick: () => step(1) });
    veilEl = App.el("div", { class: "vw-veil", hidden: true },
      App.el("span", { class: "vw-veil-icon", html: App.icon("eyeOff", 30) }),
      App.el("span", { class: "vw-veil-text", text: "Possibly explicit" }),
      App.el("div", { class: "vw-veil-actions" },
        App.el("button", { class: "btn", type: "button", text: "Show", onclick: () => reveal() }),
        App.el("button", { class: "btn", type: "button", text: "Mark as safe", title: "Not explicit (n)",
          onclick: () => markSafe(true) })),
    );
    filmEl = App.el("div", { class: "vw-film", hidden: true });
    // One frame of the storyboard over the poster, as the pointer or the
    // filmstrip picks it.
    stillEl = App.el("div", { class: "vw-still", hidden: true });
    // The outline of the face pointed at in the info panel.
    faceBox = App.el("div", { class: "vw-face", hidden: true });
    stageEl.replaceChildren(frame, veilEl, filmEl, waitEl, prevBtn, nextBtn);
    miniEl = App.el("div", { class: "info-map" });

    frame.addEventListener("pointermove", (e) => {
      if (!strip || filmEl.hidden || e.pointerType === "touch") return;
      const r = frame.getBoundingClientRect();
      stillAt(App.story.frameAt(strip, r, e.clientX).k);
    });

    // The still is a drag source like a tile: out of the window and into a mail.
    imgEl.addEventListener("pointerdown", (e) => { if (e.button === 0 && window.meerpicDesktop && card) App.drag.prepare([current()]); });
    imgEl.addEventListener("dragstart", (e) => { if (card) App.drag.start(e, [current()]); else e.preventDefault(); });

    motionEl.addEventListener("ended", () => paintLive(false));
    liveBtn.addEventListener("pointerenter", (e) => { if (e.pointerType === "mouse") liveOn(); });
    liveBtn.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") liveOff(); });
    // A finger has no hover: a tap plays it once.
    liveBtn.addEventListener("click", () => { if (motionEl.classList.contains("playing")) liveOff(); else liveOn(); });

    // A click on the empty stage around the picture closes, the way a
    // lightbox does; a click on the picture itself does nothing.
    stageEl.addEventListener("click", (e) => { if (e.target === stageEl) close(); });

    // Swipes, for a phone: sideways to walk, down to close.
    let touch = null;
    stageEl.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "touch") return;
      touch = { x: e.clientX, y: e.clientY, t: Date.now() };
    });
    stageEl.addEventListener("pointerup", (e) => {
      if (!touch || e.pointerType !== "touch") return;
      const dx = e.clientX - touch.x;
      const dy = e.clientY - touch.y;
      touch = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
      else if (dy > 110 && Math.abs(dy) > Math.abs(dx) * 1.5) close();
    });

    // The chrome fades while the mouse rests, so the picture is the only thing
    // on screen; any movement brings it back.
    root.addEventListener("pointermove", () => wake());
    new ResizeObserver(() => fit()).observe(stageEl);
  }

  function wake() {
    root.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      if (!root.hidden && !bar.matches(":hover")) root.classList.add("idle");
    }, IDLE);
  }

  // --- the picture ---------------------------------------------------------------

  /* The frame, sized to the photo's shape inside the stage. Never enlarged
     past the photo's own pixel size when that is known: a 300 px icon blown up
     to fill a monitor is a blur, not a better view of it. */
  function fit() {
    if (!card || !frame) return;
    // On a phone the info panel is a sheet over the bottom of the stage; the
    // picture moves up into what is left rather than hiding behind it.
    const sheet = infoOpen && !infoEl.hidden && getComputedStyle(infoEl).position === "absolute"
      ? infoEl.offsetHeight : 0;
    stageEl.style.paddingBottom = sheet ? `${sheet}px` : "";
    // The notes along the bottom (the wait pill, the filmstrip) stand above it.
    stageEl.style.setProperty("--sheet", `${sheet}px`);
    const W = stageEl.clientWidth;
    const H = stageEl.clientHeight - sheet;
    if (!W || !H) return;
    const d = detail && detail.id === card.id ? detail : null;
    let w = (d && d.w) || card.w;
    let h = (d && d.h) || card.h;
    let cap = 1;
    if (!w || !h) {
      const media = card.kind === "video" ? videoEl : imgEl;
      w = (media && (media.naturalWidth || media.videoWidth)) || 4;
      h = (media && (media.naturalHeight || media.videoHeight)) || 3;
      cap = Infinity;
    }
    const scale = Math.min(W / w, H / h, cap);
    frame.style.width = `${Math.max(1, Math.floor(w * scale))}px`;
    frame.style.height = `${Math.max(1, Math.floor(h * scale))}px`;
    // A storyboard frame on show is positioned in pixels of the old size.
    if (strip && stillEl && !stillEl.hidden) stillAt(stillK);
  }

  function wait(text, spin = true) {
    if (!text) { waitEl.hidden = true; return; }
    // Not straight into replaceChildren: it writes a null out as "null".
    waitEl.replaceChildren(...[spin ? App.el("span", { class: "spinner" }) : null, App.el("span", { text })].filter(Boolean));
    waitEl.hidden = false;
  }

  function stopMedia() {
    liveWanted = false;
    livePreparing = false;
    if (videoEl) { videoEl.pause(); videoEl.removeAttribute("src"); videoEl.load(); }
    motionEl.pause();
    motionEl.removeAttribute("src");
    paintLive(false);
    unfilm();
    wait("");
  }

  // --- a flagged picture, blurred until asked for ------------------------------------

  function isVeiled(c) {
    return Boolean(c && App.state.nsfw && App.state.nsfw.blur && c.nsfw_flag && !revealed.has(c.id));
  }

  function paintVeil(c) {
    const on = isVeiled(c);
    stageEl.classList.toggle("veiled", on);
    veilEl.hidden = !on;
  }

  function reveal() {
    if (!card) return;
    revealed.add(card.id);
    paintVeil(current());
    // The face crops in the info panel were held back with it.
    infoEl.querySelectorAll(".face-btn img.veiled").forEach((img) => img.classList.remove("veiled"));
    // A video held back while blurred starts now.
    if (videoEl && videoEl.getAttribute("src") && videoEl.paused) videoEl.play().catch(() => {});
  }

  // --- the storyboard, while a video waits for its playable copy -------------------

  const FILM_H = 56;

  function film(c) {
    const mine = token;
    if (!c || !c.story || !(c.story_frames > 0)) return;
    App.story.load(c.story, c.story_frames).then((s) => {
      // Too late: another photo, or the copy arrived first.
      if (mine !== token || !s || !videoEl || videoEl.getAttribute("src")) return;
      strip = s;
      const aspect = (s.w / s.n) / s.h;
      // As tall as FILM_H, or less where the frames would not fit across.
      const room = Math.max(120, stageEl.clientWidth - 48) / s.n - 3;
      const w = Math.max(12, Math.floor(Math.min(FILM_H * aspect, room)));
      const h = Math.max(8, Math.round(w / aspect));
      const cells = [];
      for (let k = 0; k < s.n; k++) {
        const cell = App.el("span", { class: "vw-film-cell", style: `width:${w}px;height:${h}px` });
        App.story.paint(cell, s, k, w, h);
        cell.addEventListener("pointerenter", () => stillAt(k));
        cell.addEventListener("click", () => stillAt(k));
        cells.push(cell);
      }
      filmEl.replaceChildren(...cells);
      filmEl.hidden = false;
      frame.append(stillEl);
      // Nothing to play yet; controls that do nothing would sit under the strip.
      videoEl.controls = false;
    });
  }

  /* Frame k over the poster, and marked in the strip. The frame box has the
     video's own shape, so covering it is showing the frame whole. */
  function stillAt(k) {
    if (!strip) return;
    stillK = k;
    App.story.paint(stillEl, strip, k, frame.clientWidth, frame.clientHeight);
    stillEl.hidden = false;
    [...filmEl.children].forEach((cell, i) => cell.classList.toggle("on", i === k));
  }

  function unfilm() {
    strip = null;
    if (!filmEl) return;
    filmEl.hidden = true;
    filmEl.replaceChildren();
    stillEl.hidden = true;
    stillEl.remove();
  }

  function paintMedia(c) {
    paintVeil(c);
    if (c.kind === "video") {
      videoEl = App.el("video", { controls: true, autoplay: !isVeiled(c), playsinline: true, preload: "auto", poster: c.thumb || null });
      videoEl.addEventListener("loadedmetadata", fit);
      frame.replaceChildren(videoEl);
    } else {
      videoEl = null;
      imgEl.onload = null;
      imgEl.removeAttribute("src");
      if (c.thumb) imgEl.src = c.thumb;
      imgEl.onload = () => fit();
      frame.replaceChildren(imgEl, motionEl);
    }
    liveBtn.hidden = !(c.kind !== "video" && c.live);
    fit();
  }

  /* What arrived with the detail: the full-size still, or the playable video. */
  function upgrade(d) {
    const mine = token;
    fit();
    if (card.kind === "video") {
      if (d.urls && d.urls.video) play(d.urls.video);
      else {
        film(Object.assign({}, card, d));
        prepare(card.id, false).then((fresh) => {
          if (fresh && mine === token && fresh.urls && fresh.urls.video) { detail = fresh; play(fresh.urls.video); }
        });
      }
      return;
    }
    liveBtn.hidden = !(d.live_video_id || card.live);
    const url = d.urls && d.urls.display;
    if (!url) return;
    const full = new Image();
    full.onload = () => { if (mine === token) imgEl.src = url; };
    full.src = url;
  }

  function play(url) {
    if (!videoEl) return;
    wait("");
    unfilm();
    videoEl.controls = true;
    videoEl.src = url;
    // Blurred, it waits for Show rather than playing behind the blur.
    if (isVeiled(current())) return;
    videoEl.play().catch(() => { /* autoplay refused; the controls are there */ });
  }

  /* Ask the agent for a playable copy and wait for it. For a Live still the
     server enqueues the companion's preview; the still's own detail says when
     it is ready (`urls.live`). */
  async function prepare(id, forLive) {
    const mine = token;
    const agent = App.status.agentAlive();
    wait(forLive ? "Preparing the motion..." : "Preparing a playable copy...");
    let answer;
    try {
      answer = await App.api.post(`/api/photos/${id}/preview`);
    } catch (err) {
      if (mine === token) wait(`No playable copy: ${err.message}`, false);
      return null;
    }
    if (mine !== token) return null;
    if (answer.ready) {
      const d = await fetchDetail(id, true).catch(() => null);
      if (mine === token) wait("");
      return d;
    }
    if (agent === false) wait("Waiting for the agent, which is not running (make up)", true);
    for (let n = 0; n < POLL_LIMIT; n++) {
      await new Promise((r) => setTimeout(r, POLL));
      if (mine !== token) return null;
      const d = await fetchDetail(id, true).catch(() => null);
      if (mine !== token) return null;
      if (d && d.preview_ready) { wait(""); return d; }
    }
    if (mine === token) wait("Still not ready: the agent may be busy with a sync or a large video.", false);
    return null;
  }

  // --- Live Photos -------------------------------------------------------------------

  function paintLive(on) {
    motionEl.classList.toggle("playing", on);
    liveBtn.classList.toggle("playing", on);
  }

  function liveOn() {
    if (!card || card.kind === "video" || liveBtn.hidden) return;
    liveWanted = true;
    const d = detail && detail.id === card.id ? detail : null;
    if (d && d.urls && d.urls.live) {
      if (motionEl.getAttribute("src") !== d.urls.live) motionEl.src = d.urls.live;
      motionEl.currentTime = 0;
      motionEl.play().then(() => { if (liveWanted) paintLive(true); }).catch(() => {});
      return;
    }
    if (!d || livePreparing) return;
    livePreparing = true;
    const mine = token;
    prepare(card.id, true).then((fresh) => {
      if (mine !== token) return;
      livePreparing = false;
      if (fresh) { detail = fresh; if (liveWanted) liveOn(); }
    });
  }

  function liveOff() {
    liveWanted = false;
    motionEl.pause();
    paintLive(false);
  }

  // --- the bar and the info panel ---------------------------------------------------

  function paintBar(c) {
    dateEl.textContent = c.taken ? App.fmt.dateTime(c.taken) : "No date";
    placeEl.textContent = c.place || c.name;
    const i = index();
    const info = App.grid.info();
    const n = list ? list.length : (info.total || items().length);
    posEl.textContent = i >= 0 && n > 1 ? `${App.fmt.n(i + 1)} of ${App.fmt.n(n)}` : "";
    if (returnTo && returnTo.id === c.id) posEl.textContent = "Original";
    mapBtn.hidden = !located(c) || !App.state.map.tile_url;
    deleteBtn.hidden = !App.state.delete;
    // Only where it means something: flagged, or marked (to take it back).
    const cc = current() || c;
    safeBtn.hidden = !(App.state.nsfw && App.state.nsfw.enabled && (cc.nsfw_flag || cc.nsfw_safe));
    safeBtn.classList.toggle("on", Boolean(cc.nsfw_safe));
    safeBtn.title = cc.nsfw_safe ? "Marked safe: take the mark back (n)" : "Not explicit: mark as safe (n)";
    prevBtn.disabled = i <= 0;
    nextBtn.disabled = i < 0 || (i >= items().length - 1 && (list || !App.grid.hasMore()));
  }

  function row(icon, ...children) {
    return App.el("div", { class: "info-row" },
      App.el("span", { class: "info-icon", html: App.icon(icon, 18) }),
      App.el("div", { class: "info-main" }, ...children));
  }

  const line = (text, cls = "info-line") => (text ? App.el("span", { class: cls, text }) : null);

  /* When iCloud got it, where that is not when it was taken: a picture a
     messenger saved, a scan of an old print. An instant, so it is shown in
     this computer's zone like every other instant. */
  function addedLine(c) {
    if (!c.added_at) return null;
    const added = new Date(c.added_at);
    if (Number.isNaN(added.getTime())) return null;
    const taken = c.taken_utc ? new Date(c.taken_utc) : null;
    if (taken && !Number.isNaN(taken.getTime()) && Math.abs(added - taken) <= 86400000) return null;
    const date = added.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    return line(`Added to iCloud ${date}, ${App.fmt.clock(added)}`, "info-sub");
  }

  function dateRows(c) {
    if (!c.taken) return row("calendar", line("No date"), line("Nothing in the file says when it was taken.", "info-sub"), addedLine(c));
    const d = App.time.parse(c.taken);
    const guess = ["mtime", "filename", "none"].includes(c.date_source);
    const bits = [App.fmt.clock(d)];
    if (c.tz_offset !== null && c.tz_offset !== undefined) bits.push(App.fmt.offset(c.tz_offset));
    if (guess) bits.push("(file date)");
    const why = guess ? (c.date_source === "filename" ? "Read from the file name." : "The file's own date: nothing inside it says when it was taken.") : "";
    return row("calendar", line(App.fmt.long(c.day || c.taken.slice(0, 10))), line(bits.join(" · "), "info-sub"), line(why, "info-sub"), addedLine(c));
  }

  const linkBtn = (text, run) => App.el("button", { class: "link", type: "button", text, onclick: run });

  function albumChip(name) {
    const q = `album:${App.query.quote(name)}`;
    return App.el("button", {
      class: "place-chip", type: "button", title: q,
      onclick: () => App.shell.setQuery(q, { push: true, view: "grid" }),
    }, App.el("span", { class: "chip-icon", html: App.icon("album", 13) }), App.el("span", { text: name }));
  }

  // Albums every photo is in, or that a row of their own already says.
  const QUIET_ALBUMS = new Set(["all photos", "favorites", "hidden", "recently deleted"]);

  /* What iCloud knows about it and the file does not: the heart, the albums,
     Hidden and Recently Deleted. */
  function libraryRows(c) {
    const out = [];
    if (c.favorite) {
      const r = row("heartFill", line("Favorite"));
      r.classList.add("fav");
      out.push(r);
    }
    const albums = (Array.isArray(c.albums) ? c.albums : []).filter((a) => !QUIET_ALBUMS.has(String(a).toLowerCase()));
    if (albums.length) out.push(row("album", App.el("div", { class: "info-chips" }, ...albums.map(albumChip))));
    if (c.hidden) out.push(row("eyeOff", line("Hidden"), line("In the Hidden album, so left out unless asked for (is:hidden).", "info-sub")));
    if (c.icloud_deleted) out.push(row("trash", line("Recently deleted"), line("In Recently Deleted in iCloud, so left out unless asked for (is:deleted).", "info-sub")));
    return out;
  }

  /* The faces the agent found, largest first, as round crops. Only from the
     full detail: the grid's cards do not carry them, and a row that is empty
     until the detail arrives would say "no faces" for a moment first. A
     video, or a library without face detection, has no row at all. */
  function facesRow(c, full) {
    if (!full) return null;
    if (c.faces_state === "pending") return row("face", line("Not searched for faces yet", "info-sub"));
    const faces = c.faces_state === "done" && Array.isArray(c.faces) ? c.faces : [];
    if (!faces.length) return null;
    const veiled = isVeiled(c);
    return row("face", App.el("div", { class: "info-faces" }, ...faces.map((f) => faceButton(f, veiled))));
  }

  /* One face, and the way to every other picture of that person. Pointing
     at it, or tabbing to it, outlines it on the photo: two crops side by
     side do not say which of the people in the picture is which. */
  function faceButton(f, veiled) {
    const label = "Find every picture with this face";
    return App.el("button", {
      class: "face-btn", type: "button", title: label, "aria-label": label,
      onclick: () => App.shell.setQuery(`face:${f.id}`, { push: true, view: "grid" }),
      onpointerenter: () => outline(f), onpointerleave: () => outline(null),
      onfocus: () => outline(f), onblur: () => outline(null),
    }, App.el("img", { src: f.url, alt: "", class: veiled ? "veiled" : null, draggable: "false" }));
  }

  /* A face's box over the photo. The box is in fractions of the picture as
     shown, and the frame has exactly the picture's shape (see fit), so
     percentages of the frame land on the face at any size. */
  function outline(f) {
    const b = f && f.box;
    if (!b) { faceBox.hidden = true; return; }
    faceBox.style.left = `${b.x * 100}%`;
    faceBox.style.top = `${b.y * 100}%`;
    faceBox.style.width = `${b.w * 100}%`;
    faceBox.style.height = `${b.h * 100}%`;
    // The frame's children are replaced per photo, so it goes back in here.
    if (faceBox.parentNode !== frame) frame.append(faceBox);
    faceBox.hidden = false;
  }

  /* The text the agent read in the picture, line by line, for reading and
     copying (an IBAN, an address, an order number). The words a `text:`
     search asked for are marked, and pointing at a line of a photo outlines
     it where it is written, with the faces' box. A video's lines come from
     several frames and have no place to point at. Long documents start
     folded. Only from the full detail, like the faces. */
  const TEXT_FOLDED = 8;
  let textOpen = false;

  function textRow(c, full) {
    if (!full) return null;
    if (c.text_state === "pending") return row("text", line("Not read for text yet", "info-sub"));
    const lines = c.text_state === "done" && Array.isArray(c.text_lines) ? c.text_lines : [];
    if (!lines.length) return null;
    const terms = App.query.tokens(App.state.q).filter((t) => t.key === "text")
      .flatMap((t) => t.value.split(/\s+/)).filter(Boolean);
    // Folded during a `text:` search, the lines it was found in rather than
    // the first ones: on a receipt the word is rarely at the top.
    const re = termsRe(terms);
    const hits = re ? lines.filter((l) => re.test(String(l.t || ""))) : [];
    const shown = textOpen || lines.length <= TEXT_FOLDED + 2 ? lines
      : (hits.length ? hits : lines).slice(0, TEXT_FOLDED);
    const body = App.el("div", { class: "info-text" }, ...shown.map((l) => textLine(l, terms)));
    const more = shown.length < lines.length
      ? linkBtn(`Show all ${App.fmt.n(lines.length)} lines`, () => { textOpen = true; paintInfo(); })
      : null;
    const copy = linkBtn("Copy text", () => copyText(lines.map((l) => l.t).join("\n")));
    return row("text", body, App.el("span", { class: "info-sub" }, ...(more ? [more, " · "] : []), copy));
  }

  function textLine(l, terms) {
    const b = Array.isArray(l.b) && l.b.length === 4 ? { x: l.b[0], y: l.b[1], w: l.b[2], h: l.b[3] } : null;
    const attrs = { class: "info-text-line" };
    if (b) {
      attrs.onpointerenter = () => outline({ box: b });
      attrs.onpointerleave = () => outline(null);
    }
    return App.el("div", attrs, ...marked(String(l.t || ""), terms));
  }

  /* The text with every occurrence of the search's words in <mark>, in any
     case, as the server matched them. */
  function termsRe(terms) {
    if (!terms.length) return null;
    const esc = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return new RegExp(`(${esc.join("|")})`, "i");
  }

  function marked(text, terms) {
    const re = termsRe(terms);
    if (!re) return [text];
    const parts = text.split(re);
    return parts.map((p, i) => (i % 2 ? App.el("mark", { text: p }) : p)).filter((p) => p !== "");
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      App.toast("Text copied");
    } catch {
      App.toast("The browser would not copy the text", { error: true });
    }
  }

  /* The classifier's flag, and what a person said about it: marked safe, or
     not flagged (or listed last in is:nsfw) for looking like a photo that
     was, which the row links to. */
  function nsfwRow(c) {
    if (!(App.state.nsfw && App.state.nsfw.enabled)) return null;
    const score = Number(c.nsfw);
    const said = Number.isFinite(score) ? `The classifier gave it ${score.toFixed(2)} out of 1.` : null;
    const like = c.nsfw_like;
    const likeLink = like
      ? App.el("button", { class: "link", type: "button", text: like.name, onclick: () => open(like.id) })
      : null;
    const alike = like ? ` (${Number(like.similarity).toFixed(2)} similar)` : "";
    if (c.nsfw_safe) {
      return row("safe", line("Marked safe"), line(said, "info-sub"),
        App.el("div", {}, linkBtn("Take the mark back", () => markSafe(false))));
    }
    if (!c.nsfw_flag && like && like.cleared) {
      return row("safe", line("Not flagged: looks like a photo you marked safe"),
        App.el("span", { class: "info-sub" }, likeLink, alike), line(said, "info-sub"));
    }
    if (!c.nsfw_flag) return null;
    return row("eyeOff", line(`Possibly explicit${Number.isFinite(score) ? ` (${score.toFixed(2)})` : ""}`),
      line("The image classifier's confidence, from 0 to 1.", "info-sub"),
      like ? App.el("span", { class: "info-sub" }, "Listed last in is:nsfw: a bit like ", likeLink, `${alike}, which you marked safe.`) : null,
      App.el("div", {}, linkBtn("Mark as safe", () => markSafe(true))));
  }

  function editRow(c) {
    if (returnTo && returnTo.id === c.id) {
      return row("edit", App.el("span", { class: "info-line" }, "The original, before editing · ", linkBtn("Back to the edit", () => backToEdit())));
    }
    if (c.original_id) {
      return row("edit", App.el("span", { class: "info-line" }, "Edited · ", linkBtn("Show original", () => showOriginal(c.original_id))));
    }
    return null;
  }

  function exposureLine(c) {
    const e = c.exposure || {};
    const bits = [];
    if (e.exposure_time) bits.push(App.fmt.exposure(e.exposure_time));
    if (e.f_number) bits.push(`f/${App.fmt.num(e.f_number)}`);
    if (e.iso) bits.push(`ISO ${e.iso}`);
    if (e.focal_length) bits.push(`${App.fmt.num(e.focal_length)} mm`);
    const eq = c.exif && c.exif.FocalLengthIn35mmFormat;
    if (eq) bits.push(`(${App.fmt.num(eq, 0)} mm equiv.)`);
    return bits.join(" · ");
  }

  function paintInfo() {
    if (!infoOpen || !card) return;
    // The buttons are drawn anew, and one going away under the pointer
    // never says the pointer left it.
    outline(null);
    const c = current();
    const full = Boolean(detail && detail.id === card.id);
    const nodes = [dateRows(c), ...libraryRows(c)];
    const faces = facesRow(c, full);
    if (faces) nodes.push(faces);
    const text = textRow(c, full);
    if (text) nodes.push(text);

    const loc = c.location || (located(c) ? { lat: c.lat, lon: c.lon } : null);
    if (loc || c.place) {
      const coords = loc ? `${App.fmt.num(loc.lat, 5)}, ${App.fmt.num(loc.lon, 5)}${loc.altitude !== null && loc.altitude !== undefined ? ` · ${Math.round(loc.altitude)} m` : ""}` : "";
      nodes.push(row("pin", line(c.place || "Somewhere"), line(coords, "info-sub"),
        loc && App.state.map.tile_url ? miniEl : null));
    }

    const cam = c.camera || {};
    const camera = [cam.make, cam.model].filter(Boolean).join(" ");
    const exposure = exposureLine(c);
    if (camera || cam.lens || exposure) {
      nodes.push(row("camera", line(camera || "Unknown camera"), line(cam.lens, "info-sub"), line(exposure, "info-sub")));
    }

    const dims = c.w && c.h ? `${c.w} × ${c.h}` : "";
    const facts = [dims, full ? App.fmt.bytes(c.size) : "", c.duration ? App.fmt.duration(c.duration) : "",
      full && c.video_codec ? c.video_codec.toUpperCase() : "", full && c.ext ? c.ext.toUpperCase() : ""].filter(Boolean);
    const kind = c.kind === "video" ? "video" : (c.screenshot ? "screenshot" : "photo");
    nodes.push(row(kind, line(c.name), line(facts.join(" · "), "info-sub"),
      c.live ? line("Live Photo", "info-sub") : null,
      c.screenshot ? line("Screenshot", "info-sub") : null));

    const edit = editRow(c);
    if (edit) nodes.push(edit);
    const nsfw = nsfwRow(c);
    if (nsfw) nodes.push(nsfw);

    if (c.path) nodes.push(row("file", App.el("span", { class: "info-path", text: c.path, title: "The original, on disk" })));
    if (full && c.error) nodes.push(row("warning", App.el("span", { class: "info-error", text: c.error })));

    const exportExt = String(c.export_name || "").split(".").pop().toUpperCase();
    const actions = App.el("div", { class: "info-actions" },
      App.el("button", { class: "btn", onclick: () => App.drag.download(current()), text: `Download${exportExt ? ` (${exportExt})` : ""}` }),
      App.el("button", { class: "btn", onclick: () => App.drag.download(current(), true), text: "Download original" }),
      App.el("button", { class: "btn", onclick: () => App.drag.copy(current()), text: "Copy" }),
      App.el("button", { class: "btn", onclick: () => App.shell.similar(card.id), text: "Find similar" }),
      App.el("button", { class: "btn", onclick: () => showInAll(), text: "Show in all photos" }),
      located(c) && App.state.map.tile_url ? App.el("button", { class: "btn", onclick: () => showOnMap(), text: "Show on map" }) : null,
      App.el("button", {
        class: "btn", text: "Open original",
        onclick: () => window.open((c.urls && c.urls.original) || `/media/original/${c.id}`, "_blank", "noopener"),
      }),
      App.state.delete ? App.el("button", { class: "btn danger", onclick: () => remove(), text: "Delete" }) : null,
    );

    infoEl.replaceChildren(
      App.el("div", { class: "info-head" },
        App.el("span", { class: "info-title", text: "Info" }),
        App.el("button", { class: "icon-btn", type: "button", title: "Close (i)", "aria-label": "Close info", html: App.icon("close", 17), onclick: () => toggleInfo(false) })),
      App.el("div", { class: "info-body" }, ...nodes, actions),
    );
    if (loc && App.state.map.tile_url && miniEl.isConnected) App.map.mini(miniEl, loc.lat, loc.lon);
    fit();
  }

  function toggleInfo(on) {
    infoOpen = on === undefined ? !infoOpen : on;
    App.store.set("viewer.info", infoOpen);
    infoEl.hidden = !infoOpen;
    infoBtn.classList.toggle("on", infoOpen);
    if (infoOpen) paintInfo(); else outline(null);
    fit();
  }

  function showOnMap() {
    const c = current();
    if (!located(c)) return;
    App.shell.showOnMap([c], { near: true });
  }

  /* Out of the list it was found in and into the timeline, ringed there.
     An original shown from its edit is in no listing; the edit stands in. */
  function showInAll() {
    if (!card) return;
    App.shell.showInAll(returnTo && returnTo.id === card.id ? returnTo.card : current());
  }

  // --- moving through the list --------------------------------------------------------

  function prefetch() {
    const arr = items();
    const i = index();
    if (i < 0) return;
    // The end of what is loaded, coming up: the next page, ahead of need.
    if (!list && i >= arr.length - 6 && App.grid.hasMore()) App.grid.more();
    [arr[i + 1], arr[i - 1]].forEach((c) => {
      if (!c || c.kind === "video") return;
      fetchDetail(c.id).then((d) => { if (d.urls && d.urls.display) new Image().src = d.urls.display; }).catch(() => {});
    });
  }

  function show(c) {
    token++;
    const mine = token;
    stopMedia();
    if (returnTo && c.id !== returnTo.id) returnTo = null;
    if (!card || card.id !== c.id) textOpen = false;
    card = c;
    detail = null;
    paintMedia(c);
    paintBar(c);
    if (infoOpen) paintInfo();
    wake();
    fetchDetail(c.id).then((d) => {
      if (mine !== token) return;
      detail = d;
      upgrade(d);
      paintBar(c);
      if (infoOpen) paintInfo();
      prefetch();
    }).catch((err) => {
      if (mine === token) wait(`Could not load this photo: ${err.message}`, false);
    });
  }

  async function step(dir) {
    if (!card) return;
    let i = index();
    if (dir > 0 && !list && i >= items().length - 1 && App.grid.hasMore()) {
      await App.grid.more();
      i = index();
    }
    const arr = items();
    const target = arr[i + dir];
    if (i < 0 || !target) return;
    show(target);
    if (!list) App.grid.setFocus(target.id, false);
    App.shell.writeUrl(false);
  }

  // --- an edit's original ------------------------------------------------------------------

  /* The original an edit replaced is in no listing (Photos keeps it behind
     the edit as well), so it is shown on its own, and the info panel offers
     the way back to the edit and the list it was walked in. */
  async function showOriginal(id) {
    const from = { card, list };
    let d;
    try {
      d = await fetchDetail(id);
    } catch (err) {
      App.toast(`Could not open the original: ${err.message}`, { error: true });
      return;
    }
    if (!card || card.id !== from.card.id) return;
    list = [d];
    show(d);
    returnTo = { id: d.id, card: from.card, list: from.list };
    paintBar(d);
    if (infoOpen) paintInfo();
    App.shell.writeUrl(false);
  }

  function backToEdit() {
    const r = returnTo;
    if (!r) return;
    returnTo = null;
    list = r.list;
    show(r.card);
    App.shell.writeUrl(false);
  }

  // --- delete ------------------------------------------------------------------------------

  /* Delete the open photo, after the confirmation, and step on to the next
     one (the previous at the end of the list); close when there is none. An
     original shown from its edit takes the edit with it, so that goes too. */
  async function remove() {
    if (!card || !App.state.delete) return;
    const c = current();
    const arr = items();
    const i = index();
    const after = i >= 0 ? (arr[i + 1] || arr[i - 1] || null) : null;
    const edit = returnTo && returnTo.id === c.id ? returnTo.card : null;
    const ok = await App.trash.ask([c], { also: edit ? [edit] : [] });
    if (!ok || !card || card.id !== c.id) return;
    if (list) list = list.filter((x) => x.id !== c.id);
    if (edit || !after) { close(); return; }
    show(after);
    if (!list) App.grid.setFocus(after.id, false);
    App.shell.writeUrl(false);
  }

  /* Mark the open photo safe (or take the mark back; with no argument, the
     one it does not have). In is:nsfw the photo, and any lookalike it
     cleared, leave the list, so this steps on to the next flagged one the way
     Delete does: going through the list is one key per photo. */
  async function markSafe(safe) {
    if (!card) return;
    const c = current();
    const want = safe === undefined ? !c.nsfw_safe : Boolean(safe);
    const arr = items();
    const i = index();
    const answer = await App.safe.mark([c], want);
    if (!answer || !card || card.id !== c.id) return;
    const d = await fetchDetail(c.id, true).catch(() => null);
    if (!card || card.id !== c.id) return;
    if (d) detail = d;
    if (want && App.safe.inNsfw() && !list) {
      const still = new Set(items().map((x) => x.id));
      const next = arr.slice(i + 1).find((x) => still.has(x.id))
        || arr.slice(0, Math.max(i, 0)).reverse().find((x) => still.has(x.id)) || null;
      if (!next) { close(); return; }
      show(next);
      App.grid.setFocus(next.id, false);
      App.shell.writeUrl(false);
      return;
    }
    paintVeil(current());
    paintBar(card);
    if (infoOpen) paintInfo();
  }

  /* Drop what the cache holds for these photos: something about them
     changed (a mark), and a stale detail would override the fresh card. */
  function forget(ids) {
    (ids || []).forEach((id) => details.delete(id));
  }

  // --- open and close --------------------------------------------------------------------

  /* Open a photo. From the grid it walks the grid's list; `opts.list` gives
     it a list of its own (a single map marker, the similar banner's source).
     An id nobody has loaded (a reload on ?photo=) is fetched and shown alone. */
  async function open(id, opts = {}) {
    if (!root) build();
    App.hover.stop();
    list = opts.list || null;
    let c = list ? list.find((x) => x.id === id) : App.grid.card(id);
    if (!c) {
      try {
        c = await fetchDetail(id);
      } catch (err) {
        App.toast(`Could not open that photo: ${err.message}`, { error: true });
        if (opts.fromUrl) App.shell.writeUrl(false);
        return;
      }
      list = [c];
    }
    const wasOpen = !root.hidden;
    root.hidden = false;
    document.body.classList.add("viewing");
    infoEl.hidden = !infoOpen;
    infoBtn.classList.toggle("on", infoOpen);
    show(c);
    if (!wasOpen && !opts.fromUrl) pushed = App.shell.writeUrl(true);
    else App.shell.writeUrl(false);
  }

  function close(fromHistory) {
    if (!root || root.hidden) return;
    const id = card ? card.id : null;
    stopMedia();
    token++;
    root.hidden = true;
    document.body.classList.remove("viewing");
    card = null;
    detail = null;
    returnTo = null;
    const hadList = Boolean(list);
    list = null;
    if (!fromHistory) {
      // Undo the entry opening it made, so Back after Esc leaves the app
      // rather than reopening the photo that was just closed.
      if (pushed) { pushed = false; history.back(); } else App.shell.writeUrl(false);
    }
    pushed = false;
    if (id !== null && !hadList && App.grid.card(id)) App.grid.setFocus(id, true);
  }

  /* Keys the viewer owns while it is open. Everything else (d, c, s, a, i, m)
     goes through the shared binding table, which asks App.shell.target(). */
  function key(e) {
    if (!root || root.hidden) return false;
    switch (e.key) {
      case "Escape":
        close();
        return true;
      case "ArrowLeft": step(-1); return true;
      case "ArrowRight": step(1); return true;
      case " ":
        if (e.repeat) return true;
        if (card && card.kind === "video" && videoEl) {
          if (videoEl.paused) videoEl.play().catch(() => {}); else videoEl.pause();
        } else liveOn();
        return true;
      default:
        return false;
    }
  }

  function keyUp(e) {
    if (e.key === " " && liveWanted) liveOff();
  }

  function init() {
    build();
    infoOpen = Boolean(App.store.get("viewer.info", false));
    document.addEventListener("keyup", keyUp);
  }

  return {
    init, open, close, key, step, remove, showInAll, markSafe, forget,
    isOpen: () => Boolean(root && !root.hidden),
    current: () => (root && !root.hidden ? current() : null),
    currentId: () => (root && !root.hidden && card ? card.id : null),
    toggleInfo: () => toggleInfo(),
  };
})();
