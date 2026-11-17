# Every check CI runs, one command each. `make` alone lists them.
# Use a virtual environment first:  python3.12 -m venv .venv && . .venv/bin/activate
SERVICES := core gateway agent

.DEFAULT_GOAL := help
.PHONY: help install hooks lint format typecheck jpl test audit check up smoke down

help: ## List the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  make %-10s %s\n", $$1, $$2}'

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
	mypy tools/jpl_check.py scripts/review.py
	for s in $(SERVICES); do (cd services/$$s && mypy app) || exit 1; done

jpl: ## NASA/JPL "Power of Ten" rules
	python tools/jpl_check.py

test: ## Unit tests with the coverage gate (90%)
	pytest tools/tests scripts/tests --cov=tools --cov=scripts
	for s in $(SERVICES); do \
		(cd services/$$s && pytest --cov=app --cov-config=../../pyproject.toml) || exit 1; \
	done

audit: ## Known vulnerabilities in the pinned dependencies (pip-audit)
	pip-audit --strict -r requirements-tools.txt \
		$(foreach s,$(SERVICES),-r services/$(s)/requirements-dev.txt)

check: lint typecheck jpl test ## Everything CI checks except Docker and the audit

up: ## Build and start the whole stack (needs Docker and a .env file)
	docker compose up -d --build --wait

smoke: ## End-to-end test against the running stack
	./scripts/smoke_test.sh

down: ## Stop the stack
	docker compose down
