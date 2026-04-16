.PHONY: dist clean

PYTHON ?= python3
PIP_INDEX_URL ?= https://mirror-pypi.runflare.com/
BINARY_NAME ?= code-one
DIST_DIR ?= dist

dist:
	$(PYTHON) -m pip install -i $(PIP_INDEX_URL) .
	$(PYTHON) -m pip install -i $(PIP_INDEX_URL) pyinstaller
	rm -rf build $(DIST_DIR)
	$(PYTHON) -m PyInstaller --onefile --name $(BINARY_NAME) --distpath $(DIST_DIR) --clean --noconfirm code_one_bootstrap.py

clean:
	rm -rf build dist
