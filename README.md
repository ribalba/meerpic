<p align="center">
  <img src="app/static/img/logo.png" width="160" alt="meerpic logo" />
</p>

<h1 align="center">meerpic</h1>

<p align="center">Your iCloud photos, on your own machine, findable</p>

---

meerpic is a fast, self-hosted photo browser for a library that **rclone** copies out of
**iCloud Photos** onto your disk. It reads every file's own metadata into **PostgreSQL**, so the
photos sort by when they were actually taken, sit on a map where they were taken, and can be
found by what is in them: type `cows` and you get cows, press `s` on a picture of a sheep and
you get the other sheep. Nothing leaves your machine; the search model runs locally too.

**Features:** newest first, the photo you took a minute ago top-left · drag it straight into a
mail (as a JPEG, even when the phone wrote HEIC) · every iPhone video playable in the browser ·
Live Photos as one photo with its motion, not a photo plus a stray two-second video · search
by what is in the picture, in English or German (`kühe auf der weide` works) · find similar ·
every picture of the same person, from a face in the info panel · the words in a picture or a
video, from a street sign to a screenshot (`text:rechnung`) ·
a map with your photos on it · a filter bar that types like meercal's and meerail's
(`in:potsdam is:video 2024`) · one Sync button for the rclone copy · your iCloud albums,
favourites and WhatsApp saves · videos that play (or flick through their frames) as you hover
over them · a search for explicit pictures, blurred until you look · Delete into a trash
folder, with every file move shown first · full keyboard control · light + dark, following
the system or pinned.

It splits into two pieces, the way the rest of the suite does:

- **`meerpic-agent`**: does the work. It walks the library, reads the metadata with
  exiftool, draws thumbnails, converts videos with ffmpeg, computes the search vectors, and
  runs rclone when you press Sync. The only writer to the database and the cache.
- **`meerpic-server`**: the web layer: FastAPI plus the UI. It reads the database, shows
  your photos, and leaves a note for the agent when you want something done. It never
  changes a photo.
- **`core`**: the library both import: configuration, the schema, the cache layout, the
  search model.

## Background

**The dates are in the files, not on them.** rclone sets each file's modification time to
iCloud's date, but a file manager sorting by "created" sees the moment the file landed on
your disk, which after a sync is the same second for twenty thousand photos. The real
capture time is inside each file: EXIF `DateTimeOriginal` in a photo, Apple's
`com.apple.quicktime.creationdate` in a video, both usually with the time zone they were
taken in. meerpic reads those, falls back to patterns in the file name
(`IMG_20240101_123456`, `WhatsApp Image 2024-01-01 at ...`) and only then to the file
time, and remembers which it used, so a photo dated by guesswork says so.

On a real library of 25,095 files: 20,474 carry a capture time, 71 more have one in their
name, 4,545 (mostly pictures saved from messengers) carry none and are dated from iCloud's own
date, and 5 have nothing at all.

**Videos do not play because they are HEVC.** An iPhone records H.265, which no browser on
Linux decodes, and which Fedora's own ffmpeg cannot decode either (its `ffmpeg-free` build has
no HEVC decoder and no libx264). That is why double-clicking a `.MOV` from the phone does
nothing useful on many Linux desktops. The agent runs in a container with Debian's full
ffmpeg and gives every video an H.264 copy at 720p, tone-mapped from the iPhone's HDR, so it
plays everywhere; on a real library the copies came out at 5 to 10% of the originals' size.
The original is untouched and always one click away.

**Live Photos are two files.** A Live Photo arrives as a HEIC and a two-second MOV sharing an
Apple `ContentIdentifier`. Shown naively, every moment appears twice. meerpic pairs them: the
still is the photo, and the motion plays when you rest on its LIVE badge. On the library
above that took 5,772 videos out of the grid.

