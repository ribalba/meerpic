/* The search bar. Types like meercal's filter bar and meerail's search, on
   purpose, with one difference that matters: here the bar *is* the state.

   Whatever is looked at (the whole library, the videos, "cows", the photos
   inside a box on the map, the pictures like this one) is a line of text in
   this bar, and every other control only ever writes into it: the sidebar's
   Videos is `is:video`, the map's Show in grid is `bbox:...`, the viewer's
   Find similar is `similar:123`. So the reader learns the language by using
   the app, the address bar carries it (?q=), and there is no second place
   where a filter can hide.

   Typing searches after a pause; Enter searches at once. What the server did
   not understand comes back in `query.errors` and is shown under the bar,
   quietly, because the rest of the query still ran. Words that match a place
   name offer that place as a chip, since "potsdam" is much better answered by
   where the photos were taken than by what they look like. */

window.App = window.App || {};

App.search = (() => {
  const DEBOUNCE = 350;

  let input = null;
  let clearBtn = null;
  let sortSwitch = null;
  let extra = null;
  let timer = null;
  let placeSeq = 0;
  let places = [];

  function submit(value, push) {
    clearTimeout(timer);
    App.shell.setQuery(value, { push: Boolean(push), fromBar: true });
  }

  /* Put text into the bar without searching: the shell does this when
     something else changed the query. */
  function set(value) {
    if (input && input.value.trim() !== value) input.value = value;
    if (clearBtn) clearBtn.hidden = !value;
  }

  // --- what came back ------------------------------------------------------------

  function paintExtra(info) {
    const nodes = [];
    const q = info && info.query;
    if (q && q.errors && q.errors.length) {
      nodes.push(App.el("span", { class: "filter-errors", text: `Ignored: ${q.errors.join("; ")}` }));
    }
    if (q && q.text && App.state.search && App.state.search.ready === false) {
      nodes.push(App.el("span", { class: "filter-note", text: "The search model is still loading; word matches will appear once it is ready." }));
    }
    places.forEach((p) => {
      const name = p.label;
      nodes.push(App.el("button", {
        class: "place-chip", type: "button", title: p.place,
        onclick: () => {
          // The words become the place: `potsdam` -> `in:"Potsdam"`, and
          // any filters already typed stay.
          const kept = App.query.withoutWords(App.state.q);
          submit(App.query.set(kept, "in", name), true);
        },
      },
        App.el("span", { class: "chip-icon", html: App.icon("pin", 13) }),
        App.el("span", { text: `Photos in ${name}` }),
        App.el("span", { class: "chip-n", text: App.fmt.n(p.count) }),
      ));
    });
    extra.replaceChildren(...nodes);
  }

  function paintSort(info) {
    // Relevance only exists for words, similarity and faces. Without them the
    // grid is in date order already and the switch would be a control for nothing.
    const scored = info && (info.mode === "score" || (info.query && (info.query.text || info.query.similar || info.query.face)));
    sortSwitch.hidden = !scored;
    if (!scored) return;
    const byDate = App.query.get(App.state.q, "sort") === "date";
    sortSwitch.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", (b.dataset.sort === "date") === byDate);
    });
  }

  /* Places whose name contains the words. Only for words, and only a few:
     this is a suggestion under the bar, not a second result list. */
  async function findPlaces(info) {
    const mine = ++placeSeq;
    const words = info && info.query && !info.query.similar && !info.query.face ? (info.query.text || "").trim() : "";
    if (words.length < 3 || App.query.get(App.state.q, "in")) {
      places = [];
      paintExtra(info);
      return;
    }
    let found = [];
    try {
      const payload = await App.api.get(`/api/places?${new URLSearchParams({ q: words, limit: "5" })}`);
      found = payload.places || [];
    } catch (e) { /* suggestions are optional */ }
    if (mine !== placeSeq) return;
    // Named by the town, which is what `in:` matches on; by the whole place
    // when two towns of the same name turned up.
    const names = found.map((p) => p.city || p.region || p.country || p.place);
    places = found.map((p, i) => ({
      ...p, label: names.filter((n) => n === names[i]).length > 1 ? p.place : names[i],
    })).filter((p) => p.label);
    paintExtra(info);
  }

  function onGrid(info) {
    paintSort(info);
    paintExtra(info);
    findPlaces(info);
  }

  // --- the help modal ------------------------------------------------------------

  function helpOpen() { return !document.getElementById("filter-help-modal").hidden; }
  function openHelp() { document.getElementById("filter-help-modal").hidden = false; }
  function closeHelp() { document.getElementById("filter-help-modal").hidden = true; }

  function init() {
    input = document.getElementById("filter-input");
    clearBtn = document.getElementById("filter-clear");
    sortSwitch = document.getElementById("sort-switch");
    extra = document.getElementById("filter-extra");
    // The full example list does not fit a phone's bar; the short one does.
    if (window.matchMedia("(max-width: 600px)").matches) input.placeholder = "Search: cows, in:Potsdam, is:video";

    input.addEventListener("input", () => {
      clearTimeout(timer);
      clearBtn.hidden = !input.value.trim();
      timer = setTimeout(() => submit(input.value.trim(), false), DEBOUNCE);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(input.value.trim(), true); }
      if (e.key === "Escape") {
        // First Esc clears what was typed; the second leaves the bar.
        e.preventDefault();
        e.stopPropagation();
        if (input.value) { input.value = ""; submit("", true); } else input.blur();
      }
    });
    clearBtn.onclick = () => { input.value = ""; submit("", true); input.focus(); };
    sortSwitch.querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        const q = b.dataset.sort === "date" ? App.query.set(App.state.q, "sort", "date") : App.query.remove(App.state.q, "sort");
        submit(q, false);
      };
    });

    const help = document.getElementById("filter-help-modal");
    document.getElementById("filter-help-btn").onclick = openHelp;
    document.getElementById("filter-help-close").onclick = closeHelp;
    help.addEventListener("click", (e) => { if (e.target === help) closeHelp(); });

    App.bus.on("grid", onGrid);
  }

  return { init, set, focus: () => { input.focus(); input.select(); }, helpOpen, openHelp, closeHelp };
})();
