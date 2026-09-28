# ===============================
# OpenWebUI - Makefile
# ===============================

# default
PRD ?= False

ifeq ($(PRD),True)
    COMPOSE_FILE=docker-compose.yml
else
    COMPOSE_FILE=docker-compose-dev.yml
endif


# -------------------------------
# Baixar última imagem
# -------------------------------
pull:
	docker compose -f $(COMPOSE_FILE) pull

# -------------------------------
# Subir
# -------------------------------
up:
	docker compose -f $(COMPOSE_FILE) up -d

# -------------------------------
# Atualizar (pull + recreate)
# -------------------------------
update:
	docker compose -f $(COMPOSE_FILE) pull
	docker compose -f $(COMPOSE_FILE) up -d --force-recreate

# -------------------------------
# Atualizar FULL (zera tudo)
# -------------------------------
upgrade:
	docker compose -f $(COMPOSE_FILE) down
	docker compose -f $(COMPOSE_FILE) pull
	docker compose -f $(COMPOSE_FILE) up -d

# -------------------------------
# Ver imagens
# -------------------------------
images:
	docker images | grep mcp-server

# -------------------------------
# Logs
# -------------------------------
logs:
	docker compose -f $(COMPOSE_FILE) logs -f --tail 200

# -------------------------------
# Log
# -------------------------------
log:
	docker compose -f $(COMPOSE_FILE) logs -f --tail 200 mcp-server

# -------------------------------
# Limpar imagens antigas
# -------------------------------
prune:
	docker image prune -f


# -------------------------------
# Parar o serviço
# -------------------------------
stop:
	docker compose -f $(COMPOSE_FILE) stop

# -------------------------------
# DOWN o serviço
# -------------------------------
down:
	docker compose -f $(COMPOSE_FILE) down
# -------------------------------
# Rebuild / recriar
# -------------------------------
build:
	docker compose -f $(COMPOSE_FILE) up -d --build