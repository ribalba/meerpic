/* meerpic desktop shell.
 *
 * A thin Electron wrapper around the local meerpic server, the same shape as
 * meercal's and meerail's: it loads the web app in a native window, opens
 * outbound links in the system browser, and shows a retry screen if the server
 * is not running.
 *
 * It exists for one thing a browser tab cannot do: **drag a real file out of
 * the window.** Dragging a photo into a mail is the everyday use of this app,
 * and a web page can only offer the target a URL or the pixels it happens to
 * be showing (a 400 px thumbnail). A mail composer wants a file. Electron's
 * `webContents.startDrag` hands the desktop an actual file, which every mail
 * client, meerail included, accepts as an attachment.
 *
 * Point it at a non-default server with MEERPIC_URL:
 *   MEERPIC_URL=http://localhost:8041 npm start
 */
const { app, BrowserWindow, clipboard, ClipboardItem, ipcMain, nativeImage, shell, Menu } = require("electron");
const fs = require("fs");
const fsp = require("fs/promises");
const path = require("path");

const APP_URL = (process.env.MEERPIC_URL || "http://localhost:8040").replace(/\/+$/, "");
const APP_ORIGIN = new URL(APP_URL).origin;
const ICON = path.join(__dirname, "build", "icon.png");

let mainWindow = null;

function isInternal(targetUrl) {
  try {
    return new URL(targetUrl, APP_URL).origin === APP_ORIGIN;
  } catch {
    return false;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 920,
    minWidth: 760,
    minHeight: 520,
    backgroundColor: "#ffffff",
    title: "meerpic",
    icon: ICON,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // The bridge below: prepare, startDrag, copyImage. Nothing else of
      // Node or Electron reaches the page.
      preload: path.join(__dirname, "preload.js"),
      partition: "persist:meerpic",
    },
  });

  // The session is persistent, so its HTTP cache outlives a restart. The
  // server is local, so refetching the app's own scripts on launch costs
  // nothing, and a shell running half-old js against a half-new server fails
  // in confusing ways. (Photos and thumbnails are keyed by version and are
  // not affected by any of this.)
  const win = mainWindow;
  win.webContents.session.clearCache()
    .finally(() => { if (!win.isDestroyed()) win.loadURL(APP_URL); });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    // "Open original" is a link to this server: a window of its own inside
    // the app. Anything else goes to the system browser.
    if (isInternal(url)) return { action: "allow" };
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (e, url) => {
    if (!isInternal(url)) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });

  mainWindow.webContents.on("did-fail-load", (_e, errorCode, _desc, validatedURL) => {
    if (errorCode === -3 || !isInternal(validatedURL || APP_URL)) return;
    mainWindow.loadURL(errorPage());
  });

  trackForeground(mainWindow);
  mainWindow.on("closed", () => { mainWindow = null; });
}

/* Tell the page when the window stops being the one in front, so it can stand
 * its polling down (see app.power.js). The same signal meercal's shell sends,
 * by the same means, so the page code is shared in spirit if not in bytes. */
function signalForeground(win, state) {
  if (!win || win.isDestroyed()) return;
  win.webContents
    .executeJavaScript(`window.dispatchEvent(new Event("meerpic:${state}"))`)
    .catch(() => {});
}

function trackForeground(win) {
  win.on("focus", () => signalForeground(win, "focus"));
  win.on("blur", () => signalForeground(win, "blur"));
  win.on("show", () => signalForeground(win, "focus"));
  win.on("restore", () => signalForeground(win, "focus"));
  win.on("hide", () => signalForeground(win, "blur"));
  win.on("minimize", () => signalForeground(win, "blur"));
  win.webContents.on("did-finish-load",
    () => signalForeground(win, win.isFocused() ? "focus" : "blur"));
}

/* --- Dragging files out ------------------------------------------------------
 *
 * The page sends photo ids and nothing else. Which file a drag carries is
 * asked of the server here, in the main process: a page that could name a
 * path could name any file on this disk, and the page is the part that renders
 * text from photo metadata. The server says, per photo:
 *
 *   export_direct  the file to hand over is the original itself (a JPEG, an
 *                  H.264 mp4): drag it from where it lies, no copy at all.
 *   urls.export    otherwise, the mail-friendly version (HEIC -> JPEG, HEVC ->
 *                  H.264), downloaded once into a temp folder and dragged
 *                  from there.
 *
 * `prepare` is sent on pointerdown, before the pointer has moved far enough
 * to be a drag, so the download is usually finished by the time `startDrag`
 * arrives. If it is not, the drag starts when it is: the button is still down.
 */
const DRAG_DIR = path.join(app.getPath("temp"), "meerpic-drag");
const prepared = new Map();   // photo id -> Promise<absolute file path>

