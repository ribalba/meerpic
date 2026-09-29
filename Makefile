# meerpic: convenience targets.
#
# `make up` runs the whole stack in Docker -- postgres, server and agent. `make
# agent` runs the indexer natively instead, which is the faster loop for
# working on it; it needs exiftool, rclone and an ffmpeg that can decode HEVC
# and encode H.264 on the host (Fedora's ffmpeg-free cannot; the container's
# can, which is why the container is the default).

COMPOSE ?= docker compose
VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
VERSION := $(shell cat VERSION)

# The containers run as you, so what they write into the cache and what rclone
# writes into your picture folder is yours. They read the zone from here,
# because a container has no zone of its own and a photo without an offset is
# a wall clock in *your* zone.
export MEERPIC_UID := $(shell id -u)
export MEERPIC_GID := $(shell id -g)
export TZ ?= $(shell readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')

CACHE_DIR  ?= $(HOME)/.cache/meerpic
RCLONE_DIR ?= $(HOME)/.config/rclone

.PHONY: help up down logs build infra dev venv agent agent-test index sync reindex \
        psql desktop test test-db config images

help:
	@echo "meerpic $(VERSION):"
	@echo "  make up         - build + run the stack (postgres + server + agent) -> http://127.0.0.1:8040"
	@echo "  make down       - stop it"
	@echo "  make logs       - tail the agent (the half doing the work)"
	@echo "  make agent-test - check exiftool, ffmpeg, rclone, the library and the model, change nothing"
	@echo "  make sync       - run one iCloud sync now, in the foreground"
	@echo "  make reindex    - redo one stage for every photo (STAGE=meta|thumbs|embeddings|previews|story|nsfw|faces|all)"
	@echo "  make psql       - a shell on the database"
	@echo "  make desktop    - run the Electron app against the local server"
	@echo "  make infra      - run only postgres (for native development)"
	@echo "  make dev        - run the server natively with --reload (needs 'make infra' + venv)"
	@echo "  make agent      - run the agent natively instead of in its container"
	@echo "  make index      - one full indexing pass natively, then exit (LIMIT=N for the newest N)"
	@echo "  make venv       - create $(VENV) with server + agent dependencies"
	@echo "  make test       - run the test suite (make test-db first)"

# The config file and the two directories are made here, before compose sees
# them: a bind mount whose source does not exist becomes a root-owned
# *directory*, and the server then finds a folder where its config should be.
config:
	@test -f meerpic.toml || { cp meerpic.example.toml meerpic.toml && chmod 600 meerpic.toml \
	  && echo "wrote meerpic.toml from the example; edit it if your photos are not in ~/Pictures/iCloud"; }
	@mkdir -p "$(CACHE_DIR)" "$(RCLONE_DIR)"

up: config
	$(COMPOSE) up --build -d
	@echo "meerpic on http://127.0.0.1:$${MEERPIC_PORT:-8040}"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f agent

build:
	$(COMPOSE) build

infra:
	$(COMPOSE) up -d db

agent-test: config
	$(COMPOSE) run --rm --no-deps agent python -m agent.main --test

sync: config
	$(COMPOSE) run --rm agent python -m agent.main --sync

STAGE ?= all
reindex:
	$(COMPOSE) run --rm agent python -m agent.main --reindex $(STAGE)

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-meerpic} -d $${POSTGRES_DB:-meerpic}

desktop:
	# Unset because a terminal inside an Electron app (VS Code) can export it,
	# and it makes Electron start as plain Node.
	cd electron && npm install && env -u ELECTRON_RUN_AS_NODE npm start

# --- native development ------------------------------------------------------

venv:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt -r agent/requirements.txt pytest httpx ruff
	@echo "ready: $(VENV)"

dev:
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

agent:
	$(PY) -m agent.main

LIMIT ?=
index:
	$(PY) -m agent.main --once $(if $(LIMIT),--limit $(LIMIT))

# A database of its own, dropped and rebuilt by the tests. Never the one your
# photos are indexed in: the API tests start by dropping every table they find.
TEST_DB ?= postgresql+psycopg://meerpic:meerpic@127.0.0.1:$${MEERPIC_DB_PORT:-5434}/meerpic_test

test-db:
	$(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-meerpic} -d postgres \
	  -c "DROP DATABASE IF EXISTS meerpic_test" \
	  -c "CREATE DATABASE meerpic_test"

test:
	MEERPIC_TEST_DB=$(TEST_DB) $(VENV)/bin/pytest -q -ra

images:
	docker build --build-arg MEERPIC_VERSION=$(VERSION) -t meerpic-server:$(VERSION) .
	docker build --build-arg MEERPIC_VERSION=$(VERSION) -f agent/Dockerfile -t meerpic-agent:$(VERSION) .
