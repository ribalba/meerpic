/* Stand the app down while it is not the one in front.

   meerpic in the background costs one status poll every twenty seconds (every
   two while a sync or the indexer is busy), so this module is small. It exists
   as much for coming *back*: a library left open overnight behind a browser is
   showing yesterday's photos, and the agent has been syncing all the while. The
   honest thing on return is to go and ask again rather than trust the screen.

   Two signals feed it, because neither covers the other:

     - `visibilitychange` is the portable one and the only one a browser has. It
       fires for a minimised window or a backgrounded tab. It does not fire for
       a window that is merely behind another, which is the common case on a
       desktop.
     - The desktop shell reports the window's focus directly (it dispatches
       `meerpic:blur` / `meerpic:focus` on window). That is what "the app in
       front" actually means.

   In a plain browser tab only the first of those ever fires, and everything
   still works; the module simply stands down less often. */

window.App = window.App || {};

App.power = (() => {
  /* How long a focus loss has to last before standing down. Alt-tabbing to the
     mail client to drop a photo in and straight back is exactly this program's
     main use, and resuming refetches the status, so flapping would cost more
     than the polling it saves. Losing *visibility* skips the wait: there is
     nothing on screen to interrupt behind a minimised window. */
  const GRACE = 3000;

  let suspended = false;
  let focused = true;      // the shell tells us; a plain browser leaves it true
  let timer = null;
  const hooks = { suspend: [], resume: [] };

  function run(fn) {
    // One hook throwing must not strand the others, and above all must not
    // leave the app stood down over a window somebody is looking at.
    try { fn(); } catch (e) { console.error("power hook failed", e); }
  }

  function background() { return document.hidden || !focused; }

  function suspend() {
    clearTimeout(timer);
    timer = null;
    // Re-checked rather than assumed: the grace timer was set on a signal that
    // may since have been undone.
    if (suspended || !background()) return;
    suspended = true;
    hooks.suspend.forEach(run);
  }

  function resume() {
    clearTimeout(timer);
    timer = null;
    if (!suspended) return;
    suspended = false;
    hooks.resume.forEach(run);
  }

  function defer() {
    clearTimeout(timer);
    if (!background()) return;
    timer = setTimeout(suspend, GRACE);
  }

  function init() {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) suspend();
      else { focused = true; resume(); }
    });
    window.addEventListener("meerpic:blur", () => { focused = false; defer(); });
    window.addEventListener("meerpic:focus", () => { focused = true; resume(); });

    // Any use of the app is a wake, and on some setups the first click reaches
    // the page ahead of the shell's focus event. Cheap while running:
    // `suspended` is false and it returns.
    const wake = () => { if (suspended) { focused = true; resume(); } };
    document.addEventListener("mousedown", wake, true);
    document.addEventListener("keydown", wake, true);
  }

  return {
    init,
    /* Called when the app stands down: stop timers, and expect to be told to
       start again rather than polling for it. */
    whenSuspended: (fn) => hooks.suspend.push(fn),
    /* Called when it comes back. Assume everything on screen is stale. */
    whenResumed: (fn) => hooks.resume.push(fn),
    isSuspended: () => suspended,
  };
})();
