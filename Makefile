# Post — build helpers
#
# Usage:
#   make deb    # Build a .deb package (output in dist/)

.PHONY: deb help

help:
	@echo "Targets:"
	@echo "  deb  Build a .deb package (output in dist/)"

deb:
	./tools/build-deb.sh
