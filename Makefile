# All comments in English as requested

PROJECT := ingparser

.PHONY: build up down restart logs sh run clean prune

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart: down up

logs:
	docker compose logs -f

sh:
	# Open a shell inside the running container
	docker compose exec $(PROJECT) bash

run:
	# Example: run your script inside the container
	# You can pass args with: make run ARGS="--input /app/data/sample.pdf"
	docker compose exec ing-kontoauszug-parser-ingparser  python /app/ing2db.py $(ARGS)

clean:
	# Remove the container and image (keeps your ./data on host)
	docker compose down --rmi local --volume
