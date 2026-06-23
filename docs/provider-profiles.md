# Provider Profile Design Notes

## Why Provider Profiles

Grok-compatible API providers do not always expose the same model set. A single `GROK_API_URL` with multiple model names can mislead users into thinking one provider can serve every search mode. Provider Profiles bind URL, key, endpoint, and model together so `fast` and `deep` can use different providers.

## Routing Rules

- At least one complete profile is required: `GROK_PROVIDER_FAST_*` or `GROK_PROVIDER_DEEP_*`.
- If only one profile is configured, all modes use that profile by default.
- If both profiles are configured, `search_mode=fast` uses FAST and `search_mode=deep` uses DEEP.
- `search_mode=auto` uses conservative query rules, then falls back to any available profile if the selected one is missing.
- Missing FAST or DEEP is not a user-visible warning unless `GROK_STRICT_SEARCH_MODE=true`.

## User-Visible Warnings

Only real failures should appear in `content` or `warnings`: API errors, invalid model, endpoint incompatibility, Grok empty result, or no configured provider. Normal profile fallback is recorded in structured routing fields only.

## Test Notes

Suggested comparison questions:

- 最近 Next.js 16 的主要变化是什么？列出 3 点并给来源。
- OpenAI 官网当前首页主推内容是什么？用中文概括，并列出来源。
- Claude Code 和 Codex 在 MCP 配置方式上有什么差异？基于最新资料回答。

Run each question with `search_mode=fast/deep` and `extra_sources=0/3`. Compare success, latency, source count, warnings, source validity, factual coverage, and provider billing records.
