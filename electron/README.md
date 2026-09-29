# meerpic desktop

A thin Electron wrapper that opens the meerpic web app in a native window. It
needs the **server running** (`make up` in the repo root) and the **agent**
indexing your library for anything to appear in it.

The same shape as meercal's and meerail's desktop shells on purpose: the three
live on one desktop and should behave the same way.

## Why it exists

To **drag a real file out of the window.** The everyday use of meerpic is "the
photo I just took, into this mail". A browser tab can only hand another program
a URL or the pixels it is drawing (a thumbnail); a mail composer, meerail's
included, wants a file. The shell uses Electron's `webContents.startDrag`, which
gives the desktop an actual file:

- a JPEG or an H.264 video is dragged **from where it lies** in your library,
  no copy made;
- a HEIC is dragged as a **JPEG** (and an HEVC video as the H.264 copy the agent
  made), because the person you are writing to may not be on an Apple device.
  The server converts it; the shell keeps it in a temp folder for a day.

The page asks for this by photo id only. Which file a drag carries is looked up
by the shell itself, from the server, so a page can never name an arbitrary
file on your disk. See `main.js`.

**Copy** puts the photo on the clipboard as an image (at most 2048 px), for
mail clients that take pasted pictures inline.

## Run in development

```bash
cd electron
npm install
npm start                                   # loads http://localhost:8040
MEERPIC_URL=http://localhost:8041 npm start # the dev stack, or a remote server
```

Dragging the original from its place on disk needs the shell to run on the
machine that holds the library; against a remote server every drag is a
download into the temp folder first, which works the same, only slower.

## Build and install

```bash
npm run dist        # -> dist/  (Linux .AppImage/.deb, macOS .dmg/.zip, Windows .exe)
make distinstall    # build, then register with the desktop (KDE / GNOME / macOS)
make distuninstall  # remove it again
```

## What else the shell adds over a browser tab

- **A window of its own**, with the app's icon in the dock and the task bar.
- **Foreground signalling**: `meerpic:focus` / `meerpic:blur` events, so the
  page stands its status polling down while it is behind another window.
- **Outbound links** (OpenStreetMap's attribution) open in the system browser.
- **A retry screen** when the server is not up, instead of a blank window.

The app icon is `build/icon.png` (1024x1024).
