PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff
UV_CACHE_DIR ?= /tmp/site2mcp-uv
export UV_CACHE_DIR

.PHONY: setup test demo lint format clean
setup:
	env -u VIRTUAL_ENV uv sync --locked --extra dev

test:
	$(PYTHON) -m unittest discover -s tests -v

demo:
	$(PYTHON) -m demo.verify

lint:
	$(RUFF) check src tests demo
	$(RUFF) format --check src tests demo

format:
	$(RUFF) check --fix src tests demo
	$(RUFF) format src tests demo

clean:
	rm -rf demo-output out build dist
