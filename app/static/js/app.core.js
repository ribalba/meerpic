/* meerpic core: the App namespace, the API client, formatting, shared state.

   Two rules run through the modules that build on this file:

   * **The browser does no timezone arithmetic.** The server sends a photo's
     wall clock as it was where the photo was taken ("2026-09-28T14:32:10", no
     offset), and everything here treats those as the numbers they are. A Date
     is only ever built from those components, so a holiday's pictures are dated
     the way the holiday was lived, whatever zone the laptop is in now.
   * **Server strings are text.** File names, places and paths are put on the
     page with `textContent` (App.el's `text`), never as markup. A photo called
     `<img onerror=...>.jpg` is a legal file name. */

window.App = window.App || {};

// --- events between modules ------------------------------------------------
App.bus = {
  handlers: {},
  on(name, fn) { (this.handlers[name] ||= []).push(fn); },
  emit(name, payload) { (this.handlers[name] || []).forEach((fn) => fn(payload)); },
};

// --- API -------------------------------------------------------------------
App.api = {
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const response = await fetch(path, opts);
    if (response.status === 401) {
      await this.promptLogin();
      return this.request(method, path, body);
    }
    return this.unwrap(response);
  },

  async unwrap(response) {
    if (!response.ok) {
      let detail = response.statusText || `HTTP ${response.status}`;
      let payload = null;
      try {
        payload = await response.json();
        // FastAPI's validation errors are a list; say the first one rather
        // than "[object Object]".
        if (typeof payload.detail === "string") detail = payload.detail;
        else if (Array.isArray(payload.detail) && payload.detail[0]) detail = payload.detail[0].msg || detail;
      } catch (e) { /* not JSON */ }
      const err = new Error(detail);
      err.status = response.status;
      // The whole answer, for the callers an error carries data for (a 409
      // on delete brings the new plan with it).
      err.payload = payload;
      throw err;
    }
    return response.status === 204 ? null : response.json();
  },

  get(path) { return this.request("GET", path); },
  post(path, body) { return this.request("POST", path, body === undefined ? {} : body); },
  put(path, body) { return this.request("PUT", path, body); },
  del(path) { return this.request("DELETE", path); },

  // One overlay and one promise however many requests hit 401 at once, which
  // is what happens when a month-long session expires and the grid, the status
  // poll and the timeline all ask again together.
  promptLogin() {
    if (this._login) return this._login;
    const overlay = document.getElementById("login-overlay");
    const form = document.getElementById("login-form");
    const input = document.getElementById("login-password");
    const error = document.getElementById("login-error");
    this._login = new Promise((resolve) => {
      overlay.hidden = false;
      input.value = "";
      error.hidden = true;
      setTimeout(() => input.focus(), 0);
      form.onsubmit = async (e) => {
        e.preventDefault();
        try {
          await fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: input.value }),
          }).then((r) => { if (!r.ok) throw new Error("Wrong password"); });
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
          input.select();
          return;
        }
        overlay.hidden = true;
        this._login = null;
        resolve();
      };
    });
    return this._login;
  },
};

// --- shared state ----------------------------------------------------------
App.state = {
  ready: false,
  version: "",
  timezone: "",
  roots: [],
  map: { tile_url: "", attribution: "", max_zoom: 19 },
  sync: { enabled: false, remote: "" },
  search: { model: "", ready: false },
  // The explicit-picture classifier: whether it runs, where "flagged" starts,
  // and whether flagged pictures are drawn blurred until asked for.
  nsfw: { enabled: false, threshold: 0.7, blur: false },
  // Whether the agent looks for faces in the photos at all.
  faces: { enabled: false },
  // Whether Delete (into the trash here; iCloud keeps the photo) is on.
  delete: false,
  counts: {},
  // `size` is the grid's target row height in CSS pixels.
  prefs: { size: 200 },
  // The filter bar's text, trimmed. The one source of truth for what is being
  // looked at: the grid, the map and the scrubber all read it from here, and
  // the address bar carries it as ?q=.
  q: "",
  view: "grid",       // grid | map
};

