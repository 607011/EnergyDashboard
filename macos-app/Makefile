# PV Monitor -- a small native macOS window app (Apple Silicon only) for the summed PV production
# and the house consumption. Run these from macos-app/ (or use `make -C macos-app <target>`).
#
# The app reads Grafana's query API with a read-only token, so `install` first makes sure a token
# exists, then builds, then copies the app to /Applications. The app is signed ad hoc (no developer
# account): fine for the Mac it was built on; elsewhere, right-click > Open once.

.DEFAULT_GOAL := help
SHELL := /bin/bash

APP_NAME    := PV Monitor
BIN_NAME    := PVMonitor
APP         := build/$(APP_NAME).app
CONFIG_DIR  ?= $(HOME)/Library/Application Support/$(APP_NAME)
CONFIG      := $(CONFIG_DIR)/config.json
ENV_FILE    := ../.env
# Address of Grafana; defaults to GRAFANA_ROOT_URL from ../.env (override: make token GRAFANA_URL=https://...)
GRAFANA_URL ?= $(shell grep -E '^GRAFANA_ROOT_URL=' $(ENV_FILE) 2>/dev/null | cut -d= -f2-)
SERVICE_ACCOUNT := pv-monitor

.PHONY: help test build token ensure-token install run clean

help:
	@echo "PV Monitor (macOS, Apple Silicon):"
	@echo "  make install   - get a token (if none yet), build the app and copy it to /Applications"
	@echo "                   (make install NEW_TOKEN=1 fetches a fresh token even if one exists)"
	@echo "  make build     - build $(APP) (runs the self-test first)"
	@echo "  make token     - create the read-only Grafana token and write $(CONFIG)"
	@echo "  make test      - build and run the self-test (averaging and colour rule)"
	@echo "  make run       - build and start the app from build/ without installing"
	@echo "  make clean     - remove build output"

test:
	swift build -c release --arch arm64
	@"$$(swift build -c release --arch arm64 --show-bin-path)/$(BIN_NAME)" --selftest

build:
	swift build -c release --arch arm64
	@set -euo pipefail; \
	bin="$$(swift build -c release --arch arm64 --show-bin-path)/$(BIN_NAME)"; \
	"$$bin" --selftest > /dev/null && echo "Self-test passed"; \
	rm -rf "$(APP)"; \
	mkdir -p "$(APP)/Contents/MacOS" "$(APP)/Contents/Resources"; \
	cp "$$bin" "$(APP)/Contents/MacOS/$(BIN_NAME)"; \
	cp Info.plist "$(APP)/Contents/Info.plist"; \
	tmp="$$(mktemp -d)"; iconset="$$tmp/AppIcon.iconset"; mkdir "$$iconset"; \
	python3 ../scripts/make-pwa-icons.py --app-icon "$$tmp/icon-1024.png" > /dev/null; \
	for size in 16 32 128 256 512; do \
	  sips -z $$size $$size "$$tmp/icon-1024.png" --out "$$iconset/icon_$${size}x$${size}.png" > /dev/null; \
	  sips -z $$((size*2)) $$((size*2)) "$$tmp/icon-1024.png" --out "$$iconset/icon_$${size}x$${size}@2x.png" > /dev/null; \
	done; \
	iconutil -c icns "$$iconset" -o "$(APP)/Contents/Resources/AppIcon.icns"; \
	rm -rf "$$tmp"; \
	codesign --force --sign - "$(APP)"; \
	echo "Built $(APP)"; \
	lipo -archs "$(APP)/Contents/MacOS/$(BIN_NAME)" | sed 's/^/Architectures: /'

# Creates the read-only service account (once) and a token for it. Needs the Grafana admin
# password (GRAFANA_ADMIN_PASSWORD in ../.env). Every run adds a token; old ones stay valid until
# deleted under Administration > Users and access > Service accounts.
token:
	@set -euo pipefail; \
	url="$(GRAFANA_URL)"; url="$${url%/}"; \
	[ -n "$$url" ] || { echo "No Grafana address: set GRAFANA_ROOT_URL in $(ENV_FILE) or pass GRAFANA_URL=..." >&2; exit 2; }; \
	password="$$(grep -E '^GRAFANA_ADMIN_PASSWORD=' $(ENV_FILE) 2>/dev/null | cut -d= -f2- || true)"; \
	[ -n "$$password" ] || { echo "GRAFANA_ADMIN_PASSWORD not found in $(ENV_FILE)" >&2; exit 2; }; \
	api() { curl -sf -u "admin:$$password" -H 'Content-Type: application/json' "$$@"; }; \
	id="$$(api "$$url/api/serviceaccounts/search?query=$(SERVICE_ACCOUNT)" | jq -r '.serviceAccounts[]|select(.name=="$(SERVICE_ACCOUNT)")|.id' | head -1)"; \
	if [ -z "$$id" ]; then \
	  id="$$(api -X POST "$$url/api/serviceaccounts" -d '{"name":"$(SERVICE_ACCOUNT)","role":"Viewer"}' | jq -r .id)"; \
	  echo "Created service account '$(SERVICE_ACCOUNT)' (id $$id, role Viewer)"; \
	else \
	  echo "Service account '$(SERVICE_ACCOUNT)' exists (id $$id)"; \
	fi; \
	token="$$(api -X POST "$$url/api/serviceaccounts/$$id/tokens" -d "{\"name\":\"macos-app-$$(date +%Y%m%d-%H%M%S)\"}" | jq -r .key)"; \
	[ -n "$$token" ] && [ "$$token" != null ] || { echo "Could not create a token" >&2; exit 1; }; \
	mkdir -p "$(CONFIG_DIR)"; umask 077; \
	jq -n --arg url "$$url" --arg token "$$token" '{url:$$url, token:$$token}' > "$(CONFIG)"; \
	chmod 600 "$(CONFIG)"; \
	echo "Wrote $(CONFIG)"

# Only fetches a token if there is no config yet, so repeated installs don't pile up tokens.
ensure-token:
	@if [ -f "$(CONFIG)" ] && [ -z "$(NEW_TOKEN)" ]; then \
	  echo "Config exists: $(CONFIG) (make install NEW_TOKEN=1 for a fresh token)"; \
	else \
	  $(MAKE) --no-print-directory token; \
	fi

install: ensure-token build
	rm -rf "/Applications/$(APP_NAME).app"
	cp -R "$(APP)" /Applications/
	@echo "Installed to /Applications/$(APP_NAME).app"

run: build
	open -n "$(APP)"

clean:
	rm -rf .build build
