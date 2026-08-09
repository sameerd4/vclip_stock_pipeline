.PHONY: dev test lint clean

PYTHON ?= python3
VENV := .venv
INSTALL_MARKER := $(VENV)/.vclip-installed

$(VENV)/bin/python:
	$(PYTHON) -m venv $(VENV)

$(INSTALL_MARKER): $(VENV)/bin/python pyproject.toml README.md
	$(VENV)/bin/python -m pip install --upgrade pip setuptools wheel
	$(VENV)/bin/python -m pip install -e ".[dev,visual]"
	touch $(INSTALL_MARKER)

dev: $(INSTALL_MARKER)

test: $(INSTALL_MARKER)
	$(VENV)/bin/python -m pytest

lint: $(INSTALL_MARKER)
	$(VENV)/bin/python -m ruff check src tests

clean:
	rm -rf $(VENV) build dist .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.py[co]' -delete