// --- loading ---------------------------------------------------------------
App.load = {
  async state() {
    const s = await App.api.get("/api/state");
    Object.assign(App.state, {
      version: s.version || "",
      timezone: s.timezone || "",
      roots: s.roots || [],
      map: Object.assign({ tile_url: "", attribution: "", max_zoom: 19 }, s.map || {}),
      sync: Object.assign({ enabled: false, remote: "" }, s.sync || {}),
      search: Object.assign({ model: "", ready: false }, s.search || {}),
      nsfw: Object.assign({ enabled: false, threshold: 0.7, blur: false }, s.nsfw || {}),
      faces: Object.assign({ enabled: false }, s.faces || {}),
      delete: Boolean(s.delete),
      counts: s.counts || {},
    });
    // The server's copy on the first load; after that the browser's own is
    // newer (a size change is saved on a delay) and must not be reverted by a
    // refresh that lands in between.
    App.state.prefs = App.state.ready
      ? Object.assign({ size: 200 }, s.prefs || {}, App.state.prefs)
      : Object.assign({ size: 200 }, s.prefs || {});
    App.state.ready = true;
    App.bus.emit("state", s);
    return s;
  },

  prefs() {
    clearTimeout(this._prefTimer);
    // Debounced: the size is changed with a key that repeats.
    this._prefTimer = setTimeout(
      () => App.api.put("/api/prefs", { value: App.state.prefs }).catch(() => {}),
      600,
    );
  },
};

