/* Videos in the grid, moving under the pointer.

   A grid of video thumbnails is a grid of first seconds, and the first second
   of a clip is rarely what it is of. So resting the mouse on a video tile for
   a moment shows the clip in place, without opening anything:

   * with a preview (the H.264 copy the viewer plays), a muted, looping
     <video> over the tile, the thumbnail or the storyboard showing until it
     actually plays;
   * without one, the storyboard: the agent's strip of frames from across the
     clip (`card.story`, `card.story_frames` side by side in one WEBP), the
     frame under the pointer's x shown, so moving across the tile scrubs
     through the video. A thin line along the bottom says where in it.

   Nothing is fetched until the pointer has rested (a sweep across the grid
   must not start forty downloads), one tile moves at a time, and leaving the
   tile puts the thumbnail back and stops and drops the video. Only for a
   real mouse: a finger has no hover, and a tap opens the viewer anyway.

   `App.story` is the arithmetic for showing one frame of a strip in a box,
   shared with the viewer's filmstrip. */

window.App = window.App || {};

App.story = {
  /* Load a strip; resolves { url, w, h, n } with its natural size, or null.
     The image is kept by the caller so a hover that ends can abandon it. */
  load(url, frames, img = new Image()) {
    return new Promise((resolve) => {
      img.onload = () => resolve({ url, w: img.naturalWidth, h: img.naturalHeight, n: Math.max(1, frames) });
      img.onerror = () => resolve(null);
      img.src = url;
    });
  },

  /* Frame `k` of `strip` as the background of `el`, a box of W x H, cropped
     to cover it (`contain` letterboxes it instead). Every frame of a strip is
     the same width, the strip's width over its frame count. */
  paint(el, strip, k, W, H, fit = "cover") {
    if (!strip || !W || !H) return;
    const fw = strip.w / strip.n;
    const fh = strip.h;
    const s = fit === "contain" ? Math.min(W / fw, H / fh) : Math.max(W / fw, H / fh);
    const i = Math.max(0, Math.min(strip.n - 1, k));
    if (el.dataset.strip !== strip.url) {
      el.style.backgroundImage = App.cssUrl(strip.url);
      el.dataset.strip = strip.url;
    }
    el.style.backgroundSize = `${strip.w * s}px ${strip.h * s}px`;
    el.style.backgroundPosition = `${-(i * fw * s) + (W - fw * s) / 2}px ${(H - fh * s) / 2}px`;
  },

  /* Which frame a pointer at clientX over `rect` is on. */
  frameAt(strip, rect, x) {
    const f = rect.width ? (x - rect.left) / rect.width : 0;
    return { k: Math.floor(Math.max(0, Math.min(0.9999, f)) * strip.n), f: Math.max(0, Math.min(1, f)) };
  },
};

App.hover = (() => {
  const DELAY = 250;
  // A mouse, or a trackpad: something that can rest over a tile without
  // pressing it.
  const fine = window.matchMedia("(hover: hover) and (pointer: fine)");

  let cur = null;           // { tile, card, x, timer, layer, bar, video, strip, img, raf }

  function begin(tile, card, x) {
    cur = { tile, card, x, timer: 0, layer: null, bar: null, video: null, strip: null, img: null, raf: 0 };
    const me = cur;
    me.timer = setTimeout(() => { if (cur === me) show(me); }, DELAY);
  }

  function show(me) {
    const { tile, card } = me;
    me.layer = App.el("div", { class: "tile-motion", "aria-hidden": "true" });
    me.bar = App.el("span", { class: "tile-progress" });
    me.layer.append(me.bar);
    tile.append(me.layer);
    tile.classList.add("moving");

    if (card.story && card.story_frames > 0) {
      me.img = new Image();
      App.story.load(card.story, card.story_frames, me.img).then((strip) => {
        if (cur !== me || !strip) return;
        me.strip = strip;
        me.layer.classList.add("scrubbing");
        scrub(me);
      });
    }

    if (card.preview) {
      const v = App.el("video", {
        class: "tile-video", muted: true, loop: true, playsinline: true, autoplay: true, preload: "auto",
        disablepictureinpicture: true, disableremoteplayback: true, tabindex: "-1",
      });
      // The attribute alone does not mute an element made from script.
      v.muted = true;
      v.defaultMuted = true;
      v.addEventListener("playing", () => {
        if (cur !== me) return;
        v.classList.add("on");
        me.layer.classList.add("playing");
        tick(me);
      });
      // A clip this browser cannot play: the storyboard, if any, stays.
      v.addEventListener("error", () => { if (cur === me) { v.remove(); me.video = null; } });
      v.src = card.preview;
      me.video = v;
      me.layer.insertBefore(v, me.bar);
      v.play().catch(() => { /* muted autoplay may still be refused; the storyboard stays */ });
    }
  }

  /* The storyboard, following the pointer. Not once the video plays: then
     the line follows the video instead. */
  function scrub(me) {
    if (!me.strip || !me.layer || me.layer.classList.contains("playing")) return;
    const rect = me.tile.getBoundingClientRect();
    const { k, f } = App.story.frameAt(me.strip, rect, me.x);
    App.story.paint(me.layer, me.strip, k, rect.width, rect.height);
    me.bar.style.width = `${f * 100}%`;
  }

  function tick(me) {
    cancelAnimationFrame(me.raf);
    const step = () => {
      if (cur !== me || !me.video) return;
      const d = me.video.duration;
      if (Number.isFinite(d) && d > 0) me.bar.style.width = `${(me.video.currentTime / d) * 100}%`;
      me.raf = requestAnimationFrame(step);
    };
    me.raf = requestAnimationFrame(step);
  }

  function stop() {
    if (!cur) return;
    const me = cur;
    cur = null;
    clearTimeout(me.timer);
    cancelAnimationFrame(me.raf);
    if (me.video) {
      // Emptying the source is what actually stops the download.
      me.video.pause();
      me.video.removeAttribute("src");
      me.video.load();
    }
    if (me.img) { me.img.onload = null; me.img.onerror = null; me.img.removeAttribute("src"); }
    if (me.layer) me.layer.remove();
    me.tile.classList.remove("moving");
  }

  /* Listen on the grid's body. `cardOf(tile)` gives a tile's card. */
  function attach(container, cardOf) {
    container.addEventListener("pointerover", (e) => {
      if (e.pointerType !== "mouse" || !fine.matches) return;
      const tile = e.target.closest(".tile");
      if (cur && cur.tile === tile) return;
      stop();
      if (!tile || e.buttons) return;
      const card = cardOf(tile);
      if (!card || card.kind !== "video" || !(card.preview || (card.story && card.story_frames > 0))) return;
      begin(tile, card, e.clientX);
    });
    container.addEventListener("pointerout", (e) => {
      // Moving between the tile's own parts (the picture, a badge) is not
      // leaving it.
      if (!cur || (e.relatedTarget && cur.tile.contains(e.relatedTarget))) return;
      stop();
    });
    container.addEventListener("pointermove", (e) => {
      if (!cur || e.pointerType !== "mouse") return;
      cur.x = e.clientX;
      scrub(cur);
    });
    // A press is a click or the start of a drag; neither wants a video
    // running under it.
    container.addEventListener("pointerdown", () => stop());
  }

  return {
    attach, stop,
    /* The tile is being repainted or dropped: let go of it first. */
    release(tile) { if (cur && cur.tile === tile) stop(); },
    active: () => (cur ? cur.card.id : null),
  };
})();
