/* Sync and indexing, in the sidebar.

   The web app runs no rclone and no ffmpeg; the agent does, and reports into
   the database, and /api/status is the one place that reads it back. So this
   module is a poll and a painter: every two seconds while something is moving
   (a sync, the indexing backlog), every twenty otherwise, and not at all while
   the window is in the background (see app.power.js).

   What it has to get across, in order of how much it matters:

   * the agent is not running, which silently stops everything else;
   * the iCloud session has expired, which is the one failure the user has to
     fix by hand, so the command to do it is printed where it can be copied;
   * a sync in progress: how far, how fast, how long;
   * the backlog: how many files still want thumbnails, search vectors,
     playable videos. New photos appear in the grid as their thumbnails land,
     and "why is it grey" is answered here. */

window.App = window.App || {};

App.status = (() => {
  const FAST = 2000;
  const SLOW = 20000;

  let timer = null;
  let running = false;
  let last = null;
  let card = null;
  let syncBtn = null;
  let warning = null;
  let inflight = false;

  const active = (job) => Boolean(job && (job.status === "queued" || job.status === "running"));

  function pending(s) {
    const idx = (s && s.index) || {};
    return (idx.meta || 0) + (idx.thumbs || 0) + (idx.embeddings || 0) + (idx.story || 0) + (idx.nsfw || 0)
      + (idx.faces || 0) + (idx.ocr || 0);
  }

  /* Fast while it is worth watching. A dead agent is not: nothing it owes
     will move until it is started again. */
  function busy(s) {
    if (!s) return false;
    if (active(s.sync)) return true;
    if (!(s.agent && s.agent.alive)) return false;
    return pending(s) > 0 || (s.jobs || []).some(active);
  }

  function start() {
    stop();
    running = true;
    poll();
  }

  function stop() {
    running = false;
    clearTimeout(timer);
    timer = null;
  }

  async function poll() {
    clearTimeout(timer);
    if (inflight) return;
    inflight = true;
    let s = null;
    try {
      s = await App.api.get("/api/status");
    } catch (e) {
      s = null;
    } finally {
      inflight = false;
    }
    if (s) {
      last = s;
      paint(s);
      App.bus.emit("status", s);
    }
    if (running) timer = setTimeout(poll, busy(s) ? FAST : SLOW);
  }

  // --- painting ------------------------------------------------------------------

  const el = App.el;

  function head(text, dotClass, ...extra) {
    return el("div", { class: "st-head" },
      dotClass !== null ? el("span", { class: `st-dot ${dotClass || ""}`.trim() }) : null,
      el("span", { text }), ...extra);
  }

  function progressBar(p) {
    const pct = Number(p.percent);
    const known = Number.isFinite(pct) && (p.total_bytes > 0 || p.total_files > 0);
    const bar = el("div", { class: `st-bar${known ? "" : " indeterminate"}` },
      el("div", { class: "st-fill", style: `width:${known ? Math.max(1, Math.min(100, pct)) : 30}%` }));
    return bar;
  }

  function cancelLink(job) {
    return el("button", {
      class: "link", type: "button", text: "Cancel", title: "Stop this sync",
      onclick: async () => {
        try { await App.api.post(`/api/jobs/${job.id}/cancel`); } catch (err) { App.toast(err.message, { error: true }); }
        poll();
      },
    });
  }

  function syncRunning(job) {
    const p = job.progress || {};
    const nodes = [];
    if (job.status === "queued") {
      nodes.push(head("Sync requested", "busy", cancelLink(job)));
      nodes.push(el("div", { class: "st-line", text:
        App.status.agentAlive() === false ? "Waiting for the agent, which is not running" : "Waiting for the agent to start it" }));
      return nodes;
    }
    const pct = Number(p.percent);
    nodes.push(head("Syncing with iCloud", "busy", cancelLink(job)));
    nodes.push(progressBar(p));
    // Bytes first, with the percentage, because that is what the bar is
    // drawn from; the file count is the number that means something.
    const counts = [];
    if (p.total_bytes) {
      const share = Number.isFinite(pct) ? `${Math.round(pct)}% \u00b7 ` : "";
      counts.push(`${share}${App.fmt.bytes(p.bytes || 0)} of ${App.fmt.bytes(p.total_bytes)}`);
    }
    if (p.total_files) counts.push(`${App.fmt.n(p.files || 0)} of ${App.fmt.plural(p.total_files, "file")}`);
    else if (p.total_checks) counts.push(`checked ${App.fmt.n(p.checks || 0)} of ${App.fmt.n(p.total_checks)}`);
    counts.forEach((c) => nodes.push(el("div", { class: "st-line st-num" }, el("span", { text: c }))));
    const pace = [];
    if (p.speed) pace.push(`${App.fmt.bytes(p.speed)}/s`);
    if (p.eta !== null && p.eta !== undefined && p.total_bytes) pace.push(`${App.fmt.eta(p.eta)} left`);
    if (p.copied) pace.push(`${App.fmt.n(p.copied)} new`);
    if (pace.length) nodes.push(el("div", { class: "st-line st-num" }, el("span", { text: pace.join(" · ") })));
    const current = Array.isArray(p.current) ? p.current.filter(Boolean) : [];
    if (current.length) nodes.push(el("div", { class: "st-current", title: current.join("\n"), text: current[0] }));
    else if (!counts.length && job.message) nodes.push(el("div", { class: "st-line" }, el("span", { text: job.message })));
    if (p.errors) {
      const tail = (job.log_tail || "").split("\n").filter(Boolean).slice(-6).join("\n");
      nodes.push(el("div", { class: "st-line st-error", title: tail || null }, el("span", { text: App.fmt.plural(p.errors, "error") })));
    }
    if (job.error) nodes.push(authOrError(job.error));
    return nodes;
  }

  /* The iCloud re-login, which the agent spells out when it sees one. The
     command is lifted out onto a line of its own, selectable in one click. */
  function authOrError(text) {
    const cmd = /rclone config reconnect \S+/.exec(text);
    if (cmd || /icloud session|reconnect|2fa|trust token/i.test(text)) {
      const prose = cmd ? text.replace(cmd[0], "").replace(/[\s:.]+$/, "").trim() : text;
      return el("div", { class: "st-auth" },
        el("span", { text: prose || "The iCloud session has expired." }),
        cmd ? el("code", { text: cmd[0], title: "Run this in a terminal" }) : null);
    }
    return el("div", { class: "st-line st-error", title: text }, el("span", { text }));
  }

  function syncFinished(s) {
    const job = s.sync;
    const nodes = [];
    if (job && job.status === "failed") {
      nodes.push(head(`Sync failed ${App.fmt.ago(job.finished_at || job.created_at)}`, null,
        el("button", { class: "link", type: "button", text: "Try again", onclick: () => sync() })));
      nodes.push(authOrError(job.error || job.message || "rclone stopped with an error"));
      if (s.last_sync) nodes.push(el("div", { class: "st-line" }, el("span", { text: `Last good sync ${App.fmt.ago(s.last_sync)}` })));
      return nodes;
    }
    if (job && job.status === "cancelled") {
      nodes.push(head(`Sync cancelled ${App.fmt.ago(job.finished_at || job.created_at)}`, ""));
      return nodes;
    }
    if (s.last_sync) {
      const note = job && job.status === "done" && job.message ? ` · ${job.message}` : "";
      nodes.push(el("div", { class: "st-line" },
        el("span", { class: "st-dot" }),
        el("span", { text: `Synced ${App.fmt.ago(s.last_sync)}${note}`, title: new Date(s.last_sync).toLocaleString() })));
    }
    return nodes;
  }

  function indexing(s) {
    const idx = s.index || {};
    const parts = [];
    if (idx.meta) parts.push(`${App.fmt.n(idx.meta)} to read`);
    if (idx.thumbs) parts.push(App.fmt.plural(idx.thumbs, "thumbnail"));
    if (idx.embeddings) parts.push(`${App.fmt.n(idx.embeddings)} for search`);
    if (idx.story) parts.push(App.fmt.plural(idx.story, "storyboard"));
    if (idx.nsfw && App.state.nsfw && App.state.nsfw.enabled) parts.push(`${App.fmt.n(idx.nsfw)} to classify`);
    if (idx.faces && App.state.faces && App.state.faces.enabled) parts.push(`${App.fmt.n(idx.faces)} to scan for faces`);
    if (idx.ocr && App.state.ocr && App.state.ocr.enabled) parts.push(`${App.fmt.n(idx.ocr)} to read for text`);
    // Previews are made on demand unless the config says "all", in which case
    // the count only means something while the agent is actually at them.
    const phase = String((s.agent && s.agent.phase) || "");
    if (idx.previews && (parts.length || /preview/i.test(phase))) parts.push(App.fmt.plural(idx.previews, "video"));
    const nodes = [];
    if (parts.length) {
      nodes.push(el("div", { class: "st-line st-index" },
        el("span", { class: `st-dot ${s.agent && s.agent.alive ? "busy" : "stalled"}` }),
        el("span", { text: `Indexing: ${parts.join(" · ")}`, title: `Indexing: ${parts.join(", ")}` })));
    }
    if (idx.failed) {
      nodes.push(el("div", { class: "st-line", title: "Each one says why in its info panel (i). They are retried when the file changes." },
        el("span", { text: `${App.fmt.plural(idx.failed, "file")} could not be processed` })));
    }
    return nodes;
  }

  /* --- deletes, which the agent does like a sync: in its own time, and never
     while a sync is running ------------------------------------------------ */

  const RECENT = 10 * 60 * 1000;
  const dismissed = new Set();

  function recent(job, ms) {
    const t = new Date(job.finished_at || job.created_at || 0).getTime();
    return Number.isFinite(t) && Date.now() - t < ms;
  }

  function deleteRunning(job, s) {
    const p = job.progress || {};
    const nodes = [];
    const n = Number(p.total) || (job.params && Array.isArray(job.params.photo_ids) ? job.params.photo_ids.length : 0);
    if (job.status === "queued") {
      nodes.push(head(n ? `Deleting ${App.fmt.plural(n, "photo")}` : "Delete requested", "busy"));
      const why = App.status.agentAlive() === false ? "Waiting for the agent, which is not running"
        : active(s.sync) ? "Waits for the sync to finish" : "Waiting for the agent to start it";
      nodes.push(el("div", { class: "st-line", text: why }));
      return nodes;
    }
    const done = Number(p.done) || 0;
    nodes.push(head("Moving to the trash", "busy"));
    nodes.push(progressBar({ percent: n ? (done / n) * 100 : NaN, total_files: n }));
    const bits = [];
    if (n) bits.push(`${App.fmt.n(done)} of ${App.fmt.plural(n, "photo")}`);
    if (p.failed) bits.push(`${App.fmt.n(p.failed)} failed`);
    if (bits.length) nodes.push(el("div", { class: "st-line st-num" }, el("span", { text: bits.join(" · ") })));
    else if (job.message) nodes.push(el("div", { class: "st-line" }, el("span", { text: job.message })));
    if (job.error) nodes.push(...errorLines(job.error));
    return nodes;
  }

  /* The agent writes one "NAME: reason" line per photo it could not delete;
     the auth hint, when it is that, stands on its own. */
  function errorLines(text) {
    const lines = String(text).split("\n").map((l) => l.trim()).filter(Boolean);
    const auth = lines.find((l) => /rclone config reconnect|icloud session|2fa|trust token/i.test(l));
    const nodes = auth ? [authOrError(auth)] : [];
    const rest = lines.filter((l) => l !== auth);
    rest.slice(0, 4).forEach((l) => nodes.push(el("div", { class: "st-line st-error", title: l }, el("span", { text: l }))));
    if (rest.length > 4) nodes.push(el("div", { class: "st-line st-error", title: rest.slice(4).join("\n") }, el("span", { text: `and ${App.fmt.n(rest.length - 4)} more` })));
    return nodes;
  }

  function deleteFinished(job) {
    const p = job.progress || {};
    const nodes = [];
    const dismiss = el("button", {
      class: "link", type: "button", text: "Dismiss",
      onclick: () => { dismissed.add(job.id); if (last) paint(last); },
    });
    if (App.trash.failed(job)) {
      const n = Number(p.failed) || 0;
      nodes.push(head(n ? `${App.fmt.plural(n, "photo")} not deleted` : `Delete failed ${App.fmt.ago(job.finished_at || job.created_at)}`, null, dismiss));
      if (job.error) nodes.push(...errorLines(job.error));
      else if (job.message) nodes.push(el("div", { class: "st-line" }, el("span", { text: job.message })));
      nodes.push(el("div", { class: "st-line", text: "They are back in the grid." }));
      return nodes;
    }
    nodes.push(el("div", { class: "st-line" },
      el("span", { class: "st-dot" }),
      el("span", { text: `${job.message || "Moved to the trash"} · ${App.fmt.ago(job.finished_at)}` })));
    return nodes;
  }

  function deleting(s) {
    const jobs = (s.jobs || []).filter((j) => j.kind === "delete").sort((a, b) => b.id - a.id);
    const nodes = [];
    jobs.filter(active).reverse().forEach((job) => nodes.push(...deleteRunning(job, s)));
    // The newest finished one, for a while: a failure until dismissed or ten
    // minutes old, a success for two.
    const done = jobs.find((j) => !active(j));
    if (done && !dismissed.has(done.id) && !jobs.some(active)
        && recent(done, App.trash.failed(done) ? RECENT : 2 * 60 * 1000)) {
      nodes.push(...deleteFinished(done));
    }
    return nodes;
  }

  function paint(s) {
    const job = s.sync;
    const nodes = active(job) ? syncRunning(job) : syncFinished(s);
    const del = deleting(s);
    if (del.length && nodes.length) nodes.push(el("div", { class: "st-sep" }));
    nodes.push(...del);
    nodes.push(...indexing(s));
    card.replaceChildren(...nodes);

    const alive = Boolean(s.agent && s.agent.alive);
    warning.hidden = alive;
    if (!alive) {
      warning.textContent = "The agent is not running: make up";
      warning.title = s.agent && s.agent.seen_at
        ? `Last heard from ${App.fmt.ago(s.agent.seen_at)}${s.agent.host ? ` on ${s.agent.host}` : ""}. Nothing is synced or indexed until it is back.`
        : "It has never reported in. Nothing is synced or indexed until it runs.";
    }
    paintSyncButton(s);
  }

  function paintSyncButton(s) {
    const on = active(s && s.sync);
    syncBtn.classList.toggle("spin", on);
    const enabled = App.state.sync && App.state.sync.enabled;
    const lastLine = s && s.last_sync ? `Last synced ${App.fmt.ago(s.last_sync)}` : "Not synced yet";
    syncBtn.title = !enabled
      ? "Sync is switched off in the configuration ([sync] in meerpic.toml)"
      : on ? `Syncing with iCloud (.)\n${lastLine}` : `Sync with iCloud (.)\n${lastLine}`;
    syncBtn.style.opacity = enabled ? "" : ".45";
  }

  /* The Sync button and `.`. The server hands back the sync already queued
     or running rather than starting a second, so pressing it twice is safe. */
  async function sync() {
    if (!(App.state.sync && App.state.sync.enabled)) {
      App.toast("Sync is switched off in the configuration ([sync] in meerpic.toml).", { error: true });
      return;
    }
    syncBtn.classList.add("spin");
    try {
      await App.api.post("/api/sync");
    } catch (err) {
      App.toast(err.message, { error: true });
    }
    poll();
  }

  function init() {
    card = document.getElementById("status-card");
    syncBtn = document.getElementById("btn-sync");
    warning = document.getElementById("agent-warning");
    syncBtn.onclick = () => sync();
  }

  return {
    init, start, stop, poll, sync,
    last: () => last,
    /* true, false, or null when nobody has asked yet. */
    agentAlive: () => (last ? Boolean(last.agent && last.agent.alive) : null),
  };
})();