// --- local, per-browser memory ---------------------------------------------
// Reading can throw: a private window is allowed to refuse storage outright,
// and a remembered map position is not worth a page that will not start.
App.store = {
  get(key, fallback = null) {
    try {
      const raw = localStorage.getItem(`meerpic.${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`meerpic.${key}`, JSON.stringify(value)); } catch (e) { /* refused */ }
  },
};

// --- time ------------------------------------------------------------------
App.time = {
  /* A wall-clock string from the server as a Date carrying those exact
     components. Not `new Date(str)`: that is the same thing in every browser
     that matters, but only by convention, and a date-only string would be
     read as UTC midnight and land on the previous evening west of Greenwich. */
  parse(s) {
    if (!s) return null;
    const [date, time = "00:00:00"] = s.split("T");
    const [y, m, d] = date.split("-").map(Number);
    const [hh, mm, ss] = time.split(":").map(Number);
    return new Date(y, (m || 1) - 1, d || 1, hh || 0, mm || 0, Math.floor(ss || 0));
  },
  ymd(d) {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  },
  today() { return this.ymd(new Date()); },
  yesterday() { const d = new Date(); d.setDate(d.getDate() - 1); return this.ymd(d); },
};

// --- formatting ------------------------------------------------------------
App.fmt = {
  n(x) { return Number(x || 0).toLocaleString(); },

  plural(n, one, many) { return `${App.fmt.n(n)} ${n === 1 ? one : (many || `${one}s`)}`; },

  /* Decimal units, the way Finder and every phone count them: a "12 MB"
     photo here is the same 12 MB the phone said it was. */
  bytes(b) {
    if (b === null || b === undefined) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(b);
    let i = 0;
    while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return `${i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
  },

  /* A video's length as a player shows it: 0:07, 3:25, 1:02:10. */
  duration(s) {
    if (s === null || s === undefined) return "";
    const t = Math.max(0, Math.round(Number(s)));
    const p = (n) => String(n).padStart(2, "0");
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    return h ? `${h}:${p(m)}:${p(t % 60)}` : `${m}:${p(t % 60)}`;
  },

  /* How long something will take, rounded to what is worth reading. */
  eta(s) {
    if (s === null || s === undefined || !Number.isFinite(Number(s))) return "";
    const t = Math.round(Number(s));
    if (t < 60) return `${t} s`;
    if (t < 3600) return `${Math.round(t / 60)} min`;
    const h = Math.floor(t / 3600);
    const m = Math.round((t % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  },

  /* An instant from the server ("...Z") as "5 min ago". */
  ago(iso) {
    if (!iso) return "";
    const then = new Date(iso);
    if (Number.isNaN(then.getTime())) return "";
    const s = Math.max(0, (Date.now() - then.getTime()) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    if (s < 2 * 86400) return "yesterday";
    if (s < 14 * 86400) return `${Math.round(s / 86400)} days ago`;
    return then.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  },

  /* A day heading: "Today", "Yesterday", else the full date in the reader's
     own locale, weekday first, because "which Sunday was that" is how photos
     are remembered. */
  day(ymd) {
    if (!ymd) return "No date";
    if (ymd === App.time.today()) return "Today";
    if (ymd === App.time.yesterday()) return "Yesterday";
    return App.time.parse(ymd).toLocaleDateString(undefined,
      { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  },

  /* The whole date, never "Today": the info panel is where a date is read
     rather than glanced at. */
  long(ymd) {
    const d = App.time.parse(ymd);
    return d ? d.toLocaleDateString(undefined,
      { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "No date";
  },

  /* Always H:MM on a 24-hour clock, the way meercal writes a time. */
  clock(d) { return `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`; },

  dateTime(wall) {
    const d = App.time.parse(wall);
    if (!d) return "No date";
    const date = d.toLocaleDateString(undefined,
      { weekday: "short", day: "numeric", month: "short", year: "numeric" });
    return `${date}, ${App.fmt.clock(d)}`;
  },

  short(wall) {
    const d = App.time.parse(wall);
    if (!d) return "No date";
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  },

  month(ym, style = "long") {
    const [y, m] = String(ym).split("-").map(Number);
    return new Date(y, (m || 1) - 1, 1).toLocaleDateString(undefined, { month: style, year: "numeric" });
  },

  /* Minutes east of UTC as "UTC+2", "UTC+5:30", "UTC-3". */
  offset(min) {
    if (min === null || min === undefined) return "";
    const sign = min < 0 ? "-" : "+";
    const a = Math.abs(min);
    const h = Math.floor(a / 60);
    const m = a % 60;
    return `UTC${sign}${h}${m ? `:${String(m).padStart(2, "0")}` : ""}`;
  },

  // Trailing zeros off: "f/1.6", "5.1 mm", "f/8" rather than "f/8.0".
  num(x, digits = 1) { return String(Number(Number(x).toFixed(digits))); },

  /* A shutter speed as a photographer writes it: 1/120 s, 0.5 s, 2 s. */
  exposure(t) {
    if (!t) return "";
    if (t >= 1) return `${App.fmt.num(t)} s`;
    const inv = 1 / t;
    return inv >= 2 ? `1/${Math.round(inv)} s` : `${App.fmt.num(t, 2)} s`;
  },
};

/* --- the query language, as far as the browser needs it ---------------------

   The server owns the language (app/query.py). The browser only ever needs to
   add, replace or drop one filter (the Relevance/Date switch, "Show in grid",
   a place chip) without disturbing whatever else was typed, so this is a
   tokenizer and three edits, not a parser. Quoting follows the server's:
   whitespace separates, `"double quotes"` group, and `key:"a value"` works. */
App.query = (() => {
  const TOKEN = /[A-Za-z]+:"[^"]*"?|"[^"]*"?|\S+/g;

  const unquote = (s) => s.replace(/^"/, "").replace(/"$/, "");

  function tokens(text) {
    return (String(text || "").match(TOKEN) || []).map((raw) => {
      const m = /^([A-Za-z]+):(.*)$/.exec(raw);
      if (m) return { raw, key: m[1].toLowerCase(), value: unquote(m[2]) };
      // A bare year is a filter too; see the language's `year:` shorthand.
      if (/^(19|20)\d\d$|^2100$/.test(raw)) return { raw, key: "year", value: raw };
      return { raw, key: null, value: unquote(raw) };
    });
  }

  function quote(value) {
    const v = String(value).replace(/"/g, "");
    return /\s/.test(v) ? `"${v}"` : v;
  }

  const join = (list) => list.map((t) => t.raw).join(" ");

  return {
    tokens,
    quote,
    get(text, key) {
      const hit = tokens(text).find((t) => t.key === key);
      return hit ? hit.value : null;
    },
    /* Replace every `key:` token with one `key:value`, at the end. */
    set(text, key, value) {
      const rest = tokens(text).filter((t) => t.key !== key);
      rest.push({ raw: `${key}:${quote(value)}` });
      return join(rest);
    },
    remove(text, key) { return join(tokens(text).filter((t) => t.key !== key)); },
    /* The free words: what the server turns into a semantic search. */
    words(text) { return tokens(text).filter((t) => !t.key).map((t) => t.value).join(" "); },
    withoutWords(text) { return join(tokens(text).filter((t) => t.key)); },
  };
})();

// --- small helpers ---------------------------------------------------------
App.el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    // `html` is for this file's own icon markup only (App.icon); nothing that
    // came from the server is ever passed through it.
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "style") node.setAttribute("style", v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  children.flat().forEach((c) => {
    if (c === null || c === undefined || c === false || c === "") return;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  });
  return node;
};

/* A server path ("/media/export/12") as the absolute URL a drag or the
   clipboard needs: a file manager has no page to resolve it against. */
App.abs = (url) => new URL(url, window.location.href).href;

/* A line of feedback at the bottom of the window: "Copied", or what went
   wrong. One at a time; a newer one replaces the older. */
App.toast = (() => {
  let timer = null;
  return (text, opts = {}) => {
    const el = document.getElementById("toast");
    if (!el) return;
    clearTimeout(timer);
    el.textContent = text;
    el.classList.toggle("error", Boolean(opts.error));
    el.hidden = false;
    timer = setTimeout(() => { el.hidden = true; }, opts.ms || (opts.error ? 5000 : 2200));
  };
})();

/* A question whose yes cannot be taken back, as a modal of our own rather
   than confirm(). The browser's dialog puts the focus on OK, so an Enter
   meant for something else, or a key held a moment too long, says yes. Here
   the card itself has the focus when it opens: Enter on it does nothing, Esc
   and a click outside say no, and the danger button is only reached with a
   deliberate Tab or click. Resolves true for that button, false otherwise.

   `body` is a list: a string or a list of nodes is a paragraph, a node is
   placed as it is. `confirm`, when given, runs on the yes before the card
   closes (the buttons wait meanwhile): an error it throws is shown in the
   card, and an object it returns ({ title, body, ... }) redraws the card
   with that and asks again, focus back on the card. */
App.confirm = (() => {
  let backdrop = null;
  let pending = null;       // { resolve, before, opts, card, ok, cancel, error, busy }

  const focusables = () => [...backdrop.querySelectorAll("button:not(:disabled), summary")];

  function finish(answer) {
    if (!pending) return;
    const { resolve, before } = pending;
    pending = null;
    backdrop.hidden = true;
    backdrop.replaceChildren();
    // Back to where the keyboard was, if that is still on the page.
    if (before && before.isConnected && typeof before.focus === "function") before.focus({ preventScroll: true });
    resolve(answer);
  }

  /* No while the yes is still being carried out would be a lie: it may
     already have happened. */
  function cancel() { if (pending && !pending.busy) finish(false); }

  function onKey(e) {
    if (!pending) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      cancel();
      return;
    }
    const list = focusables();
    if (e.key === "Tab" && list.length) {
      // The focus stays in the card: Tab goes round its controls only.
      e.preventDefault();
      const i = list.indexOf(document.activeElement);
      const n = list.length;
      const k = i < 0 ? (e.shiftKey ? n - 1 : 0) : (i + (e.shiftKey ? -1 : 1) + n) % n;
      list[k].focus();
      return;
    }
    // Enter or Space on the card itself (nothing chosen yet) is swallowed;
    // on a focused control the browser's own activation runs as usual.
    if ((e.key === "Enter" || e.key === " ") && !list.includes(document.activeElement)) e.preventDefault();
    // Nothing typed here reaches the app's shortcuts behind the card.
    e.stopPropagation();
  }

  function render(p) {
    const { title, body = [], ok = "OK", danger = false, wide = false } = p.opts;
    p.error = App.el("div", { class: "modal-error", hidden: true });
    p.cancel = App.el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => cancel() });
    p.ok = App.el("button", {
      class: `btn ${danger ? "danger solid" : "primary"}`, type: "button", text: ok, onclick: () => accept(p),
    });
    p.card = App.el("div", {
      class: `modal-card confirm-card${wide ? " wide" : ""}`, tabindex: "-1", role: "alertdialog",
      "aria-modal": "true", "aria-label": title,
    },
      App.el("div", { class: "modal-head" }, App.el("span", { class: "modal-title", text: title })),
      App.el("div", { class: "modal-body" },
        ...body.filter(Boolean).map((t) => (t.nodeType ? t : App.el("p", { class: "confirm-text" }, t))),
        p.error),
      App.el("div", { class: "modal-foot" }, App.el("span", { class: "grow" }), p.cancel, p.ok),
    );
    backdrop.replaceChildren(p.card);
    p.card.focus({ preventScroll: true });
  }

  async function accept(p) {
    if (pending !== p || p.busy) return;
    if (!p.opts.confirm) { finish(true); return; }
    p.busy = true;
    p.ok.disabled = true;
    p.cancel.disabled = true;
    p.error.hidden = true;
    let answer;
    try {
      answer = await p.opts.confirm();
    } catch (err) {
      if (pending !== p) return;
      p.busy = false;
      p.ok.disabled = false;
      p.cancel.disabled = false;
      p.error.textContent = err.message;
      p.error.hidden = false;
      p.card.focus({ preventScroll: true });
      return;
    }
    if (pending !== p) return;
    p.busy = false;
    if (answer && typeof answer === "object") {
      Object.assign(p.opts, answer);
      render(p);
      return;
    }
    finish(answer !== false);
  }

  function ask(opts) {
    if (pending) cancel();
    if (pending) return Promise.resolve(false);
    if (!backdrop) {
      backdrop = App.el("div", { class: "modal-backdrop confirm-backdrop", hidden: true });
      backdrop.addEventListener("keydown", onKey);
      backdrop.addEventListener("click", (e) => { if (e.target === backdrop) cancel(); });
      document.body.append(backdrop);
    }
    return new Promise((resolve) => {
      pending = { resolve, before: document.activeElement, opts: Object.assign({}, opts), busy: false };
      backdrop.hidden = false;
      render(pending);
    });
  }

  return { ask, cancel, isOpen: () => Boolean(pending) };
})();

/* A server URL as a CSS `url()`. The server builds these from an id and a
   hex sig, but a string becomes CSS here, so it is quoted as CSS quotes. */
App.cssUrl = (url) => `url("${String(url).replace(/["\\\n\r]/g, (ch) => `\\${ch.charCodeAt(0).toString(16)} `)}")`;

/* Icons, as inline SVG.

   Drawn rather than borrowed from a font: a glyph is whatever the reader's
   font decides it is, it sits on the text baseline rather than in the middle
   of its button, and half the useful ones are not in any UI font. One weight,
   `currentColor`, so they follow whatever the button is doing about hover and
   theme. The same set and the same pen as meercal's, plus what photos need. */
App.icons = {
  search: '<circle cx="11" cy="11" r="7"/><line x1="16.6" y1="16.6" x2="21" y2="21"/>',
  refresh: '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><polyline points="20.5 3.5 20.5 9 15 9"/>',
  // A disc lit from one side: light, dark, or whatever the system says.
  theme: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 0 0 17z" fill="currentColor" stroke="none"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><line x1="12" y1="11" x2="12" y2="16.5"/>'
      + '<circle cx="12" cy="7.7" r="1" fill="currentColor" stroke="none"/>',
  close: '<line x1="6.5" y1="6.5" x2="17.5" y2="17.5"/><line x1="17.5" y1="6.5" x2="6.5" y2="17.5"/>',
  check: '<polyline points="5 12.4 9.8 17.5 19 6.8"/>',
  // The tile's select circle: a ring with the tick already in it, so hover
  // says what a click will do.
  select: '<circle cx="12" cy="12" r="9"/><polyline points="8 12.4 10.8 15.2 16.2 9.2"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  minus: '<line x1="5" y1="12" x2="19" y2="12"/>',
  left: '<polyline points="14.5 5 8 12 14.5 19"/>',
  right: '<polyline points="9.5 5 16 12 9.5 19"/>',
  chevron: '<polyline points="6 9.5 12 15.5 18 9.5"/>',
  up: '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="6 11 12 5 18 11"/>',
  menu: '<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/>'
      + '<line x1="4" y1="17" x2="20" y2="17"/>',
  download: '<path d="M12 3.5v11"/><polyline points="7.5 10 12 14.5 16.5 10"/>'
          + '<path d="M4.5 17.5v2a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1v-2"/>',
  photo: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/>'
       + '<polyline points="20.5 15.5 15 10.5 6 19"/>',
  video: '<rect x="3.5" y="6.5" width="12" height="11" rx="2"/>'
       + '<polygon points="15.5 10.5 20.5 7.5 20.5 16.5 15.5 13.5"/>',
  play: '<polygon points="8 5.5 19 12 8 18.5" fill="currentColor"/>',
  // Apple's own figure for a Live Photo: a dot, a ring, and a dotted ring.
  live: '<circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="5.3"/>'
      + '<circle cx="12" cy="12" r="8.7" stroke-dasharray="1.2 2.4"/>',
  screenshot: '<rect x="7" y="3" width="10" height="18" rx="2"/><line x1="10.5" y1="17.8" x2="13.5" y2="17.8"/>',
  map: '<polygon points="3.5 6.5 9 4 15 6.5 20.5 4 20.5 17.5 15 20 9 17.5 3.5 20"/>'
     + '<line x1="9" y1="4" x2="9" y2="17.5"/><line x1="15" y1="6.5" x2="15" y2="20"/>',
  pin: '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/>',
  calendar: '<rect x="4" y="5" width="16" height="15" rx="2"/><line x1="4" y1="9.5" x2="20" y2="9.5"/>'
          + '<line x1="8.5" y1="3" x2="8.5" y2="6.5"/><line x1="15.5" y1="3" x2="15.5" y2="6.5"/>',
  camera: '<path d="M4 8.5a2 2 0 0 1 2-2h2.2l1.5-2h4.6l1.5 2H18a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>'
        + '<circle cx="12" cy="12.8" r="3.4"/>',
  file: '<path d="M6 3.5h8l4 4v13H6z"/><polyline points="14 3.5 14 7.5 18 7.5"/>',
  // A capital T with a line of text beneath: the words in a picture.
  text: '<path d="M5.5 7V4.5h13V7"/><line x1="12" y1="4.5" x2="12" y2="15"/><line x1="9.5" y1="15" x2="14.5" y2="15"/>'
      + '<line x1="5" y1="19.5" x2="19" y2="19.5"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/>'
      + '<path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
  // Three frames and a spark: "more like this".
  similar: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/>'
         + '<rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/>'
         + '<path d="M17 13.3l1 2.7 2.7 1-2.7 1-1 2.7-1-2.7-2.7-1 2.7-1z"/>',
  // A face in a viewfinder's corners: a face the agent found in a picture.
  face: '<path d="M4 8.5V6a2 2 0 0 1 2-2h2.5M15.5 4H18a2 2 0 0 1 2 2v2.5'
      + 'M20 15.5V18a2 2 0 0 1-2 2h-2.5M8.5 20H6a2 2 0 0 1-2-2v-2.5"/>'
      + '<line x1="9.5" y1="9.3" x2="9.5" y2="10.7"/><line x1="14.5" y1="9.3" x2="14.5" y2="10.7"/>'
      + '<path d="M9.3 14.4a3.6 3.6 0 0 0 5.4 0"/>',
  external: '<path d="M14 4.5h5.5V10"/><line x1="19.5" y1="4.5" x2="11" y2="13"/>'
          + '<path d="M18 14v4.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1H10"/>',
  warning: '<path d="M12 4 21 19.5H3z"/><line x1="12" y1="10" x2="12" y2="14"/>'
         + '<circle cx="12" cy="16.8" r=".9" fill="currentColor" stroke="none"/>',
  grid: '<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/>'
      + '<rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
  // The grid with one frame filled: this picture, among all the others.
  inGrid: '<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1" fill="currentColor"/>'
        + '<rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
  aperture: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5 14.6 12M20.5 12 12 14.6M12 20.5 9.4 12M3.5 12 12 9.4"/>',
  heart: '<path d="M12 19.5 5.2 12.7a4.3 4.3 0 0 1 6.1-6.1l.7.7.7-.7a4.3 4.3 0 0 1 6.1 6.1z"/>',
  heartFill: '<path d="M12 19.5 5.2 12.7a4.3 4.3 0 0 1 6.1-6.1l.7.7.7-.7a4.3 4.3 0 0 1 6.1 6.1z" fill="currentColor"/>',
  // An eye with a stroke through it: "not shown until you ask".
  eyeOff: '<path d="M3.5 12s3.2-5.5 8.5-5.5 8.5 5.5 8.5 5.5-3.2 5.5-8.5 5.5S3.5 12 3.5 12z"/>'
        + '<circle cx="12" cy="12" r="2.4"/><line x1="4.5" y1="4.5" x2="19.5" y2="19.5"/>',
  // Marked safe: a shield with a tick.
  safe: '<path d="M12 3.5 19 6.2v5.3c0 4.3-2.9 7.6-7 9-4.1-1.4-7-4.7-7-9V6.2z"/>'
    + '<polyline points="8.8 12.2 11.2 14.6 15.4 10.2"/>',
  trash: '<line x1="4.5" y1="7" x2="19.5" y2="7"/><path d="M9.5 7V4.8h5V7"/>'
       + '<path d="M6.5 7l.9 12.1a1 1 0 0 0 1 .9h7.2a1 1 0 0 0 1-.9L17.5 7"/>'
       + '<line x1="10.2" y1="10.5" x2="10.2" y2="16.5"/><line x1="13.8" y1="10.5" x2="13.8" y2="16.5"/>',
  // Two prints, one behind the other.
  album: '<rect x="3.5" y="7.5" width="13.5" height="12.5" rx="2"/><path d="M7 4.5h11.5a2 2 0 0 1 2 2v10"/>',
  chat: '<path d="M5 19.5l1.2-3.7A7.6 7.6 0 1 1 9 18.6z"/>',
  // A tray with something in it: pictures that arrived rather than were taken.
  inbox: '<path d="M3.8 13.5h4.7l1.5 2.5h4l1.5-2.5h4.7"/>'
       + '<path d="M6 5h12l2.2 8.5v5A1.5 1.5 0 0 1 18.7 20H5.3a1.5 1.5 0 0 1-1.5-1.5v-5z"/>',
  edit: '<path d="M4.5 19.5h3.8L19 8.8a2.7 2.7 0 0 0-3.8-3.8L4.5 15.7z"/><line x1="13.8" y1="6.4" x2="17.6" y2="10.2"/>',
};

App.icon = (name, size = 17) => {
  const body = App.icons[name];
  if (!body) return "";
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
};

/* Fill everything carrying `data-icon`. Called once at boot, and again by
   anything that builds buttons of its own after it. */
App.paintIcons = (root = document) => {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = App.icon(el.dataset.icon, Number(el.dataset.iconSize) || 17);
  });
};
