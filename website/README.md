# meerpic.com

The marketing site for meerpic. A static page, a stylesheet and some images,
served by nginx. Nothing to build.

## Running it locally

```bash
make -C website serve     # the same as the two lines below
cd website
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
# http://127.0.0.1:8083
```

The dev overlay publishes the port (8080, 8081 and 8082 are meerail's,
meercal's and meerverse's sites, and the meerpic app itself is on 8040, so all
of them run side by side) and mounts `public/` over the image, so an edit is a
reload away. `WEBSITE_PORT=9000` moves it.

While editing you can skip Docker entirely:

```bash
python3 -m http.server -d public 8083
```

## Layout

```text
website/
├── Dockerfile              nginx:1.31-alpine + nginx.conf + public/, with a healthcheck
├── .dockerignore           only nginx.conf and public/ go into the image
├── docker-compose.yml      the Coolify file: one service, `website`, expose 80
├── docker-compose.dev.yml  local overlay: 127.0.0.1:8083, public/ mounted read-only
├── nginx.conf              gzip, cache headers, charset, www.meerpic.com -> meerpic.com
├── Makefile                `make serve`, `make screenshots`, `make demo-down`
├── screenshots/            the demo stack, its library and the capture script (see its README)
└── public/
    ├── index.html          the whole page
    ├── llms.txt            what meerpic is, for LLMs that visit the site
    ├── css/site.css        meercal's styles verbatim + meerpic additions
    └── img/
        ├── logo.png        the hero meerkat, holding a photo
        ├── logo-square.png the app's own icon (app/static/img/logo.png)
        ├── favicon-*.png   the app's favicons (app/static/img)
        ├── og.jpg          1200x630 link preview: logo.png, name and tagline
        └── screenshots/    WebP, 2880x1800, plus credits.txt
```

## The styles

`public/css/site.css` is meercal's `website/public/css/site.css` copied
verbatim, which is itself meerail's, which is meerato's landing page: the sites
are one family and should read as one. Keep everything above the
`===== meerpic additions =====` heading byte-identical to meercal's, so a fix
to one site can be carried to the others with a plain diff (run from
`website/`, with meercal checked out beside meerpic):

```bash
diff <(tail -n +7 ../../meercal/website/public/css/site.css) \
     <(sed -n '7,/===== meerpic additions/p' public/css/site.css | head -n -2)
```

No output means the shared part is still identical.

The lightbox script at the foot of `index.html` is meercal's, copied
unchanged. meercal's slider script is not used here: the grid gets one wide
screenshot under the hero instead.

## Screenshots

They are captured from a running meerpic filled with CC0 demo photos from
StockSnap, never from your own library, so a shot that renders here is one the
app actually produces and shows nobody's pictures:

```bash
make -C website screenshots   # builds and fills a separate demo stack, then shoots
make -C website demo-down     # when finished
```

Output lands in `public/img/screenshots/` as WebP at 2x device scale: the page
serves 2880x1800 files and displays them at half that, so they stay sharp on
retina panels. `grid-dark.webp` is the dark variant the page offers through
`<picture><source media="(prefers-color-scheme: dark)">`. The photo credits go
to `public/img/screenshots/credits.txt`, which the footer links. Details, and
where the demo photos come from, are in `screenshots/README.md`.

## Deploying on Coolify

`docker-compose.yml` is the Coolify file: one service, `website`, with
`expose` rather than `ports`, since Coolify's Traefik reaches it over the
project network and terminates TLS in front of it. The site lives in this
folder of the app's repository, so the resource points at the repository with
a base directory.

1. **New resource**, pick this repository (`github.com/ribalba/meerpic`),
   build pack **Docker Compose**, **Base Directory** `/website`, **Docker
   Compose Location** `/docker-compose.yml` (relative to the base directory).
2. On the `website` service set **Domains** to
   `https://meerpic.com,https://www.meerpic.com`. Port 80 is the one Coolify
   routes to when a domain names none. nginx answers the `www` name with a 301
   to the bare domain, so there is one canonical address.
3. Point the DNS A records for `meerpic.com` and `www.meerpic.com` at the
   Coolify host, and deploy. The image has a healthcheck, so Coolify only
   switches traffic once nginx answers.

Nothing is stored and nothing is secret: the container is disposable, and
there are no environment variables to set.

The site shares its repository with the app. With automatic deployment on, a
push to `main` that only touches the app redeploys the site too, which is
harmless (the same page, rebuilt) but pointless. If that matters, Coolify's
**Watch Paths** setting on the resource is an option: set to `website/**`, it
limits automatic deployments to pushes that change something under this
folder. The pattern assumes paths from the repository root; check it with a
test push before relying on it.

## llms.txt

`public/llms.txt` is the site's grounding page for language models, in the
[llmstxt.org](https://llmstxt.org/) format: a Markdown summary at the site root
that says what meerpic is, who it is for, what it can and cannot do yet, and
where the documentation lives.

`index.html` points at it with a `<link rel="alternate">` and carries the same
facts as schema.org JSON-LD (`SoftwareApplication`) in its `<head>`, for
readers that only fetch the page itself. Keep all three in step with the app's
own `README.md` when a feature, a requirement or the install command changes:
every claim on the page should be one the README makes.
