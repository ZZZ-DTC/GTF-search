# Changelog

## 0.3.0 - 2026-06-23

- Replaced single Grok API configuration with provider profiles.
- Added `GROK_PROVIDER_FAST_*` and `GROK_PROVIDER_DEEP_*` entries so each search mode can use a different API provider, endpoint, and model.
- Added `GROK_STRICT_SEARCH_MODE` for users who want missing profile requests to fail instead of falling back to the only configured profile.
- Updated `web_search` to route by provider profile and return `provider_profile_*`, `model_used`, and `endpoint_used` metadata.
- Updated `diagnose_grok_config` to validate each provider profile independently.
- Deprecated `switch_model`; models are now configured inside provider profiles.

## 0.2.0 - 2026-06-21

- Added `diagnose_grok_config` for manual Grok API diagnostics without exposing API keys.
- Added `search_mode` to `web_search` with `fast`, `deep`, and conservative `auto` routing.
- Added `GROK_MODEL_FAST`, `GROK_MODEL_DEEP`, and `GROK_SEARCH_MODE` configuration.
- Added structured `warnings` and visible content notices when Grok returns empty or unavailable results.
- Changed the default Grok endpoint to `chat/completions` for OpenAI-compatible providers.

## 0.1.0

- Initial fork baseline.
