# Post — build helpers
#
# Usage:
#   make deb       # Build a .deb package (output in dist/)
#   make rpm       # Build an .rpm package (output in dist/)
#   make packages  # Build .deb and .rpm (requires both toolchains)

.PHONY: deb rpm packages help

help:
	@echo "Targets:"
	@echo "  deb       Build a .deb package (output in dist/)"
	@echo "  rpm       Build an .rpm package (output in dist/)"
	@echo "  packages  Build .deb and .rpm (requires both toolchains)"

deb:
	./tools/build-deb.sh

rpm:
	./tools/build-rpm.sh

packages:
	./tools/build-packages.sh
