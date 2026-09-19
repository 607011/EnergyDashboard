.DEFAULT_GOAL := help

# --- Konfiguration (überschreibbar: make deploy PI_HOST=192.168.0.5) ---
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
	@echo "Lokale Entwicklung:"
	@echo "  make up             - Stack lokal bauen und starten"
	@echo "  make down           - Lokalen Stack stoppen"
	@echo "  make restart        - Lokalen Stack neu starten"
	@echo "  make logs           - Poller-Logs verfolgen"
	@echo "  make ps             - Laufende Container anzeigen"
	@echo ""
	@echo "Deployment auf den Raspberry Pi (PI_HOST=$(PI_HOST), PI_DIR=$(PI_DIR)):"
	@echo "  make deploy         - Kompletter Deploy: bauen, alle Images übertragen, starten"
	@echo "  make deploy-poller  - Schneller Re-Deploy: nur das Poller-Image (nach Code-Änderungen)"
	@echo "  make deploy-config  - Nur docker-compose.yml/.env/grafana/ übertragen (kein Neustart)"
	@echo "  make deploy-start   - Stack auf dem Pi (neu) starten"
	@echo "  make clean-deploy   - Lokale Deploy-Artefakte (Tarballs) löschen"

# --- Lokale Entwicklung ---

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

# --- Deployment auf den Pi ---
#
# Baut lokal (arm64), speichert die Images als Tarball, überträgt sie per scp
# und lädt sie auf dem Pi via `docker load` -- kein Bauen auf dem Pi nötig.
# Setzt passwortlosen SSH-Zugriff auf PI_HOST voraus.

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

# Schneller Re-Deploy nach Code-Änderungen am Poller: nur dessen Image neu
# bauen/übertragen (Basis-Layer sind auf dem Pi schon vorhanden -> klein & schnell)
# statt aller vier Images.
deploy-poller: prepare-deploy
	docker save se10k-poller:latest | gzip -1 > $(DEPLOY_DIR)/poller.tar.gz
	scp $(DEPLOY_DIR)/poller.tar.gz $(PI_HOST):$(PI_DIR)/
	ssh $(PI_HOST) "cd $(PI_DIR) && zcat poller.tar.gz | docker load && rm poller.tar.gz"
	rm -f $(DEPLOY_DIR)/poller.tar.gz
	$(MAKE) deploy-config
	$(MAKE) deploy-start

clean-deploy:
	rm -f $(DEPLOY_DIR)/*.tar.gz
