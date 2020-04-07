SHELL := bash
PYTHON_FILES = *.py

.PHONY: reformat check venv

all: venv

reformat:
	scripts/format-code.sh $(PYTHON_FILES)

check:
	scripts/check-code.sh $(PYTHON_FILES)

venv:
	scripts/create-venv.sh
