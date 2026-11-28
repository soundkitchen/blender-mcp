# Optional local overrides (silently skipped when absent).
# Exported so child processes inherit them as environment variables.
-include .env
export

PYTHON ?= python
MACOS_BLENDER_BIN = /Applications/Blender.app/Contents/MacOS/Blender
ADDON_MANIFEST = addon/blender_mcp_addon/blender_manifest.toml
PYTHON_SOURCE_DIRS_TO_CHECK = mcp/blmcp/ addon/blender_mcp_addon/ chat_client/ _misc/

define HELP_TEXT

Targets
   * test:              Run unit tests.
   * test_rst_parse:    Run unit tests for RST manual/API doc parsing.
   * test_rst_search:   Run unit tests for the RST text-search layer.
   * test_integration:  Run integration tests (requires BLENDER_BIN).
                        Loads .env if present (e.g. ANTHROPIC_API_KEY).
                        Uses .test_venv (delete to force a rebuild).

     List all tests:    make test_integration TESTS_LIST=1
     Run tests:         make test_integration TESTS=TestChatClient.test_name
     Multiple tests:    make test_integration TESTS="test_one test_two"
   * build_addon:       Build the add-on into an installable zip under build/.
                        Uses BLENDER_BIN, then blender in PATH, then
                        /Applications/Blender.app on macOS.
   * release_addon:     Build the add-on and publish it as a GitHub Release
                        tagged v<version>-fork.<N> (N counts up from 1 per
                        add-on version). Requires a clean main that matches
                        origin/main, and an authenticated gh.
   * format:            Auto-format Python sources with autopep8.
   * readme_update:     Regenerate the tools listing in readme_tools.rst.

Static Source Code Checking
   * check_license:   Verify SPDX headers in all Python files.
   * check_ascii:     Reject non-ASCII characters in sources.
                      Both check files git knows about (tracked, or untracked
                      and not ignored), so .venv/ and build/ are skipped.
                      Ignore rules include .git/info/exclude and the global
                      excludes file, so a new file matching personal ignore
                      rules is checked only once it is added.
   * check_mypy:      Run mypy type checking (fails when mypy is not installed
                      or does not finish).
   * check_pylint:    Run pylint linting (not part of check_all, it still
                      reports existing warnings).
   * check_ruff:      Run ruff linting.
   * check_vulture:   Run vulture dead-code detection.
   * check_namespace: Verify __all__ is defined in all Python modules.
   * check_all:       Run all checks (ruff, mypy, vulture, license, ascii, namespace).

   The tools run as "PYTHON -m <tool>". The development environment at the
   repository root (pyproject.toml) provides them at the versions pinned in
   uv.lock:

     make check_all PYTHON="uv run python"

   Without uv, install mcp/ and mcp/requirements_dev.txt (same tools, no
   versions) for PYTHON.

Reference Data
   * update_reference_manual:
     Copy RST and Python files from a Blender manual checkout.

     Usage: make update_reference_manual MANUAL_DIR=/path/to/manual

   * update_reference_api:
     Copy RST files from a Blender API reference build.

     Usage: make update_reference_api API_DIR=/path/to/api

