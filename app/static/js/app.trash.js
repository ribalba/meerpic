/* Deleting: here, into the trash. The photo stays in iCloud and on the
   phone: rclone can read iCloud Photos but not delete there, so the agent
   moves the files to .meerpic-trash in the library folder and every later
   sync leaves them in iCloud (agent/delete.py).

   Nothing moves before the reader has seen exactly what will. A press of
   Delete first asks the server for the plan (POST /api/photos/delete/plan):
   every file that goes (a Live Photo's video, an edit's original) and where
   it moves. The confirmation shows the moves as they will happen, and its
   Delete is never the focused button (see App.confirm). The yes sends the
   moves back exactly as shown; if the library changed in between, the
   server answers 409 with a fresh plan, which replaces the old one in the
   same card and has to be confirmed again.

   Once accepted, the pictures leave the grid at once; the server has marked
   them and queued one `delete` job, and the agent does the deleting when no
   sync is running. The sidebar's status card follows that job like a sync.
   If some of it fails, those photos are unmarked by the agent, so the grid is
   loaded again (they come back) and the toast says which. */

window.App = window.App || {};

App.trash = (() => {
  const statusOf = new Map();   // delete job id -> the status last seen
  const mine = new Set();       // delete jobs this window started
  let planning = false;         // a plan is being asked for

  const active = (status) => status === "queued" || status === "running";

  function noun(n, videos) {
    if (n === 1) return videos ? "this video" : "this photo";
    return `${App.fmt.n(n)} ${videos ? "videos" : "photos"}`;
  }

  /* The plan in an answer: the body itself, or FastAPI's `detail` when the
     server raised it with a 409. */
  function planOf(x) {
    if (!x || typeof x !== "object") return null;
    if (Array.isArray(x.moves)) return x;
    if (x.detail && typeof x.detail === "object" && Array.isArray(x.detail.moves)) return x.detail;
    if (x.plan && typeof x.plan === "object" && Array.isArray(x.plan.moves)) return x.plan;
    return null;
  }

  /* How many files the plan takes: the server's own count (core/deletion.py),
     else one step per file in each group, else one move per file. */
  function fileCount(plan) {
    if (Number.isInteger(plan.count)) return plan.count;
    let n = 0;
    (Array.isArray(plan.groups) ? plan.groups : []).forEach((g) => {
      if (g && Array.isArray(g.steps)) n += g.steps.length;
    });
    return n || (plan.moves || []).length;
  }

  function block(lines, cls) {
    return App.el("pre", { class: `confirm-cmds ${cls || ""}`.trim(), text: lines.join("\n") });
  }

  /* The card's body for a plan: what deleting means, how many files that is,
     and the exact moves. */
  function bodyFor(plan, ctx, changed) {
    const gone = (plan.missing || []).map(Number);
    const ids = ctx.ids.filter((id) => !gone.includes(id));
    const n = ids.length;
    const files = fileCount(plan);
    const moves = (plan.moves || []).map(String);
    const icloud = (plan.excluded || []).length > 0;
    const nodes = [];
    const note = (text, cls = "confirm-note") => App.el("p", { class: `confirm-text ${cls}`, text });
    if (changed) nodes.push(note("Something changed; please check again.", "confirm-warn"));
    nodes.push([
      n === 1 ? "The file here moves to " : "The files here move to ",
      App.el("code", { text: ".meerpic-trash" }),
      " in the library folder, which is emptied after 30 days.",
    ]);
    if (icloud) {
      nodes.push(note(n === 1
        ? "It stays in iCloud and on your iPhone. Sync won't download it again."
        : "They stay in iCloud and on your iPhone. Sync won't download them again."));
    }
    if (files > n) {
      nodes.push(note(`${App.fmt.plural(n, "photo")} = ${App.fmt.plural(files, "file")}: Live Photo videos and originals of edits go with them.`));
    }
    if (ctx.original) nodes.push(note("This is the original of an edit; the edit is deleted too."));
    const missing = gone.length;
    if (missing) nodes.push(note(`${App.fmt.plural(missing, "photo")} of these ${missing === 1 ? "is" : "are"} gone already and left out.`));
    nodes.push(App.el("div", { class: "confirm-heading", text: moves.length === 1 ? "This file will move" : "These files will move" }));
    nodes.push(block(moves));
    return { body: nodes, title: `Delete ${noun(n, ctx.videos)} here?`, ids };
  }

  /* Ask, and on yes do it. `opts.also` are cards that go with these without
     being asked for by id (an edit, when its original is deleted); they only
     leave the grid. Resolves true once the server has taken the delete and
     the tiles are gone (the agent does the rest later): the viewer steps on. */
  async function ask(cards, opts = {}) {
    cards = (cards || []).filter(Boolean);
    if (!App.state.delete || !cards.length || planning) return false;
    const ctx = {
      ids: cards.map((c) => c.id),
      videos: cards.every((c) => c.kind === "video"),
      original: Boolean(opts.also && opts.also.length),
    };
    let plan;
    planning = true;
    try {
      plan = planOf(await App.api.post("/api/photos/delete/plan", { ids: ctx.ids }));
      if (!plan) throw new Error("the server sent no plan");
    } catch (err) {
      App.toast(`Could not prepare the delete: ${err.message}`, { error: true, ms: 7000 });
      return false;
    } finally {
      planning = false;
    }
    let view = bodyFor(plan, ctx, false);
    if (!view.ids.length) {
      App.grid.remove(ctx.ids);
      App.toast("Those are gone already");
      return false;
    }
    let answer = null;
    const yes = await App.confirm.ask({
      title: view.title, body: view.body, ok: "Delete", danger: true, wide: true,
      confirm: async () => {
        try {
          // The moves as shown, and the names Sync will leave out: a change
          // to either sends the dialog back.
          answer = await App.api.post("/api/photos/delete",
            { ids: view.ids, moves: plan.moves, excluded: plan.excluded || [] });
          return true;
        } catch (err) {
          const fresh = err.status === 409 ? planOf(err.payload) : null;
          if (!fresh) throw err;
          plan = fresh;
          view = bodyFor(plan, ctx, true);
          if (!view.ids.length) throw new Error("They are gone already; nothing is left to delete.");
          return { title: view.title, body: view.body };
        }
      },
    });
    if (!yes) return false;
    if (answer && answer.job) {
      mine.add(answer.job.id);
      statusOf.set(answer.job.id, answer.job.status);
    }
    App.grid.remove([...ctx.ids, ...(opts.also || []).map((c) => c.id)]);
    const what = noun(view.ids.length, ctx.videos);
    App.toast(`Moving ${what.startsWith("this ") ? what.replace("this ", "the ") : what} to the trash...`);
    App.status.poll();
    return true;
  }

  /* The Delete key: the open photo, else the selection, else the tile the
     keyboard is on. */
  function fromKeys() {
    if (!App.state.delete) return;
    if (App.viewer.isOpen()) { App.viewer.remove(); return; }
    if (App.state.view !== "grid") return;
    const sel = App.grid.selection();
    if (sel.length) { ask(sel); return; }
    const f = App.grid.focused();
    if (f) ask([f]);
  }

  /* "NAME: reason" lines from the job, as a short sentence for a toast. */
  function failures(job) {
    const lines = String(job.error || "").split("\n").map((s) => s.trim()).filter(Boolean);
    const p = job.progress || {};
    const n = Number(p.failed) || lines.length || 0;
    const head = n ? `${App.fmt.plural(n, "photo")} could not be deleted` : "The delete failed";
    return lines.length ? `${head}: ${lines.slice(0, 2).join("; ")}${lines.length > 2 ? "; ..." : ""}` : head;
  }

  const failed = (job) => job.status !== "done" || Number((job.progress || {}).failed) > 0;

  function finished(job) {
    mine.delete(job.id);
    if (failed(job)) {
      // The agent has unmarked what it could not delete; show it again.
      App.grid.load({ keepFocus: true });
      App.toast(failures(job), { error: true, ms: 9000 });
    } else {
      App.toast(job.message || "Moved to the trash");
    }
    App.shell.refresh(true);
  }

  /* Watch every delete job through the status poll, and act once when one
     that was running (or that this window started) has finished. A job first
     seen already finished is history from before this page and is left be. */
  function onStatus(s) {
    (s.jobs || []).filter((j) => j.kind === "delete").forEach((job) => {
      const before = statusOf.get(job.id);
      statusOf.set(job.id, job.status);
      const was = before !== undefined ? active(before) : mine.has(job.id);
      if (was && !active(job.status)) finished(job);
    });
  }

  function init() {
    App.bus.on("status", onStatus);
  }

  return { init, ask, fromKeys, failed };
})();
