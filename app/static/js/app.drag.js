/* Getting a photo out of the window: drag, copy, download.

   This is the program's first job. The usual reason to open it is "the photo I
   took ten minutes ago, into this email", so the path from the grid to another
   application has to be one gesture, and what arrives has to be something a
   mail client takes without complaint: a JPEG of a sensible size rather than a
   40 MB HEIC. The server makes that file (/media/export/{id}); this module only
   hands it over.

   Two worlds:

   * **The desktop shell** (`window.meerpicDesktop`, only in the Electron app)
     can start a real operating-system file drag. Electron's startDrag needs
     the file on disk *before* the drag starts, so a pointerdown on a tile asks
     the shell to `prepare` the export (it downloads it in the background), and
     the dragstart hands over to `startDrag`. For files the server would send
     unchanged (`export_direct`) the shell drags the original path instead.
   * **A plain browser** can only describe a download. Chromium understands the
     `DownloadURL` drag type and turns a drop on the desktop or a file manager
     into a download; everything else gets the URL as text/uri-list. */

window.App = window.App || {};

App.drag = (() => {
  const desktop = () => window.meerpicDesktop || null;

  // What the shell is given. `url` is the export's path as the card has it;
  // the shell resolves it against the page it is showing.
  function item(card) {
    return {
      id: card.id,
      url: card.export,
      name: card.export_name,
      path: card.path,
      direct: Boolean(card.export_direct),
    };
  }

  const MIME = {
    jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", gif: "image/gif",
    webp: "image/webp", avif: "image/avif", heic: "image/heic", heif: "image/heif",
    tif: "image/tiff", tiff: "image/tiff", dng: "image/x-adobe-dng",
    mp4: "video/mp4", mov: "video/quicktime", m4v: "video/x-m4v", "3gp": "video/3gpp",
    mpg: "video/mpeg", mpeg: "video/mpeg", avi: "video/x-msvideo", mkv: "video/x-matroska",
    webm: "video/webm",
  };

  function mimeOf(name) {
    const ext = String(name || "").split(".").pop().toLowerCase();
    return MIME[ext] || "application/octet-stream";
  }

  // DownloadURL is "mime:name:url", split on the first two colons; a colon in
  // the name would move the URL. The same goes for the separators the file
  // systems at the other end refuse.
  const safeName = (name) => String(name || "photo").replace(/[:/\\]/g, "_");

  function prepare(cards) {
    const d = desktop();
    if (!d || typeof d.prepare !== "function" || !cards.length) return;
    try { d.prepare(cards.map(item)); } catch (e) { console.error("prepare failed", e); }
  }

  /* A little card under the pointer saying how many are coming, for a drag of
     several. The browser draws a single tile well enough on its own. */
  function ghost(e, count) {
    if (count < 2 || !e.dataTransfer.setDragImage) return;
    const el = App.el("div", { class: "drag-ghost", text: `${count} photos` });
    document.body.append(el);
    e.dataTransfer.setDragImage(el, -10, -10);
    setTimeout(() => el.remove(), 0);
  }

  function start(e, cards) {
    if (!cards.length) return;
    const d = desktop();
    if (d && typeof d.startDrag === "function") {
      e.preventDefault();
      try { d.startDrag(cards.map(item)); } catch (err) { console.error("startDrag failed", err); }
      return;
    }
    const dt = e.dataTransfer;
    if (!dt) return;
    const first = cards[0];
    const urls = cards.map((c) => App.abs(c.export));
    dt.effectAllowed = "copy";
    // Only one file per DownloadURL, which is a Chromium limit and not ours.
    // The rest still travel as links.
    dt.setData("DownloadURL", `${mimeOf(first.export_name)}:${safeName(first.export_name)}:${urls[0]}`);
    dt.setData("text/uri-list", urls.join("\r\n"));
    dt.setData("text/plain", urls.join("\n"));
    ghost(e, cards.length);
  }

  /* --- copy -----------------------------------------------------------------

     A PNG on the clipboard, because that is the one image type every paste
     target accepts; the long edge capped at 2048 so a paste into a mail is not
     a 30 MB attachment. The server does the resize (and the HEIC decoding);
     the canvas only re-encodes. A video copies its frame. */
  const COPY_EDGE = 2048;

  function copyUrl(card) {
    return card.kind === "video" ? card.thumb : `/media/export/${card.id}?max=${COPY_EDGE}`;
  }

  async function toPng(url) {
    const response = await fetch(url);
    if (response.status === 401) {
      await App.api.promptLogin();
      return toPng(url);
    }
    if (!response.ok) throw new Error(`Could not fetch the image (${response.status})`);
    const bitmap = await createImageBitmap(await response.blob());
    const scale = Math.min(1, COPY_EDGE / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    return new Promise((resolve, reject) => canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("Could not encode the image"))), "image/png"));
  }

  async function copy(card) {
    if (!card) return;
    const url = copyUrl(card);
    if (!url) { App.toast("Nothing to copy yet: the thumbnail is still being made", { error: true }); return; }
    const d = desktop();
    const browserCan = Boolean(navigator.clipboard && window.ClipboardItem && window.isSecureContext);
    try {
      // The shell's clipboard takes JPEG and PNG, which the export is; a
      // video's frame is the WebP thumbnail, which the page re-encodes itself
      // whenever the browser's own clipboard is there to take it.
      if (d && typeof d.copyImage === "function" && !(card.kind === "video" && browserCan)) {
        await d.copyImage(url);
      } else {
        if (!navigator.clipboard || !window.ClipboardItem) {
          // Clipboard images need a secure context: https, localhost, or the
          // desktop app. Saying so beats a button that silently does nothing.
          App.toast("Copying images needs https or the desktop app. Drag the photo instead.", { error: true });
          return;
        }
        // The promise goes into the ClipboardItem unresolved, so the write
        // starts inside the keypress or click that asked for it; awaiting the
        // fetch first would spend the user activation and be refused.
        await navigator.clipboard.write([new ClipboardItem({ "image/png": toPng(url) })]);
      }
      App.toast(card.kind === "video" ? "Copied the frame" : "Copied");
    } catch (err) {
      App.toast(`Could not copy: ${err.message}`, { error: true });
    }
  }

  /* --- download -------------------------------------------------------------

     A link click, not a fetch: the server marks both as attachments, so the
     browser saves them through its own download machinery, with its progress
     and its "show in folder", and the page stays where it is. */
  function save(url, name) {
    const a = App.el("a", { href: url, download: name || "" });
    document.body.append(a);
    a.click();
    a.remove();
  }

  function download(card, original) {
    if (!card) return;
    if (original) save(`/media/original/${card.id}?download=1`, card.name);
    else save(card.export, card.export_name);
  }

  /* Several at once. Spaced out, because a browser treats a burst of downloads
     from one click as something to ask about, and asks once per burst. */
  function downloadMany(cards, original) {
    cards.forEach((card, i) => setTimeout(() => download(card, original), i * 350));
    if (cards.length > 1) App.toast(`Downloading ${cards.length} files`);
  }

  return { prepare, start, copy, download, downloadMany, item, mimeOf };
})();