Environment Variables
   Variables may be set in a .env file (loaded automatically).

   PYTHON              Python interpreter (default: python).
   BLENDER_BIN         Path to the Blender binary (default: blender;
                       build_addon also falls back to /Applications/Blender.app).
   BLENDER_MCP         Path to the blender-mcp command (default: blender-mcp).
   BLENDER_PATH        Path to the Blender binary used by the MCP server
                       (default: blender) and by blender-mcp install-addon
                       (default: blender in PATH, then /Applications/Blender.app
                       on macOS).
   BLENDER_MCP_HOST    Host the MCP addon listens on (default: localhost).
   BLENDER_MCP_PORT    Port the MCP addon listens on (default: 9876).
   BLENDER_MCP_TIMEOUT Startup timeout in seconds for tests (default: 10).
   GLOBAL_TIMEOUT_SCALE
                       Multiply all test timeouts by this factor
                       (default: 1). Useful on slower systems or
                       with slower models.
   BLENDER_MCP_FOREGROUND
                       When set, run Blender in the foreground during tests.
   ANTHROPIC_API_KEY   API key for Claude integration tests.
   ANTHROPIC_MODEL     Model name for Claude tests
                       (default: claude-sonnet-4-20250514).
   USE_LLAMA_CXX       When set, run LLM tests using llama-server.
                       Cannot be combined with USE_ANTHROPIC.
                       Requires LLAMA_SERVER_BIN and
                       LLAMA_SERVER_ARGS.
                       Note: many tests may fail depending on the
                       capability of the model.
   LLAMA_SERVER_BIN    Path to the llama-server binary.
   LLAMA_SERVER_ARGS   Extra arguments for llama-server
                       (e.g. --jinja -m model.gguf).
                       The port is provided by the test harness;
                       do not include --port.
   LLAMA_SERVER_VERBOSE
                       When set, forward llama-server output to
                       the terminal.

endef
export HELP_TEXT

help:
	@echo "$$HELP_TEXT"

test:
	$(PYTHON) tests/test_tool_listing.py
	$(PYTHON) tests/test_install_addon.py
	$(PYTHON) tests/test_rst_parse.py
	$(PYTHON) tests/test_rst_search.py
	$(PYTHON) tests/test_mcp_server.py
	$(PYTHON) tests/test_blender_mcp_with_blender.py

test_rst_parse:
	$(PYTHON) tests/test_rst_parse.py

test_rst_search:
	$(PYTHON) tests/test_rst_search.py

test_integration:
ifdef TESTS_LIST
	@$(PYTHON) _misc/test_integration_tests_list.py
else
	$(PYTHON) tests/integration/test_blender_mcp_with_llm.py $(TESTS)
endif

build_addon:
	@blender_bin="$${BLENDER_BIN:-$$(command -v blender || true)}"; \
	if [ -z "$$blender_bin" ] && [ -x "$(MACOS_BLENDER_BIN)" ]; then \
		blender_bin="$(MACOS_BLENDER_BIN)"; \
	fi; \
	if [ -z "$$blender_bin" ]; then \
		echo "Blender not found: set BLENDER_BIN or add blender to PATH"; exit 1; \
	fi; \
	echo "Using $$blender_bin"; \
	mkdir -p build && \
	"$$blender_bin" --command extension build \
		--source-dir addon/blender_mcp_addon --output-dir build

# Release only what is on origin/main, so the tag matches published sources.
# The tag is created by `gh release create` (on publishing), not pushed beforehand,
# so a failed upload does not leave a tag without a release.
release_addon:
	@set -e; \
	branch="$$(git rev-parse --abbrev-ref HEAD)"; \
	if [ "$$branch" != "main" ]; then \
		echo "release_addon: run on main (current branch: $$branch)"; exit 1; \
	fi; \
	if [ -n "$$(git status --porcelain)" ]; then \
		echo "release_addon: the working tree has uncommitted or untracked changes"; exit 1; \
	fi; \
	if ! gh auth status >/dev/null 2>&1; then \
		echo "release_addon: gh is not authenticated, run gh auth login first"; exit 1; \
	fi; \
	git fetch --quiet origin main; \
	if [ "$$(git rev-parse HEAD)" != "$$(git rev-parse origin/main)" ]; then \
		echo "release_addon: main differs from origin/main, pull or push first"; exit 1; \
	fi; \
	version="$$(sed -n 's/^version = "\(.*\)"$$/\1/p' $(ADDON_MANIFEST))"; \
	if [ -z "$$version" ]; then \
		echo "release_addon: no version found in $(ADDON_MANIFEST)"; exit 1; \
	fi; \
	last="$$(git ls-remote --tags --refs origin "refs/tags/v$$version-fork.*" | \
		sed -n "s|.*refs/tags/v$$version-fork\.\([0-9][0-9]*\)$$|\1|p" | sort -n | tail -n 1)"; \
	tag="v$$version-fork.$$(( $${last:-0} + 1 ))"; \
	zip="build/mcp-$$version.zip"; \
	rm -f "$$zip"; \
	$(MAKE) --no-print-directory build_addon; \
	if [ ! -f "$$zip" ]; then \
		echo "release_addon: $$zip was not built"; exit 1; \
	fi; \
	echo "Releasing $$tag with $$zip"; \
	if ! gh release create "$$tag" "$$zip" --target "$$(git rev-parse HEAD)" --title "$$tag" \
		--notes "Blender add-on built from this fork (upstream add-on version $$version). Install mcp-$$version.zip, not the Source code archives."; then \
		echo "release_addon: gh release create failed."; \
		echo "  GitHub creates the tag only when the release is published, so no tag is left behind,"; \
		echo "  but a draft release may be. Check with 'gh release list' and remove it with"; \
		echo "  'gh release delete $$tag' before running release_addon again."; \
		exit 1; \
	fi; \
	git fetch --quiet origin "refs/tags/$$tag:refs/tags/$$tag"