**Search by meaning, locally.** Every photo is turned into a vector by
[SigLIP 2](https://huggingface.co/google/siglip2-base-patch16-224), a model trained to put an
image and a sentence describing it close together. The same model turns what you type into a
vector, and search is the photos nearest to it; "similar" is the photos nearest to another
photo. The vectors live in Postgres with [pgvector](https://github.com/pgvector/pgvector),
beside everything else. It runs on the CPU through ONNX Runtime: about 0.1 s per photo to
index on a laptop, and a few tens of milliseconds per query.

**What iCloud knows and the files do not.** A heart set on the phone, the Hidden album,
Recently Deleted, the moment a picture arrived, and every album, the one WhatsApp keeps
included: none of it is in the files `rclone copy` fetches. rclone lists it, though (`rclone
lsjson --metadata` over the album folders beside "All Photos"), and after every sync the agent
reads it and matches it to the local files by name, or by size and time where iCloud renamed a
file to tell two `IMG_0886.HEIC` apart. Nothing is downloaded for it. Hidden photos and ones in
Recently Deleted stay out of every listing unless asked for.

**WhatsApp leaves nothing behind.** Its saves are bare JPEGs with random names: no date, no
camera, and nothing about the chat they came from, so there is no way to group them by chat,
here or anywhere. What there is: iCloud's WhatsApp album (`is:whatsapp`), and
`is:saved` for every picture an app or a browser saved rather than a camera took.

**Edits are one photo.** iCloud hands over an edited photo as a second file,
`IMG_1925-edited.heic` beside `IMG_1925.HEIC`. As in Photos, the edit is what the grid shows and
the original is one click behind it. On the library above: 552 pairs.

**Places, offline.** Coordinates are turned into town, region and country with a copy of
GeoNames that ships with the agent: no geocoding service sees where you have been. The map
tiles are the one thing the browser fetches from elsewhere, and the tile server is a
setting.

## Requirements

| | |
| --- | --- |
| **Docker** | Engine 24+ with the Compose v2 plugin. Runs Postgres, the server and the agent. |
| **rclone** | 1.74+ with an `iclouddrive` remote using `service = photos` (see below). The agent image brings its own rclone binary; it uses your `rclone.conf`. |
| **Disk** | For the cache, about 40 KB per photo for thumbnails, 5 to 10% of your videos' size for the playable copies, and 1.5 GB for the search model. |
| **CPU** | The first index is the expensive part: about two hours for 25,000 photos on a 20-core laptop (10 worker processes), then the video copies in the background. After that, only new photos cost anything. |

## Install

```bash
git clone https://github.com/ribalba/meerpic
cd meerpic
make up          # postgres + server + agent  ->  http://127.0.0.1:8040
```

`make up` writes `meerpic.toml` from `meerpic.example.toml` the first time. The defaults
assume photos in `~/Pictures/iCloud`; if yours are elsewhere, edit `[library] roots` there
(and `MEERPIC_PICTURES` in `.env` if they are outside `~/Pictures`).

The agent starts indexing at once, newest first: the last few days are browsable within a
minute, searchable a little after, and the rest fills in behind. Videos get their playable
copies once everything has a thumbnail and a search vector (a 4K conversion takes eight
cores, and the grid matters more), except one you open, which is converted right away.
`make logs` shows it working; the sidebar shows what is left.

```bash
make agent-test   # checks exiftool, ffmpeg, rclone, the library and the model; changes nothing
make logs         # watch the agent
make sync         # one iCloud sync, in the foreground
make psql         # a shell on the database
```

### iCloud and rclone

The Sync button runs the command you would otherwise type:

```bash
rclone copy 'iclouddrive:PrimarySync/All Photos' ~/Pictures/iCloud
```

with JSON progress instead of `--progress`, so the window can draw the progress bar. `copy`,
not `sync`: nothing is ever deleted on either side. Photos you deleted in meerpic are left
out with `--exclude-from`, so the copy doesn't bring them back. The remote comes from your own
`~/.config/rclone/rclone.conf`, which is mounted into the agent read-write because rclone
refreshes the iCloud session in it.

If you have no remote yet: `rclone config`, a new remote of type `iclouddrive`, your Apple
ID, and `service = photos`. When Apple asks for a new two-factor code (every few weeks),
the sync fails with a message saying so; run `rclone config reconnect iclouddrive:` in a
terminal and press Sync again.

## Using it

**The photo you just took, into a mail.** Press Sync (or `.`), and it appears top-left as
soon as it has landed. Drag it out of the window into the mail. In the desktop app that drag
carries the actual file: the original when it is a JPEG, a JPEG made from it when the phone
wrote HEIC (the person you are writing to may not be on an Apple device). In a browser tab,
`c` copies it to the clipboard and `d` downloads the JPEG; a browser cannot hand a real file
to another program by dragging.

**Something in the picture.** Type it: `cows`, `birthday cake`, `receipt`, `kühe`. Results
come by relevance; the Relevance | Date switch beside the bar orders the same matches by date.

**Something written in it.** `text:rechnung` lists every photo and video with that word in
it: a letter, a receipt, a sign, a screenshot, a slide in a video. Every word of
`text:"opening hours"` has to be there, anywhere in the picture and in any case, and part of
a word is enough. The info panel (`i`) shows what was read, with the words you searched for
marked; pointing at a line outlines it on the photo, and Copy text puts all of it on the
clipboard. PaddleOCR's PP-OCRv6 reads it (23 MB, on the CPU, nothing leaves the machine),
umlauts and ß included. A video is read on ten frames spread over the clip, and screenshots
and saved pictures are read before camera photos, because that is where the text is.

**More like this one.** `s` on a photo, or Find similar in the viewer. It is the filter
`similar:1234`, so it combines with everything else: `similar:1234 2023`.

**The same person.** The info panel (`i`) shows the faces in a photo; pointing at one outlines
it on the picture, and clicking it lists every picture with that person in it, closest
first. It is the filter `face:5678`, so `face:5678 2019` is that person in 2019. Nobody is
named and nothing is grouped ahead of time: InsightFace's
[buffalo_l](https://huggingface.co/immich-app/buffalo_l) (SCRFD to find the faces, ArcFace to
measure them, 190 MB, on the CPU, nothing leaves the machine) turns each face into a vector,
and the list is the faces nearest to the one you clicked. Its weights are licensed for
non-commercial use only.

**Where it sits in the timeline.** `a` on a photo, or Show in all photos in the viewer, leaves
the search (or album, or map) it was found in for the plain timeline, scrolled to that photo
and ringed, with the rest of its day around it.

**Somewhere.** The Map (`M`) shows everything with a position, clustered, with a thumbnail
per cluster. Pan to the place and press "Show in grid" for the photos in view. Or type
`in:potsdam`; the places your photos were taken in are offered as you type.

**What is in that video.** Rest the pointer on a video: it plays in place, muted, once its
playable copy exists; until then, moving the pointer across the tile flicks through ten frames
from the whole clip, so a ten-minute video is as quick to check as a ten-second one.

**Pictures you did not want.** `is:nsfw` lists what a classifier made for exactly this
([vit-base-nsfw-detector](https://huggingface.co/AdamCodd/vit-base-nsfw-detector), 88 MB, on
the CPU, nothing leaves the machine) scores as explicit, most certain first; it looks at the
pictures apps saved before anything else. Those tiles are blurred everywhere until the pointer
is on them. It is a ranking, not a verdict: look before you delete.

**When it is wrong,** press `n` (or Mark as safe on the blurred photo, in the info panel, or on
a selection). A photo marked safe is never flagged again, and neither is a flagged photo that
looks just like it: meerpic compares the search vectors, and at 0.92 or more (an edit and its
original are about 0.97 apart, unrelated photos about 0.55) it is the same shot. So one mark
clears a whole series. A looser lookalike (0.85 or more) stays flagged but moves to the end of
`is:nsfw`. The classifier itself is not retrained: that would need thousands of examples. The
info panel says why a photo is not flagged and links to the photo it resembles; `n` again, or
Take the mark back, flags it and its lookalikes again. `is:safe` lists your marks.

**Delete.** Select, press `Del` (or the button). Deleting only happens on this computer:
the files move to `.meerpic-trash/<day>/` in the library folder, which is emptied after 30
days. **The photo stays in iCloud and on your iPhone**, because rclone can read iCloud Photos
but not change them (`rclone deletefile` there answers "optional feature not implemented").
To remove it from the phone too, delete it there.

The dialog lists **every file move before it happens**, one per file (a Live Photo's video
and an edit's original go with it). Only that list runs: the agent is handed the plan you
saw and refuses anything else, and if the library changed in between, the dialog shows the
new list and asks again.

Sync won't download a deleted photo again. meerpic remembers each deleted file from iCloud
(table `deleted_files`: its name, size and SHA-256), and every `rclone copy` leaves those
names out. iCloud can rename a photo when a new one with the same name arrives
(`IMG_0988.HEIC` becomes `IMG_0988_<iCloud id>.HEIC`). If a deleted photo comes back under a
new name that way, or through an `rclone copy` you run by hand, the scan recognises its
content and moves it straight back to the trash. If a name you deleted later belongs to a
different photo in iCloud, meerpic stops leaving that name out.

**To undo a delete**, move the file from `.meerpic-trash/<day>/` back into the library folder
before the 30 days are up. The next scan shows it again and forgets that it was deleted.
Turn Delete off with `[library] delete = false`.

### The filter bar

Plain words search by meaning. Everything else narrows, and all of it combines:

| | |
| --- | --- |
| `in:potsdam` | the place: town, region or country, as far as GeoNames knows it |
| `near:52.39,13.06,2` | within 2 km of a point (default 1 km) |
| `is:video` `is:photo` `is:live` `is:screenshot` | what it is |
| `is:located` `is:unlocated` `is:undated` | what it knows |
| `is:favorite` `album:"Urlaub Polen"` | iCloud's hearts and albums |
| `is:whatsapp` `is:saved` | WhatsApp's album; anything an app or a browser saved |
| `is:nsfw` | possibly explicit, most certain first |
| `is:safe` | the ones you marked safe (`n`) |
| `is:edited` `is:hidden` `is:deleted` | edits; iCloud's Hidden and Recently Deleted |
| `2024` `year:2024` `month:2024-07` `on:2024-07-22` | when, in the time it was taken where it was taken |
| `after:2024-03` `before:2024-06-30` | a range; each takes a year, a month or a day |
| `camera:iphone` | make or model |
| `file:IMG_12` | the file name |
| `text:rechnung` `text:"opening hours"` | words written in it: every one, anywhere in it, in any case |
| `similar:1234` | nearest to that photo |
| `face:5678` | the same person as that face; click a face in the info panel |
| `sort:date` | order a search by date rather than relevance |

### Keys

`/` search · arrows move · `Enter` open · `Esc` back · `x` select · `s` similar ·
`m` show on map · `a` show in all photos · `M` map view · `d` download JPEG · `D` original ·
`c` copy · `i` info · `.` sync · `n` not explicit (mark safe) · `Del` delete · `g f` favourites · `+`/`-` bigger/smaller ·
`Home` newest · `?` all of them.

## The desktop app

```bash
make desktop     # or: cd electron && make distinstall
```

A thin Electron window around the server, like meercal's and meerail's, with one thing a
browser cannot do: dragging a photo out hands the target a real file. See
[electron/README.md](electron/README.md).

## Configuration

Everything is in `meerpic.toml`; `meerpic.example.toml` documents every key. The ones worth
knowing:

| | |
| --- | --- |
| `[library] roots` | the folders to index, `name = "path"`; more than one is fine |
| `[library] delete` | whether Delete exists; it moves files to the trash here and never deletes in iCloud |
| `[server] password` | off by default, for 127.0.0.1; set it before exposing the port |
| `[server] timezone` | where a photo without a zone of its own was taken (default: this machine's) |
| `[video] previews` | `"all"` converts every video in the background, `"on-demand"` when first opened |
| `[search] model` | SigLIP 2 (default, multilingual) or `Qdrant/clip-ViT-B-32` (six times faster, English) |
| `[sync] interval` | minutes between automatic syncs; 0 = only when you press the button |
| `[sync] albums` | read favourites, hidden photos and albums from iCloud after each sync |
| `[nsfw] threshold`, `blur` | where "possibly explicit" starts (0.7), and whether to blur those tiles |
| `[nsfw] clear_above`, `demote_above` | how alike a photo must be to one marked safe to be cleared (0.92) or listed last (0.85); 0 turns either off |
| `[faces] min_score` | how alike two faces must be to count as one person (0.35); lower finds more, and sooner a stranger |
| `[ocr] enabled`, `videos` | read the text in photos, and in videos (ten frames each) |
| `[share] strip_location` | take the GPS position out of what you drag, copy and download |
| `[map] tile_url` | the tile server; `""` turns the map off |

## Architecture

```
  iCloud Photos
       │  rclone copy (when you press Sync)
       ▼
  ~/Pictures/iCloud ───▶ meerpic-agent ──writes──▶ PostgreSQL + pgvector
                         (exiftool, ffmpeg,              ▲
                          SigLIP, GeoNames)              │ reads, queues jobs
                              │ writes                   │
                              ▼                          │
                        ~/.cache/meerpic ──reads──▶ meerpic-server ◀── browser / desktop app
```

The two halves share the database and the cache and nothing else. The server never runs
ffmpeg or rclone: the Sync button leaves a row in `jobs`, the agent picks it up within two
seconds, and writes its progress back into the same row for the UI to poll.

| Table | What it holds |
| --- | --- |
| `photos` | One row per file: where it is, when and where it was taken, what took it, which processing stages are done for which version of it |
| `photo_embeddings` | One vector per photo, from the configured model, with an HNSW index |
| `photo_texts` | What a photo or video says, for the files that say anything: its lines, where each is on the photo, and a trigram index for `text:` |
| `albums`, `photo_albums` | iCloud's albums and which local files are in them, rebuilt after every sync |
| `deleted_files` | Files deleted here that iCloud still has: the name every sync leaves out, size, SHA-256, where the file went in the trash |
| `jobs` | Syncs, album refreshes, deletes (with the plan that was confirmed), video conversions, re-indexing: what the agent was asked to do and how far it got |
| `settings` | UI preferences and the agent's heartbeat |

Nothing is ever written to a photo, and nothing is deleted except by a Delete somebody
confirmed. Every column is derived from the file and can be rebuilt
(`make reindex STAGE=all`); every file in the cache can be deleted and will be made again.
A changed file gets a new cache key, so its thumbnail and its URLs change with it and a
browser can cache everything forever without ever showing a stale one.

## Development

```bash
make venv                  # .venv with both requirement sets, pytest, ruff
make infra                 # just Postgres (pgvector) on 127.0.0.1:5434
make dev                   # uvicorn --reload on :8000
make index LIMIT=200       # one indexing pass natively over the newest 200 files
docker compose run --rm agent python -m agent.main --relink   # re-pair Live Photos and edits
make test-db && make test  # the suite, against a throwaway database
```

The database tests skip without `MEERPIC_TEST_DB`, and the agent's exiftool and ffmpeg
tests skip without those tools, so a bare `pytest` still runs everything else. The search
model, the NSFW classifier and the text models are replaced by deterministic stand-ins in
tests (`MEERPIC_EMBED_FAKE=1`, `MEERPIC_NSFW_FAKE=1`, `MEERPIC_OCR_FAKE=1`), so no test
downloads anything, and every rclone call in the agent's tests is refused unless it points
at a local folder.

Running the agent natively needs exiftool, rclone and an ffmpeg with an HEVC decoder and
libx264 on the host. On Fedora that means RPM Fusion's ffmpeg rather than `ffmpeg-free`;
without it, H.264 videos still get their copies and HEVC ones fail with a message saying
why. The container has everything.

## Status

0.1.0. What works: indexing, dates, places, Live Photo and edit pairing, thumbnails,
playable video copies and hover storyboards, search and similar, the map, sync, iCloud albums
and favourites, NSFW scoring, faces, text in photos and videos, drag and drop, and Delete (on
this computer only).

Not there yet:

- **An installer.** meercal's `meercal.sh` and published images are the model; for now it
  is `make up` from a checkout.
- **Faces.** Search by what is in a photo works; by who is in it does not.
- **Editing anything.** On purpose: the files are the truth, and meerpic never writes one.
- **Chats.** WhatsApp keeps no trace of the chat in what it saves; see Background.

## License

AGPL-3.0. See [LICENSE](LICENSE).
