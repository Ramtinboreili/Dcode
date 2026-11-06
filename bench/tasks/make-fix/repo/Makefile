OUT_DIR = out
BUNDLE = $(OUT_DIR)/bundle.txt

build: $(BUNDLE)

$(BUNDLE): $(wildcard src/*.txt)
    mkdir -p $(OUT_DIR)
    cat $^ > $@

test:
	./run_tests.sh

clean:
	rm -rf $(OUTDIR)