function safeName(name, id) {
  const cleaned = String(name || `photo-${id}`).replace(/[\/\\:*?"<>|\u0000-\u001f]/g, "_").slice(0, 180);
  return cleaned || `photo-${id}`;
}

async function fetchOk(session, url) {
  const response = await session.fetch(new URL(url, APP_URL).toString());
  if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ${url}`);
  return response;
}

async function resolveFile(session, id) {
  const detail = await (await fetchOk(session, `/api/photos/${id}`)).json();
  if (detail.export_direct && detail.path) {
    try {
      const st = await fsp.stat(detail.path);
      if (st.isFile()) return detail.path;
    } catch {
      // Not reachable from here (a server on another machine): fall through
      // to downloading it like any other.
    }
  }
  const dir = path.join(DRAG_DIR, String(id));
  await fsp.mkdir(dir, { recursive: true });
  // The file keeps its real name, because that is what the recipient sees on
  // the attachment; the folder per id is what keeps two IMG_0001.jpg apart.
  const target = path.join(dir, safeName(detail.export_name, id));
  if (fs.existsSync(target)) return target;
  const body = Buffer.from(await (await fetchOk(session, detail.urls.export)).arrayBuffer());
  const partial = `${target}.partial`;
  await fsp.writeFile(partial, body);
  await fsp.rename(partial, target);
  return target;
}

function idsOf(items) {
  return (Array.isArray(items) ? items : [items])
    .map((item) => Number(item && typeof item === "object" ? item.id : item))
    .filter((id) => Number.isInteger(id) && id > 0)
    .slice(0, 200);
}

function prepare(session, ids) {
  for (const id of ids) {
    if (!prepared.has(id)) {
      const job = resolveFile(session, id);
      prepared.set(id, job);
      // A failure is not kept: the next attempt should try again.
      job.catch(() => prepared.delete(id));
    }
  }
  return Promise.all(ids.map((id) => prepared.get(id)));
}

ipcMain.on("meerpic:prepare", (event, items) => {
  prepare(event.sender.session, idsOf(items)).catch(() => {});
});

ipcMain.on("meerpic:start-drag", async (event, items) => {
  const ids = idsOf(items);
  if (!ids.length) return;
  let files;
  try {
    files = await prepare(event.sender.session, ids);
  } catch (err) {
    console.warn(`drag: ${err.message}`);
    return;
  }
  const icon = nativeImage.createFromPath(ICON).resize({ width: 64, height: 64 });
  event.sender.startDrag({ file: files[0], files, icon });
});

/* Copy: the photo onto the clipboard as an image, for pasting into a mail
 * that takes pictures inline. The page asks for a size-capped export, and
 * nativeImage decodes JPEG, which is what the export is; the clipboard is
 * given PNG, the one image type every program that pastes understands.
 * Electron 44's clipboard is the W3C shape (write + ClipboardItem); the old
 * writeImage is gone. */
ipcMain.handle("meerpic:copy-image", async (event, url) => {
  if (!isInternal(url)) throw new Error("only this server's images can be copied");
  const body = Buffer.from(await (await fetchOk(event.sender.session, url)).arrayBuffer());
  const image = nativeImage.createFromBuffer(body);
  if (image.isEmpty()) throw new Error("not an image the clipboard can hold");
  const png = new Blob([image.toPNG()], { type: "image/png" });
  await clipboard.write([new ClipboardItem({ "image/png": png })]);
  return true;
});

/* Yesterday's drags are nobody's business: a temp folder of your photos
 * should not grow for as long as the app is installed. */
async function sweepDragDir(maxAgeMs = 24 * 3600 * 1000) {
  let entries = [];
  try {
    entries = await fsp.readdir(DRAG_DIR, { withFileTypes: true });
  } catch {
    return;
  }
  const cutoff = Date.now() - maxAgeMs;
  for (const entry of entries) {
    const full = path.join(DRAG_DIR, entry.name);
    try {
      const st = await fsp.stat(full);
      if (st.mtimeMs < cutoff) await fsp.rm(full, { recursive: true, force: true });
    } catch {
      // Gone already, or not ours to remove. Either way not worth a failure.
    }
  }
}

function errorPage() {
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8" />
    <style>
      body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;color:#1d1d1f}
      .card{text-align:center;max-width:440px;padding:2rem}
      h1{font-size:1.3rem;margin:0 0 .5rem}p{color:#57606a;line-height:1.5}
      button{font:inherit;font-weight:600;cursor:pointer;border:none;border-radius:8px;
        padding:.7rem 1.3rem;background:#1d6ff2;color:#fff;margin-top:1rem}
      code{background:#e6e8eb;padding:.15rem .4rem;border-radius:5px}
    </style></head><body><div class="card">
      <h1>Can't reach the meerpic server</h1>
      <p>Couldn't connect to <code>${APP_ORIGIN}</code>. Start it with
      <code>make up</code> in the repo and try again.</p>
      <button onclick="location.href='${APP_URL}'">Retry</button>
    </div></body></html>`;
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

function buildMenu() {
  const isMac = process.platform === "darwin";
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(isMac ? [{ role: "appMenu" }] : []),
    { role: "fileMenu" },
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { label: "Home", accelerator: "CmdOrCtrl+Shift+H",
          click: () => mainWindow && mainWindow.loadURL(APP_URL) },
        { role: "reload" }, { role: "forceReload" }, { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" },
        { role: "togglefullscreen" }, { role: "toggleDevTools" },
      ],
    },
    { role: "windowMenu" },
  ]));
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    buildMenu();
    createWindow();
    sweepDragDir();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
