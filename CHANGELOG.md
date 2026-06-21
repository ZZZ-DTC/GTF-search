# Changelog

## 0.2.0 - 2026-06-21

- Added `diagnose_grok_config` for manual Grok API diagnostics without exposing API keys.
- Added `search_mode` to `web_search` with `fast`, `deep`, and conservative `auto` routing.
- Added `GROK_MODEL_FAST`, `GROK_MODEL_DEEP`, and `GROK_SEARCH_MODE` configuration.
- Added structured `warnings` and visible content notices when Grok returns empty or unavailable results.
- Changed the default Grok endpoint to `chat/completions` for OpenAI-compatible providers.

## 0.1.0

- Initial fork baseline.
