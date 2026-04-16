.PHONY: build build-api build-renderer build-admin test compose-up compose-down

DOCKER_IMAGE ?= openstreetmap-tile-server

build: build-api build-renderer build-admin

build-api:
	docker build --target api -t $(DOCKER_IMAGE)-api .

build-renderer:
	docker build --target renderer -t $(DOCKER_IMAGE)-renderer .

build-admin:
	docker build --target admin-worker -t $(DOCKER_IMAGE)-admin-worker .

test:
	python3 -m unittest discover -s tests

compose-up:
	docker compose up --build

compose-down:
	docker compose down --volumes --remove-orphans
