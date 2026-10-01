.DEFAULT_GOAL := help

# --- Configuration (override e.g. via: make deploy PI_HOST=192.168.0.5) ---
# An SSH destination: "pihole" is a Host alias in ~/.ssh/config (192.168.0.2 with its own key,
# so deploys don't depend on that key being loaded in the SSH agent).
PI_HOST      ?= pihole
PI_DIR       ?= ~/se10k
DEPLOY_DIR   := deploy
IMAGES       := redis/redis-stack-server:latest grafana/grafana:11.3.0 se10k-poller:latest se10k-grafana-init:latest se10k-hoymiles-poller:latest se10k-weishaupt-poller:latest se10k-meter-form:latest se10k-push:latest se10k-sysmon:latest se10k-compute-controller:latest se10k-shelly-poller:latest se10k-caddy:latest
COMPOSE_SRC  := docker-compose.yml
COMPOSE_DST  := $(DEPLOY_DIR)/docker-compose.yml

.PHONY: help up down restart logs ps build \
        deploy deploy-poller deploy-config deploy-images deploy-start \
        reading prepare-deploy clean-deploy

help:
	@echo "Local development:"
	@echo "  make up             - Build and start the stack locally"
	@echo "  make down           - Stop the local stack"
	@echo "  make restart        - Restart the local stack"
	@echo "  make logs           - Follow the poller logs"
	@echo "  make ps             - Show running containers"
	@echo ""
	@echo "Deployment to the Raspberry Pi (PI_HOST=$(PI_HOST), PI_DIR=$(PI_DIR)):"
	@echo "  make deploy         - Full deploy: build, transfer all images, start"
	@echo "  make deploy-poller  - Fast re-deploy: just the poller image (after code changes)"
	@echo "  make deploy-config  - Only sync docker-compose.yml/.env/grafana/ (no restart)"
	@echo "  make deploy-start   - (Re)start the stack on the Pi"
	@echo "  make clean-deploy   - Remove local deploy artifacts (tarballs)"
	@echo ""
	@echo "Heat pump electricity meter (manual reading):"
	@echo "  make reading KWH=12345.6 [THERMAL=8324] [AT=\"2026-09-21 08:00\"] [LOCAL=1]"

# --- Local development ---

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f poller

ps:
	docker compose ps

build:
	docker compose --profile proxy build poller grafana-init hoymiles-poller weishaupt-poller meter-form push sysmon compute-controller shelly-poller caddy

# --- Deployment to the Pi ---
#
# Builds locally (arm64), saves the images as a tarball, transfers them via
# scp and loads them on the Pi via `docker load` -- no building on the Pi
# needed. Assumes passwordless SSH access to PI_HOST.

prepare-deploy: build
	mkdir -p $(DEPLOY_DIR)
	sed -e 's|build: ./poller|image: se10k-poller:latest|' \
	    -e 's|build: ./grafana-init|image: se10k-grafana-init:latest|' \
	    -e 's|build: ./hoymiles-poller|image: se10k-hoymiles-poller:latest|' \
	    -e 's|build: ./weishaupt-poller|image: se10k-weishaupt-poller:latest|' \
	    -e 's|build: ./meter-form|image: se10k-meter-form:latest|' \
	    -e 's|build: ./push|image: se10k-push:latest|' \
	    -e 's|build: ./sysmon|image: se10k-sysmon:latest|' \
	    -e 's|build: ./compute-controller|image: se10k-compute-controller:latest|' \
	    -e 's|build: ./shelly-poller|image: se10k-shelly-poller:latest|' \
	    -e 's|build: ./caddy|image: se10k-caddy:latest|' \
	    $(COMPOSE_SRC) > $(COMPOSE_DST)
	cp .env $(DEPLOY_DIR)/.env
	rm -rf $(DEPLOY_DIR)/grafana
	cp -R grafana $(DEPLOY_DIR)/grafana

# The grafana/ and caddy/ directories on the Pi are bind-mounted into running containers, so
# they must be updated *in place*: deleting and re-creating a directory leaves the containers
# pointing at the deleted one ("no such file or directory") until they are re-created.
# Files are only overwritten, never removed: a dashboard deleted from the repo stays on the Pi
# until you delete it there (and emptying the directory could make Grafana drop dashboards).
deploy-config: prepare-deploy
	ssh $(PI_HOST) "mkdir -p $(PI_DIR)/grafana $(PI_DIR)/caddy"
	scp -q $(COMPOSE_DST) $(DEPLOY_DIR)/.env $(PI_HOST):$(PI_DIR)/
	COPYFILE_DISABLE=1 tar -C $(DEPLOY_DIR)/grafana -cf - . | ssh $(PI_HOST) "cd $(PI_DIR)/grafana && tar -xf -"
	scp -q caddy/Caddyfile $(PI_HOST):$(PI_DIR)/caddy/Caddyfile
	ssh $(PI_HOST) "mkdir -p $(PI_DIR)/caddy/pwa"
	COPYFILE_DISABLE=1 tar -C caddy/pwa -cf - . | ssh $(PI_HOST) "cd $(PI_DIR)/caddy/pwa && tar -xf -"

deploy-images: prepare-deploy
	docker save $(IMAGES) | gzip -1 > $(DEPLOY_DIR)/images.tar.gz
	scp $(DEPLOY_DIR)/images.tar.gz $(PI_HOST):$(PI_DIR)/
	ssh $(PI_HOST) "cd $(PI_DIR) && zcat images.tar.gz | docker load && rm images.tar.gz"
	rm -f $(DEPLOY_DIR)/images.tar.gz

deploy-start:
	ssh $(PI_HOST) "cd $(PI_DIR) && docker compose --profile proxy up -d"
	ssh $(PI_HOST) "cd $(PI_DIR) && docker compose --profile proxy exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force"
	ssh $(PI_HOST) "cd $(PI_DIR) && docker compose --profile proxy ps"

deploy: deploy-images deploy-config deploy-start
	@echo ""
	@echo "Deployed to $(PI_HOST)."

# Fast re-deploy after poller code changes: only rebuild/transfer its image
# (base layers already exist on the Pi -> small & fast) instead of all four.
deploy-poller: prepare-deploy
	docker save se10k-poller:latest | gzip -1 > $(DEPLOY_DIR)/poller.tar.gz
	scp $(DEPLOY_DIR)/poller.tar.gz $(PI_HOST):$(PI_DIR)/
	ssh $(PI_HOST) "cd $(PI_DIR) && zcat poller.tar.gz | docker load && rm poller.tar.gz"
	rm -f $(DEPLOY_DIR)/poller.tar.gz
	$(MAKE) deploy-config
	$(MAKE) deploy-start

clean-deploy:
	rm -f $(DEPLOY_DIR)/*.tar.gz

# Record a manual electricity meter reading for the heat pump (writes to the Pi, or local with LOCAL=1).
reading:
	@test -n "$(KWH)" || { echo 'Usage: make reading KWH=12345.6 [THERMAL=8324] [AT="2026-09-21 08:00"] [LOCAL=1]'; exit 2; }
	@PI_HOST="$(PI_HOST)" PI_DIR="$(PI_DIR)" scripts/meter-reading.sh "$(KWH)" "$(AT)" "$(THERMAL)"