format:
	@for d in mcp addon _misc tests chat_client; do \
		autopep8 --in-place --recursive $$d || exit 1; \
	done

check_license:
	@$(PYTHON) _misc/check_license.py

check_ascii:
	@$(PYTHON) _misc/check_ascii.py

# Errors are found by grepping the output (errors under stubs/ are ignored),
# so a missing mypy (no ": error:" line) and a crash (exit status 2 or more)
# are checked separately, otherwise they would pass.
check_mypy:
	@$(PYTHON) -m mypy --version >/dev/null 2>&1 || \
		{ echo "mypy: not installed for $(PYTHON) (try PYTHON=\"uv run python\")"; exit 1; }
	@status=0; \
	output="$$($(PYTHON) -m mypy --exclude 'data/api/examples/' $(PYTHON_SOURCE_DIRS_TO_CHECK) 2>&1)" || status=$$?; \
	if [ "$$status" -gt 1 ]; then \
		printf '%s\n' "$$output"; echo "mypy: failed to run (exit status $$status)"; exit 1; \
	fi; \
	if printf '%s\n' "$$output" | grep -v '^stubs/' | grep ': error:'; then \
		echo "mypy: found errors"; exit 1; \
	fi

# `chat_client/` has no `__init__.py`, pylint cannot load it as a directory.
check_pylint:
	$(PYTHON) -m pylint $(patsubst chat_client/,chat_client/chat_client.py,$(PYTHON_SOURCE_DIRS_TO_CHECK)) \
		--disable=C0103,C0115,C0116,C0209,C0413,C0415,R0801,R0903,R0912,R0914,R0915,W0122

check_ruff:
	$(PYTHON) -m ruff check $(PYTHON_SOURCE_DIRS_TO_CHECK)

# `block_text`, `rawtext` and `state_machine` are arguments that docutils passes
# to the directive and role functions in `rst_parse_docs.py`.
check_vulture:
	$(PYTHON) -m vulture $(PYTHON_SOURCE_DIRS_TO_CHECK) \
		--exclude mcp/blmcp/data/api/examples \
		--ignore-decorators '@mcp.tool,@mcp.prompt' \
		--ignore-names 'bl_*,draw,execute,exclude,block_text,rawtext,state_machine' \
		--min-confidence 61

check_namespace:
	@$(PYTHON) _misc/check_namespace.py --skip mcp/blmcp/data/api/examples $(PYTHON_SOURCE_DIRS_TO_CHECK)

check_all: check_ruff check_mypy check_vulture check_license check_ascii check_namespace

readme_update:
	$(PYTHON) _misc/readme_update_from_tools.py

update_reference_manual:
	@test -n "$(MANUAL_DIR)" || { echo "Usage: make update_reference_manual MANUAL_DIR=/path/to/blender/manual"; exit 1; }
	$(PYTHON) _misc/update_reference_manual.py "$(MANUAL_DIR)"

update_reference_api:
	@test -n "$(API_DIR)" || { echo "Usage: make update_reference_api API_DIR=/path/to/api"; exit 1; }
	$(PYTHON) _misc/update_reference_api.py "$(API_DIR)"
