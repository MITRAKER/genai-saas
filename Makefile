# Every check CI runs, one command each. `make` alone lists them.
# Use a virtual environment first:  python3.12 -m venv .venv && . .venv/bin/activate
SERVICES := core gateway agent github
# The github service's tests need Postgres with pgvector: `make test-db`.
TEST_DB := genai-saas-test-db
# The same image as docker-compose.yml, where Dependabot keeps it up to date.
PGVECTOR_IMAGE := $(shell grep -o 'pgvector/pgvector:[^ ]*' docker-compose.yml)
# The demo is its own Compose project, with its own database, so it never
# touches the data of `make up`. The second file adds the fake GitHub.
DEMO := COMPOSE_PROJECT_NAME=genai-saas-demo COMPOSE_FILE=docker-compose.yml:docker-compose.e2e.yml

.DEFAULT_GOAL := help
.PHONY: help install hooks lint format typecheck jpl test test-db test-db-down audit check \
	env up smoke down demo-key demo demo-down

help: ## List the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  make %-13s %s\n", $$1, $$2}'

install: ## Install the tools and every service's dependencies, then the git hooks
	pip install -r requirements-tools.txt
	for s in $(SERVICES); do pip install -r services/$$s/requirements-dev.txt || exit 1; done
	pre-commit install

hooks: ## Run every pre-commit hook on every file
	pre-commit run --all-files

lint: ## Lint and check formatting (ruff)
	ruff check .
	ruff format --check .

format: ## Apply safe lint fixes and format the code
	ruff check --fix .
	ruff format .

typecheck: ## Static type check (mypy --strict)
	mypy tools/jpl_check.py scripts/review.py scripts/fake_github.py
	for s in $(SERVICES); do (cd services/$$s && mypy app) || exit 1; done

jpl: ## NASA/JPL "Power of Ten" rules
	python tools/jpl_check.py

test: ## Unit tests with the coverage gate (90%); start `make test-db` first
	pytest tools/tests scripts/tests --cov=tools --cov=scripts
	for s in $(SERVICES); do \
		(cd services/$$s && pytest --cov=app --cov-config=../../pyproject.toml) || exit 1; \
	done

# The wait asks over TCP, not the socket: while Postgres initializes, it
# listens on the socket only, and then it restarts.
test-db: ## Start Postgres with pgvector for the github tests (localhost:5433)
	@docker start $(TEST_DB) >/dev/null 2>&1 || docker run --detach --name $(TEST_DB) \
		--env POSTGRES_USER=test --env POSTGRES_PASSWORD=test --env POSTGRES_DB=test \
		--publish 127.0.0.1:5433:5432 $(PGVECTOR_IMAGE) >/dev/null
	@for _ in $$(seq 60); do \
		docker exec $(TEST_DB) pg_isready --host 127.0.0.1 --username test >/dev/null && exit 0; \
		sleep 1; \
	done; echo "the test database did not start" >&2; exit 1
	@echo "test database ready on localhost:5433"

test-db-down: ## Delete the test database
	docker rm --force $(TEST_DB)

audit: ## Known vulnerabilities in the pinned dependencies (pip-audit)
	pip-audit --strict -r requirements-tools.txt \
		$(foreach s,$(SERVICES),-r services/$(s)/requirements-dev.txt)

check: lint typecheck jpl test ## Everything CI checks except Docker and the audit

env: ## Create .env with fresh secrets (an existing one is kept)
	./scripts/make_env.sh

up: env ## Build and start the whole stack (needs Docker)
	docker compose up --detach --build --wait

smoke: ## End-to-end test against the running stack
	./scripts/smoke_test.sh

down: ## Stop the stack
	docker compose down --remove-orphans

demo-key: secrets/demo-app.pem ## A throwaway GitHub App key for the demo

# The worker in its container runs as another user (uid 10001), so the key must
# be readable by others. Fine for a throwaway key; see README.md for a real one.
secrets/demo-app.pem:
	@mkdir -p secrets
	openssl genrsa -out $@ 2048
	chmod 644 $@

demo: env demo-key ## Review a pull request on a fake GitHub, end to end (run `make down` first)
	$(DEMO) docker compose down --volumes --remove-orphans
	$(DEMO) docker compose up --detach --build --wait
	./scripts/github_e2e.sh
	@echo "The demo is still running. Logs: $(DEMO) docker compose logs github-worker"
	@echo "Stop it with: make demo-down"

demo-down: ## Stop the demo and delete its data
	$(DEMO) docker compose down --volumes --remove-orphans
