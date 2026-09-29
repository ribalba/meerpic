# meerpic desktop (Electron) build & run helpers.
#
# The app points at the local server via the MEERPIC_URL env var (see main.js).
# Override it on the command line, e.g.  make start MEERPIC_URL=http://meerpic.local:8040

MEERPIC_URL ?= http://localhost:8040
export MEERPIC_URL

NPM ?= npm

# Where `distinstall` puts things. Everything lands under the user's own dirs,
# so no root is needed. Override e.g. with  make distinstall XDG_DATA_HOME=/opt/share
BIN_DIR       := $(HOME)/.local/bin

# Linux: an AppImage plus an XDG desktop entry.
XDG_DATA_HOME ?= $(HOME)/.local/share
APP_HOME      := $(XDG_DATA_HOME)/meerpic
APP_IMAGE     := $(APP_HOME)/meerpic.AppImage
DESKTOP_DIR   := $(XDG_DATA_HOME)/applications
DESKTOP_FILE  := $(DESKTOP_DIR)/meerpic.desktop
ICON_DIR      := $(XDG_DATA_HOME)/icons/hicolor/512x512/apps

# macOS: the .app bundle itself is the install. ~/Applications rather than
# /Applications keeps it to this user and needs no sudo; Spotlight, Launchpad
# and the Dock all index it just the same.
MAC_APP_DIR   ?= $(HOME)/Applications
MAC_APP       := $(MAC_APP_DIR)/meerpic.app

# `distinstall` means something different per desktop, so it is one name in
# front of two recipes rather than a Linux-only target that fails everywhere
# else with a puzzling "no AppImage in dist/".
ifeq ($(shell uname -s),Darwin)
INSTALL_KIND := macos
else
INSTALL_KIND := linux
endif

.PHONY: help install start pack dist clean distinstall distuninstall \
        distinstall-linux distuninstall-linux distinstall-macos distuninstall-macos

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	$(NPM) install

start: ## Run the app against $(MEERPIC_URL) (needs the server up)
	$(NPM) start

pack: ## Build an unpacked app into dist/ (no installer)
	$(NPM) run pack

dist: ## Build distributable installers into dist/
	$(NPM) run dist

distinstall: distinstall-$(INSTALL_KIND) ## Build and install the app for this desktop (macOS or Linux)

distuninstall: distuninstall-$(INSTALL_KIND) ## Remove what distinstall put in place

distinstall-linux: dist
	@set -e; \
	src=$$(ls -t dist/*.AppImage 2>/dev/null | head -n 1); \
	if [ -z "$$src" ]; then echo "No AppImage in dist/; is 'linux' a build target?" >&2; exit 1; fi; \
	echo "Installing $$src -> $(APP_IMAGE)"; \
	mkdir -p "$(APP_HOME)" "$(BIN_DIR)" "$(DESKTOP_DIR)" "$(ICON_DIR)"; \
	install -m 0755 "$$src" "$(APP_IMAGE)"; \
	install -m 0644 build/icon.png "$(ICON_DIR)/meerpic.png"; \
	ln -sfn "$(APP_IMAGE)" "$(BIN_DIR)/meerpic"; \
	printf '%s\n' \
		'[Desktop Entry]' \
		'Type=Application' \
		'Name=meerpic' \
		'GenericName=Photos' \
		'Comment=Your iCloud photos, searchable, on your own machine.' \
		'Exec=env MEERPIC_URL=$(MEERPIC_URL) $(APP_IMAGE) %U' \
		'TryExec=$(APP_IMAGE)' \
		'Icon=meerpic' \
		'Terminal=false' \
		'Categories=Graphics;Photography;' \
		'Keywords=photos;pictures;icloud;meerpic;' \
		'StartupNotify=true' \
		'StartupWMClass=meerpic' \
		> "$(DESKTOP_FILE)"; \
	chmod 0644 "$(DESKTOP_FILE)"; \
	command -v desktop-file-validate >/dev/null 2>&1 && desktop-file-validate "$(DESKTOP_FILE)" || true; \
	command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(DESKTOP_DIR)" || true; \
	command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$(XDG_DATA_HOME)/icons/hicolor" >/dev/null 2>&1 || true; \
	for k in kbuildsycoca6 kbuildsycoca5; do command -v $$k >/dev/null 2>&1 && $$k >/dev/null 2>&1 && break; done; true; \
	echo "Installed. Launch from your app menu, or run 'meerpic' (needs $(BIN_DIR) on PATH)."

distuninstall-linux:
	@rm -f "$(DESKTOP_FILE)" "$(ICON_DIR)/meerpic.png" "$(BIN_DIR)/meerpic" "$(APP_IMAGE)"
	@rmdir "$(APP_HOME)" 2>/dev/null || true
	@command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(DESKTOP_DIR)" || true
	@command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$(XDG_DATA_HOME)/icons/hicolor" >/dev/null 2>&1 || true
	@for k in kbuildsycoca6 kbuildsycoca5; do command -v $$k >/dev/null 2>&1 && $$k >/dev/null 2>&1 && break; done; true
	@echo "Uninstalled."

# The .dmg is for handing to someone else; installing here means the bundle
# electron-builder already staged. Copied with ditto rather than cp, which is
# the one that carries a bundle's symlinks, extended attributes and code
# signature across intact: an arm64 build is ad-hoc signed and will not launch
# if the copy breaks that.
distinstall-macos: dist
	@set -e; \
	src=$$(ls -td dist/mac*/meerpic.app 2>/dev/null | head -n 1); \
	if [ -z "$$src" ]; then echo "No meerpic.app in dist/; is 'mac' a build target?" >&2; exit 1; fi; \
	echo "Installing $$src -> $(MAC_APP)"; \
	mkdir -p "$(MAC_APP_DIR)" "$(BIN_DIR)"; \
	rm -rf "$(MAC_APP)"; \
	ditto "$$src" "$(MAC_APP)"; \
	ln -sfn "$(MAC_APP)/Contents/MacOS/meerpic" "$(BIN_DIR)/meerpic"; \
	echo "Installed. Launch from Spotlight or Launchpad, or run 'meerpic' (needs $(BIN_DIR) on PATH)."; \
	if [ "$(MEERPIC_URL)" != "http://localhost:8040" ]; then \
		echo; \
		echo "Note: MEERPIC_URL is not baked into the bundle. macOS does not give a"; \
		echo "      Finder launch your shell environment, so the app menu starts it on"; \
		echo "      the default http://localhost:8040. To reach $(MEERPIC_URL),"; \
		echo "      run 'meerpic' from a shell that has MEERPIC_URL set."; \
	fi

distuninstall-macos:
	@rm -rf "$(MAC_APP)"
	@rm -f "$(BIN_DIR)/meerpic"
	@rmdir "$(MAC_APP_DIR)" 2>/dev/null || true
	@echo "Uninstalled."

clean: ## Remove build output
	rm -rf dist
