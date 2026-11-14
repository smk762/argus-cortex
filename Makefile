UV ?= uv

.PHONY: help install lint format test schema build clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create venv and install with test extras
	$(UV) venv
	$(UV) pip install -e ".[dev]"

lint:  ## Ruff check + format check
	$(UV)x ruff@0.15.16 check src/ tests/
	$(UV)x ruff@0.15.16 format --check src/ tests/

format:  ## Ruff autoformat + autofix
	$(UV)x ruff@0.15.16 format src/ tests/
	$(UV)x ruff@0.15.16 check --fix src/ tests/

test:  ## Run the test suite
	$(UV) run --no-sync pytest --tb=short -q

schema:  ## Regenerate the committed taxonomy-schema.json, provenance-schema.json and consent-schema.json
	$(UV) run --no-sync python -c "from pathlib import Path; from argus_cortex.taxonomy import taxonomy_schema; from argus_cortex.provenance import provenance_schema; from argus_cortex.consent import consent_schema; [Path(f).write_text(r(), encoding='utf-8') for f, r in (('taxonomy-schema.json', taxonomy_schema), ('provenance-schema.json', provenance_schema), ('consent-schema.json', consent_schema))]"

build:  ## Build sdist + wheel
	$(UV) build

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/argus_cortex/_version.py
