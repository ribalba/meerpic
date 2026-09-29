/* Marking pictures safe: the NSFW classifier's flag, overruled by the reader.

   A photo marked safe is never flagged again, and a flagged photo that looks
   just like it (by the search vectors, see app/query.py safe_near) is not
   flagged either, so one mark clears a series of near-identical shots. The
   server answers with which other photos that cleared, or flagged again
   when a mark is taken back, so the page updates exactly those: in is:nsfw
   what is no longer flagged leaves the list at once; everywhere else the
   tiles are repainted, blur off or on. */

window.App = window.App || {};

App.safe = (() => {
  const inNsfw = () => /(^|\s)is:nsfw(\s|$)/.test(App.state.q || "");

  function said(n, safe, answer) {
    const what = n === 1 ? "photo" : App.fmt.plural(n, "photo");
    const others = safe ? answer.cleared.length : answer.flagged.length;
    const head = safe ? `Marked ${n === 1 ? "the photo" : what} safe` : `Took the safe mark back from ${n === 1 ? "the photo" : what}`;
    if (!others) return head;
    return safe
      ? `${head}; ${App.fmt.plural(others, "lookalike")} ${others === 1 ? "is" : "are"} not flagged any more`
      : `${head}; ${App.fmt.plural(others, "lookalike")} ${others === 1 ? "is" : "are"} flagged again`;
  }

  /* Mark these cards safe, or take the mark back. Resolves the server's
     answer ({changed, cleared, flagged}), or null when it failed. */
  async function mark(cards, safe = true) {
    cards = (cards || []).filter(Boolean);
    if (!cards.length) return null;
    const ids = cards.map((c) => c.id);
    let answer;
    try {
      answer = await App.api.post("/api/photos/safe", { ids, safe });
    } catch (err) {
      App.toast(`Could not ${safe ? "mark that safe" : "take the mark back"}: ${err.message}`, { error: true, ms: 7000 });
      return null;
    }
    answer.cleared = answer.cleared || [];
    answer.flagged = answer.flagged || [];
    App.viewer.forget([...ids, ...answer.cleared, ...answer.flagged]);
    if (inNsfw()) {
      // Marked, or cleared with it: not in this list any more. A mark taken
      // back brings photos in, which only a fresh load can place.
      if (safe) App.grid.remove([...ids, ...answer.cleared]);
      else App.grid.load({ keepFocus: true });
    }
    App.grid.refresh();
    App.shell.refresh(true);
    App.toast(said(cards.length, safe, answer));
    return answer;
  }

  /* The key: the open photo, else the selection, else the tile the keyboard
     is on. Marks, unless every one of them is marked already: then it takes
     the marks back. */
  function fromKeys() {
    if (!(App.state.nsfw && App.state.nsfw.enabled)) return;
    if (App.viewer.isOpen()) { App.viewer.markSafe(); return; }
    if (App.state.view !== "grid") return;
    const sel = App.grid.selection();
    const cards = sel.length ? sel : [App.grid.focused()].filter(Boolean);
    if (!cards.length) return;
    mark(cards, !cards.every((c) => c.nsfw_safe));
  }

  return { mark, fromKeys, inNsfw };
})();
