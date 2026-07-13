PYTHON := .venv/bin/python
ifeq ($(OS),Windows_NT)
	PYTHON := .venv/Scripts/python.exe
endif

.PHONY: install test fmt workflows

install:
	pip install uv
	uv venv
	uv pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest tests/ $(ARGS)

fmt:
	$(PYTHON) -m black .

workflows:
	python3 workflows/generate_workflows.py $(ARGS)
