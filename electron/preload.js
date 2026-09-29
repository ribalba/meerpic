/* The bridge between the page and the desktop shell: three calls, all about
 * handing a photo to another program.
 *
 * Only photo ids cross it. Which file that means is decided in main.js by
 * asking the server, never by trusting a path the page supplies; see the
 * comment on dragging there. The page detects the shell by the presence of
 * `window.meerpicDesktop` and works without it.
 */
const { contextBridge, ipcRenderer } = require("electron");

function ids(items) {
  return (Array.isArray(items) ? items : [items])
    .map((item) => (item && typeof item === "object" ? item.id : item));
}

contextBridge.exposeInMainWorld("meerpicDesktop", {
  version: 1,
  // Start fetching what a drag of these would carry, before it is a drag.
  prepare: (items) => ipcRenderer.send("meerpic:prepare", ids(items)),
  // Called from the page's dragstart, after preventDefault().
  startDrag: (items) => ipcRenderer.send("meerpic:start-drag", ids(items)),
  // An image URL on this server onto the system clipboard.
  copyImage: (url) => ipcRenderer.invoke("meerpic:copy-image", String(url)),
});
