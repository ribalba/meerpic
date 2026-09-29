# Website screenshots

The images on meerpic.com are generated, not taken by hand, and not taken of
anybody's photos:

```bash
make -C website screenshots                          # everything, from nothing
make -C website shoot SHOOT_ARGS="--only map text"   # just those, stack already up
make -C website demo-down                            # when finished
```

`shoot.py` needs a Python with Playwright and its Chromium, `../.venv` by
default:

```bash
.venv/bin/pip install playwright && .venv/bin/playwright install chromium
make -C website screenshots SHOOT_PY=/path/to/other/python   # or bring your own
```

Output goes to `website/public/img/screenshots/` as WebP at 2880x1800 (1440x900
at 2x), which is what `index.html` serves, plus `grid-dark.webp` for the page's
dark mode and `credits.txt` for the photos.

## Why it works this way

The shots are of **meerpic itself, built from this checkout**, driven in a
real browser against a real Postgres, over files the real agent indexed: dates
from EXIF, places from GeoNames, vectors from SigLIP 2, text from the OCR
stage, Live Photos paired by their `ContentIdentifier`. Nothing is mocked, so a
feature that works in a screenshot is a feature that works.

It runs as a **stack of its own**, `meerpic-demo` (`docker-compose.yml` here):
its own containers, its own image tags (`meerpic-server:demo`,
`meerpic-agent:demo`, so the `:local` images a running meerpic uses are never
replaced), its own database volume, port 18040, and `MEERPIC_CONFIG=""` so no
`meerpic.toml` is read. Nothing of yours is mounted: not `~/Pictures`, not
`~/.cache/meerpic`, not your `rclone.conf`. The one thing borrowed is the
models folder of an existing `~/.cache/meerpic`, copied once (a reflink on
btrfs, so no space and no time) to save a 2.6 GB download; without one the
agent downloads its models as usual.

Everything the demo writes lives under `DEMO_DIR` (default
`~/.cache/meerpic-demo`): the downloaded originals, the built library, the
cache. Nothing in it is precious and all of it can be rebuilt.

## The demo library

`demo_photos.py` is the whole library as data: about eighty pictures arranged
as a year of somebody's photos, with where and when each was taken and on
what. The last few days at home near Potsdam, a day in Berlin, a summer on the
Baltic, a week in Lisbon, winter, spring, and a hike in the Alps the year
before. Two library roots, as someone with a phone and a camera has:

| Root | What | Files |
| --- | --- | --- |
| `icloud` | what `rclone copy` of iCloud Photos leaves: one flat folder | `IMG_7301.HEIC` ... HEIC stills, HEVC `.MOV` videos, Live Photo pairs, an `-edited.heic`, two `PNG` screenshots, WhatsApp saves with random names and no metadata |
| `camera` | camera imports, a folder per trip | `2025-09 Alps/DSC04480.JPG` (Sony), `2026-05 Lisbon/DSCF2104.JPG` (Fujifilm) |

Every picture is a **CC0 photo from StockSnap** (`SOURCES`, with the
photographer and the photo's page; `credits.txt` is written from it). The
960 px rendition is cropped to the camera's aspect and scaled up to its size,
so the grid and the info panel look like a real library; nobody should
mistake them for 12 megapixels. The videos are slow push-ins on a still,
encoded HEVC the way an iPhone records. The two screenshots in the library are
of this website at iPhone size (`shoot.py --phone`).

Dates are relative to the day the library is built: the newest photo is from
this morning, so "Today" and "Yesterday" are always there. The seasonal sets
are pinned to their month instead (the beach in August, the snow in January),
at the most recent such date at least three weeks back.

Favourites and albums are not in any file; the real agent reads them from
iCloud after a sync. The demo has no iCloud, so `seed.py` writes the same rows
that refresh would, from `demo_photos.py`, once the agent has finished.

## The pieces

| File | Runs | Does |
| --- | --- | --- |
| `demo_photos.py` | everywhere | The library as data: sources, places, cameras, dates, names. Standard library only. |
| `build_library.py` | agent image | Downloads the sources, builds the files, writes their metadata with exiftool, writes `credits.txt`. Rebuilds what was built for another day; `--force` rebuilds everything. |
| `docker-compose.yml` | Docker | The `meerpic-demo` stack. |
| `seed.py` | agent container | Waits until every file is indexed (size and time matching the disk, every stage done), then writes favourites and albums. Refuses any database but `meerpic_demo`. |
| `shoot.py` | host | Drives Chromium through Playwright; `SHOTS` maps each name to its capture function. `--phone` renders the library's two screenshots. |

## Things that bite

Each of these cost a round of debugging and is handled in code.

- **exiftool's `-Orientation=1` writes 3.** Without `#`, exiftool reads the
  value against its descriptions and lands on "Rotate 180". The builder writes
  `-Orientation#=1`.
- **Same size, same time, same photo.** meerpic knows a file by path, size and
  modification time. A rebuilt file with other bytes but the same size would
  keep its old thumbnail and metadata, so the builder puts a hash of the
  content into the sub-second part of the file time.
- **Adding a picture renames the others.** iPhone files are numbered in the
  order they were taken (on a fixed reference day, `NUMBERING_DAY`, so the
  build day does not reorder them). A new item anywhere but at the newest end
  shifts every name after it, and the old names stay behind in `DEMO_DIR` and
  in the shots. The builder lists such files with the command that would
  remove them; it deletes nothing itself.
- **A Live Photo needs an Apple maker note.** exiftool writes Apple's
  `ContentIdentifier` only into a maker note that already exists, which only a
  phone's file has. The builder makes the smallest one that carries it.
- **x265 takes every core, per encode.** The builder runs a quarter of the
  cores, at low priority, with x265's thread pool off.
- **The grid's row height is a server-side setting** (`/api/prefs`).
  `shoot.py` writes it before every run, so a shot does not depend on what the
  last run, or a visitor to the demo stack, pressed.
- **The phone screenshots need HTTP.** The page's paths are absolute
  (`/css/site.css`), so `file://` renders it unstyled; `shoot.py --phone`
  serves `public/` on a throwaway port.

## Adding a shot

Write a function in `shoot.py`, decorate it with `@register`, and add a tile to
the screenshots section of `public/index.html`. Assert that what it
photographs is on screen before calling `shot()`. The page's grid is four
tiles across, so the count wants to stay a multiple of four; it is eight.
