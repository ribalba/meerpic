/* meerpic theme: light, dark, or whatever the system says.

   Loaded from <head>, ahead of every other module: the attribute has to be on
   <html> before the first paint, or a forced-light window on a dark machine
   comes up dark for a frame. Shares its shape with meercal's and meerail's,
   deliberately: the three run side by side and should not disagree about what
   dark means. */

window.App = window.App || {};

App.theme = (() => {
  const KEY = "meerpic.theme";
  const MODES = ["system", "light", "dark"];

  // Storage can refuse outright (a private window); a theme is not worth a
  // page that will not start.
  function mode() {
    let saved = null;
    try { saved = localStorage.getItem(KEY); } catch (e) { /* refused */ }
    return MODES.includes(saved) ? saved : "system";
  }

  // The CSS carries both palettes and picks between them with color-scheme;
  // all this does is say which. No attribute means "let the OS decide".
  function apply(m) {
    if (m === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", m);
  }

  function set(m) {
    if (!MODES.includes(m)) m = "system";
    try { localStorage.setItem(KEY, m); } catch (e) { /* refused */ }
    apply(m);
  }

  function cycle() {
    set(MODES[(MODES.indexOf(mode()) + 1) % MODES.length]);
    return mode();
  }

  apply(mode());
  return { mode, set, cycle, MODES };
})();
