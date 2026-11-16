# Contributing

Use Python 3.9 or newer on Linux or macOS. The hooks, installers and tests use only the Python standard library; do not add third-party Python dependencies or require newer Python features.

Keep changes focused. Describe the problem and resulting behavior in your pull request, and add a regression test for behavior changes in `tests/run_tests.py`.

## Tests

From the repository root, run:

```sh
env -u CTRLB_KEY -u CTRLB_RUN -u CTRLB_AGENT -u CTRLB_URL -u CTRLB_MCP_URL -u CTRLB_DATASET python3 -I tests/run_tests.py
```

The suite uses synthetic fixtures and a local fake server. Temporary homes and generated files live under `tests/work/`; remove that directory after testing. Optional checks run `claude plugin validate` and `codex mcp list` when those CLIs are installed; the suite also passes without them. On Python 3.9 and 3.10 without Codex, successful TOML installation checks are skipped because the installer needs Python 3.11's `tomllib` or Codex to validate TOML; refusal checks still run. CI covers Python 3.9 and 3.13 on Linux and macOS.

## Public repository

Everything submitted here is public, including source, fixtures, comments, issues and pull requests. Use synthetic examples. Do not include credentials, real session content, personal paths, private repository or service names, or references to unavailable internal plans or decisions. Report vulnerabilities through the private route in [SECURITY.md](SECURITY.md).

## Releases

Update these three versions together:

- `version` in `plugin/.claude-plugin/plugin.json`;
- the `ctrlb-agent` plugin's `version` in `.claude-plugin/marketplace.json`;
- `VERSION` in `lib/common.py`.

Installed Claude Code plugins update only when the plugin version changes. A source change alone does not update an installed plugin, so bump all three versions when shipping changes, including agent instructions. `tests/run_tests.py` also pins the expected plugin version; update that pin with the bump. Run the test suite and merge the version bump to `main` to release the plugin. Do not create a tag or a GitHub release for a plugin release.

GitHub releases in this repository are for the standalone CtrlB binary, whose version is separate from the plugin/client version. Local installation uses `releases/latest`, so never mark a release as latest unless it includes the platform binaries, `SHA256SUMS` and `install.sh`. A release missing those assets breaks local installation. See [Local mode](docs/local-mode.md) for binary installation and updates.
