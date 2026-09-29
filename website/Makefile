# meerpic.com: the static site, and the screenshots on it.
#
#   make -C website serve         the site on http://127.0.0.1:8083, public/ live-mounted
#   make -C website screenshots   build the demo stack, fill it, photograph it
#   make -C website demo-down     stop the demo stack (its data is kept)
#
# The screenshots are of meerpic itself, built from this checkout and run as a
# stack of its own ("meerpic-demo", screenshots/docker-compose.yml) over a
# library of CC0 demo photos. It shares nothing with a meerpic you run for your
# own photos: not the containers, not the images, not the database, not the
# cache, not meerpic.toml. See screenshots/README.md.

COMPOSE   ?= docker compose
DEMO      := $(COMPOSE) -f screenshots/docker-compose.yml

# Where the demo library, its cache and the downloaded photos live.
export DEMO_DIR  ?= $(HOME)/.cache/meerpic-demo
export DEMO_PORT ?= 18040
export MEERPIC_UID := $(shell id -u)
export MEERPIC_GID := $(shell id -g)

# A Python with Playwright for shoot.py. Any will do; install it with
#   ../.venv/bin/pip install playwright && ../.venv/bin/playwright install chromium
SHOOT_PY  ?= ../.venv/bin/python
SHOOT_ARGS ?=

# The search and NSFW models, 2.6 GB. Copied (a reflink on btrfs and XFS, so
# free and instant) from a meerpic you already run, if there is one, rather
# than downloaded again. Read only; nothing is written back.
MODELS_FROM ?= $(HOME)/.cache/meerpic/models

.PHONY: help serve serve-down demo-images demo-dirs demo-models demo-phone demo-library \
        demo-up demo-seed screenshots shoot demo-down demo-logs

help:
	@echo "  make serve        - the site on http://127.0.0.1:$${WEBSITE_PORT:-8083}"
	@echo "  make screenshots  - demo stack up, demo library in, every shot taken (SHOOT_ARGS=\"--only map\")"
	@echo "  make shoot        - just the shots, against a demo stack that is already up and seeded"
	@echo "  make demo-down    - stop the demo stack; DEMO_DIR ($(DEMO_DIR)) and its database are kept"

serve:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up --build -d
	@echo "http://127.0.0.1:$${WEBSITE_PORT:-8083}"

serve-down:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml down

# --- the demo stack -------------------------------------------------------------

demo-images:
	$(DEMO) build

# Made here, as you, before compose mounts them: a bind-mount source that does
# not exist yet is created by Docker, owned by root.
demo-dirs:
	@mkdir -p "$(DEMO_DIR)/cache" "$(DEMO_DIR)/sources" "$(DEMO_DIR)/library/iCloud" "$(DEMO_DIR)/library/Camera"

demo-models: demo-dirs
	@if [ ! -e "$(DEMO_DIR)/cache/models" ] && [ -d "$(MODELS_FROM)" ]; then \
	  echo "copying the models from $(MODELS_FROM)"; \
	  cp -a --reflink=auto "$(MODELS_FROM)" "$(DEMO_DIR)/cache/models"; \
	fi

# The two iPhone screenshots in the demo library are of this site, at phone size.
demo-phone: demo-dirs
	$(SHOOT_PY) screenshots/shoot.py --phone

demo-library: demo-images demo-dirs demo-phone
	$(DEMO) run --rm --no-deps agent python /website/screenshots/build_library.py

demo-up: demo-library demo-models
	$(DEMO) up -d
	@echo "demo meerpic on http://127.0.0.1:$(DEMO_PORT)"

demo-seed:
	$(DEMO) exec -T agent python /website/screenshots/seed.py

screenshots: demo-up demo-seed shoot
	@echo "the demo stack is still up (http://127.0.0.1:$(DEMO_PORT)); make -C website demo-down stops it"

shoot:
	$(SHOOT_PY) screenshots/shoot.py $(SHOOT_ARGS)

demo-logs:
	$(DEMO) logs -f agent

demo-down:
	$(DEMO) down
