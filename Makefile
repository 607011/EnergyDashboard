.DEFAULT_GOAL := help

# --- Configuration (override e.g. via: make deploy PI_HOST=192.168.0.5) ---
PI_HOST      ?= 192.168.0.2
PI_DIR       ?= ~/se10k
DEPLOY_DIR   := deploy
IMAGES       := redis/redis-stack-server:latest grafana/grafana:11.3.0 se10k-poller:latest se10k-grafana-init:latest
COMPOSE_SRC  := docker-compose.yml
COMPOSE_DST  := $(DEPLOY_DIR)/docker-compose.yml

.PHONY: help up down restart logs ps build \
        deploy deploy-poller deploy-config deploy-images deploy-start \
        prepare-deploy clean-deploy

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
	docker compose build poller grafana-init

# --- Deployment to the Pi ---
#
# Builds locally (arm64), saves the images as a tarball, transfers them via
# scp and loads them on the Pi via `docker load` -- no building on the Pi
# needed. Assumes passwordless SSH access to PI_HOST.

prepare-deploy: build
	mkdir -p $(DEPLOY_DIR)
	sed -e 's|build: ./poller|image: se10k-poller:latest|' \
	    -e 's|build: ./grafana-init|image: se10k-grafana-init:latest|' \
	    $(COMPOSE_SRC) > $(COMPOSE_DST)
	cp .env $(DEPLOY_DIR)/.env
	rm -rf $(DEPLOY_DIR)/grafana
	cp -R grafana $(DEPLOY_DIR)/grafana

deploy-config: prepare-deploy
	ssh $(PI_HOST) "mkdir -p $(PI_DIR)"
	scp -q $(COMPOSE_DST) $(DEPLOY_DIR)/.env $(PI_HOST):$(PI_DIR)/
	ssh $(PI_HOST) "rm -rf $(PI_DIR)/grafana"
	scp -q -r $(DEPLOY_DIR)/grafana $(PI_HOST):$(PI_DIR)/

deploy-images: prepare-deploy
	docker save $(IMAGES) | gzip -1 > $(DEPLOY_DIR)/images.tar.gz
	scp $(DEPLOY_DIR)/images.tar.gz $(PI_HOST):$(PI_DIR)/
	ssh $(PI_HOST) "cd $(PI_DIR) && zcat images.tar.gz | docker load && rm images.tar.gz"
	rm -f $(DEPLOY_DIR)/images.tar.gz

deploy-start:
	ssh $(PI_HOST) "cd $(PI_DIR) && docker compose up -d"
	ssh $(PI_HOST) "cd $(PI_DIR) && docker compose ps"

deploy: deploy-images deploy-config deploy-start
	@echo ""
	@echo "Deployed. Dashboard: http://$(PI_HOST):3000"

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
