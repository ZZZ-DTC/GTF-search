import sys
from pathlib import Path

# 支持直接运行：添加 src 目录到 Python 路径
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from fastmcp import FastMCP, Context
from typing import Annotated, Optional
from pydantic import Field
from math import ceil

# 尝试使用绝对导入（支持 mcp run）
try:
    from grok_search.providers.grok import GrokSearchProvider
    from grok_search.logger import log_info
    from grok_search.config import config
    from grok_search.sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from grok_search.planning import engine as planning_engine, _split_csv
except ImportError:
    from .providers.grok import GrokSearchProvider
    from .logger import log_info
    from .config import config
    from .sources import SourcesCache, merge_sources, new_session_id, split_answer_and_sources
    from .planning import engine as planning_engine, _split_csv

import asyncio

mcp = FastMCP("grok-search")

_SOURCES_CACHE = SourcesCache(max_size=256)
_AVAILABLE_MODELS_CACHE: dict[tuple[str, str], list[str]] = {}
_AVAILABLE_MODELS_LOCK = asyncio.Lock()


async def _fetch_available_models(api_url: str, api_key: str) -> list[str]:
    import httpx

    models_url = f"{api_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            models_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

    models: list[str] = []
    for item in (data or {}).get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    return models


async def _get_available_models_cached(api_url: str, api_key: str) -> list[str]:
    key = (api_url, api_key)
    async with _AVAILABLE_MODELS_LOCK:
        if key in _AVAILABLE_MODELS_CACHE:
            return _AVAILABLE_MODELS_CACHE[key]

    try:
        models = await _fetch_available_models(api_url, api_key)
    except Exception:
        models = []

    async with _AVAILABLE_MODELS_LOCK:
        _AVAILABLE_MODELS_CACHE[key] = models
    return models


def _normalize_extra_sources(value: int) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = config.default_extra_sources
    if requested < 0:
        requested = config.default_extra_sources
    return min(requested, config.max_extra_sources)


def _split_extra_source_counts(total: int, has_tavily: bool, has_firecrawl: bool) -> tuple[int, int]:
    if total <= 0:
        return 0, 0
    if has_tavily and not has_firecrawl:
        return total, 0
    if has_firecrawl and not has_tavily:
        return 0, total
    if not has_tavily and not has_firecrawl:
        return 0, 0

    tavily_count = ceil(total * config.tavily_ratio)
    tavily_count = min(total, max(0, tavily_count))
    firecrawl_count = total - tavily_count

    if total >= config.firecrawl_min_total and firecrawl_count == 0:
        firecrawl_count = 1
        tavily_count = total - 1

    return tavily_count, firecrawl_count


def _backend_status(provider: str, requested: int, ok: bool, count: int = 0, error: str = "") -> dict:
    status = {"provider": provider, "requested": requested, "ok": ok, "count": count}
    if error:
        status["error"] = error[:300]
    return status


_DEEP_SEARCH_KEYWORDS = (
    "对比", "比较", "评估", "风险", "方案", "选型", "交叉验证", "深度", "研究", "政策",
    "compare", "comparison", "evaluate", "risk", "strategy", "research", "deep", "policy",
)


def _normalize_search_mode(value: str) -> str:
    mode = (value or config.default_search_mode or "fast").strip().lower()
    if mode in ("fast", "deep", "auto"):
        return mode
    return "fast"


def _select_search_mode(requested_mode: str, query: str, extra_sources: int) -> str:
    if requested_mode in ("fast", "deep"):
        return requested_mode
    query_lower = query.lower()
    if extra_sources >= 5:
        return "deep"
    if any(keyword in query or keyword in query_lower for keyword in _DEEP_SEARCH_KEYWORDS):
        return "deep"
    return "fast"


def _grok_warning_from_status(status: dict) -> dict | None:
    if status.get("provider") != "grok" or status.get("ok"):
        return None
    error = status.get("error") or "empty_result"
    return {
        "code": "grok_empty_result",
        "message": (
            "Grok 主搜索返回为空或不可用，当前回答可能主要依赖 Tavily/Firecrawl 补充信源。"
            "建议运行 diagnose_grok_config 检查 GROK_PROVIDER_*_API_URL、ENDPOINT、MODEL。"
        ),
        "provider": "grok",
        "error": error,
    }


def _prepend_warning(content: str, warning: dict | None) -> str:
    if not warning:
        return content
    notice = f"注意：{warning['message']}"
    if not content:
        return notice
    return f"{notice}\n\n{content}"


def _fallback_answer(extra_sources: list[dict], statuses: list[dict]) -> str:
    failures = [s for s in statuses if not s.get("ok") and (s.get("requested", 0) > 0 or s.get("provider") == "grok")]
    if extra_sources:
        lines = ["Grok 未返回可用回答；以下为补充信源摘要："]
        for item in extra_sources[:5]:
            title = item.get("title") or item.get("url") or "Untitled"
            desc = item.get("description") or item.get("content") or ""
            url = item.get("url") or ""
            provider = item.get("provider") or "source"
            summary = f"- [{provider}] {title} {url}".strip()
            if desc:
                summary += f" — {desc[:180]}"
            lines.append(summary)
        if failures:
            failed = ", ".join(f"{s.get('provider')}({s.get('error', 'failed')})" for s in failures)
            lines.append(f"后端状态：{failed}")
        return "\n".join(lines)

    if failures:
        failed = "; ".join(f"{s.get('provider')}: {s.get('error', 'failed')}" for s in failures)
        return f"搜索失败：{failed}"
    return "搜索未返回可用内容。"


def _extra_results_to_sources(
    tavily_results: list[dict] | None,
    firecrawl_results: list[dict] | None,
) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()

    if firecrawl_results:
        for r in firecrawl_results:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            item: dict = {"url": url, "provider": "firecrawl"}
            title = (r.get("title") or "").strip()
            if title:
                item["title"] = title
            desc = (r.get("description") or "").strip()
            if desc:
                item["description"] = desc
            sources.append(item)

    if tavily_results:
        for r in tavily_results:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            item: dict = {"url": url, "provider": "tavily"}
            title = (r.get("title") or "").strip()
            if title:
                item["title"] = title
            content = (r.get("content") or "").strip()
            if content:
                item["description"] = content
            sources.append(item)

    return sources


@mcp.tool(
    name="web_search",
    output_schema=None,
    description="""
    Before using this tool, please use the plan_intent tool to plan the search carefully.
    Performs a deep web search based on the given query and returns Grok's answer directly.

    This tool extracts sources if provided by upstream, caches them, and returns:
    - session_id: string (When you feel confused or curious about the main content, use this field to invoke the get_sources tool to obtain the corresponding list of information sources)
    - content: string (answer only)
    - sources_count: int
    """,
    meta={"version": "2.0.0", "author": "guda.studio"},
)
async def web_search(
    query: Annotated[str, "Clear, self-contained natural-language search query."],
    platform: Annotated[str, "Target platform to focus on (e.g., 'Twitter', 'GitHub', 'Reddit'). Leave empty for general web search."] = "",
    search_mode: Annotated[str, "Grok search mode: fast, deep, or auto. Empty uses GROK_SEARCH_MODE, default fast."] = "",
    extra_sources: Annotated[int, "Number of additional reference results from Tavily/Firecrawl. Set 0 to disable. Default 1. Values above GROK_SEARCH_MAX_EXTRA_SOURCES are capped."] = 1,
) -> dict:
    session_id = new_session_id()
    effective_extra_sources = _normalize_extra_sources(extra_sources)
    requested_search_mode = _normalize_search_mode(search_mode)
    effective_search_mode = _select_search_mode(requested_search_mode, query, effective_extra_sources)
    try:
        grok_profile, provider_profile_reason = config.resolve_grok_provider(effective_search_mode)
    except ValueError as e:
        await _SOURCES_CACHE.set(session_id, [])
        return {
            "session_id": session_id,
            "content": f"配置错误: {str(e)}",
            "sources_count": 0,
            "warnings": [{"code": "grok_provider_config_error", "message": str(e), "provider": "grok"}],
            "provider_profile_requested": effective_search_mode,
            "provider_profile_used": "",
            "provider_profile_reason": "config_error",
        }

    grok_provider = GrokSearchProvider(
        grok_profile.api_url,
        grok_profile.api_key,
        grok_profile.model,
        grok_profile.endpoint,
    )

    # 计算额外信源配额：默认低成本启用 Tavily；3 条以上才让 Firecrawl 参与托底。
    has_tavily = bool(config.tavily_api_key) and config.tavily_enabled
    has_firecrawl = bool(config.firecrawl_api_key)
    tavily_count, firecrawl_count = _split_extra_source_counts(effective_extra_sources, has_tavily, has_firecrawl)

    # 并行执行搜索任务，保留后端状态，避免静默失败。
    async def _safe_grok() -> tuple[str, dict]:
        try:
            result = await grok_provider.search(query, platform)
            if result:
                return result, _backend_status("grok", 1, True, 1)
            return "", _backend_status("grok", 1, False, 0, "empty_result")
        except Exception as exc:
            return "", _backend_status("grok", 1, False, 0, f"{type(exc).__name__}: {exc}")

    async def _safe_tavily() -> tuple[list[dict] | None, dict]:
        if not tavily_count:
            return None, _backend_status("tavily", 0, False, 0, "not_requested")
        try:
            results = await _call_tavily_search(query, tavily_count)
            count = len(results or [])
            if count:
                return results, _backend_status("tavily", tavily_count, True, count)
            return results, _backend_status("tavily", tavily_count, False, 0, "empty_or_failed")
        except Exception as exc:
            return None, _backend_status("tavily", tavily_count, False, 0, f"{type(exc).__name__}: {exc}")

    async def _safe_firecrawl() -> tuple[list[dict] | None, dict]:
        if not firecrawl_count:
            return None, _backend_status("firecrawl", 0, False, 0, "not_requested")
        try:
            results = await _call_firecrawl_search(query, firecrawl_count)
            count = len(results or [])
            if count:
                return results, _backend_status("firecrawl", firecrawl_count, True, count)
            return results, _backend_status("firecrawl", firecrawl_count, False, 0, "empty_or_failed")
        except Exception as exc:
            return None, _backend_status("firecrawl", firecrawl_count, False, 0, f"{type(exc).__name__}: {exc}")

    grok_pair, tavily_pair, firecrawl_pair = await asyncio.gather(
        _safe_grok(),
        _safe_tavily(),
        _safe_firecrawl(),
    )

    grok_result, grok_status = grok_pair
    tavily_results, tavily_status = tavily_pair
    firecrawl_results, firecrawl_status = firecrawl_pair
    backend_status = [grok_status, tavily_status, firecrawl_status]

    answer, grok_sources = split_answer_and_sources(grok_result or "")
    extra = _extra_results_to_sources(tavily_results, firecrawl_results)
    all_sources = merge_sources(grok_sources, extra)
    warnings = []
    grok_warning = _grok_warning_from_status(grok_status)
    if grok_warning:
        warnings.append(grok_warning)

    if not answer:
        answer = _fallback_answer(extra, backend_status)
    answer = _prepend_warning(answer, grok_warning)

    await _SOURCES_CACHE.set(session_id, all_sources)
    return {
        "session_id": session_id,
        "content": answer,
        "sources_count": len(all_sources),
        "warnings": warnings,
        "provider_profile_requested": effective_search_mode,
        "provider_profile_used": grok_profile.name,
        "provider_profile_reason": provider_profile_reason,
        "model_used": grok_profile.model,
        "endpoint_used": grok_profile.endpoint,
        "search_mode_requested": requested_search_mode,
        "search_mode_effective": effective_search_mode,
        "extra_sources_requested": extra_sources,
        "extra_sources_effective": effective_extra_sources,
        "tavily_count": tavily_count,
        "firecrawl_count": firecrawl_count,
        "backend_status": backend_status,
    }


@mcp.tool(
    name="get_sources",
    description="""
    When you feel confused or curious about the search response content, use the session_id returned by web_search to invoke the this tool to obtain the corresponding list of information sources.
    Retrieve all cached sources for a previous web_search call.
    Provide the session_id returned by web_search to get the full source list.
    """,
    meta={"version": "1.0.0", "author": "guda.studio"},
)
async def get_sources(
    session_id: Annotated[str, "Session ID from previous web_search call."]
) -> dict:
    sources = await _SOURCES_CACHE.get(session_id)
    if sources is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }
    return {"session_id": session_id, "sources": sources, "sources_count": len(sources)}


async def _call_tavily_extract(url: str) -> str | None:
    import httpx
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            if data.get("results") and len(data["results"]) > 0:
                content = data["results"][0].get("raw_content", "")
                return content if content and content.strip() else None
            return None
    except Exception:
        return None


async def _call_tavily_search(query: str, max_results: int = 6) -> list[dict] | None:
    import httpx
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{config.tavily_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_raw_content": False,
        "include_answer": False,
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", ""), "score": r.get("score", 0)}
                for r in results
            ] if results else None
    except Exception:
        return None


async def _call_firecrawl_search(query: str, limit: int = 14) -> list[dict] | None:
    import httpx
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            results = data.get("data", {}).get("web", [])
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
                for r in results
            ] if results else None
    except Exception:
        return None


async def _call_firecrawl_scrape(url: str, ctx=None) -> str | None:
    import httpx
    api_url = config.firecrawl_api_url
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    max_retries = config.retry_max_attempts
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
        }
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
                markdown = data.get("data", {}).get("markdown", "")
                if markdown and markdown.strip():
                    return markdown
                await log_info(ctx, f"Firecrawl: markdown为空, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
        except Exception as e:
            await log_info(ctx, f"Firecrawl error: {e}", config.debug_enabled)
            return None
    return None


@mcp.tool(
    name="web_fetch",
    output_schema=None,
    description="""
    Fetches and extracts complete content from a URL, returning it as a structured Markdown document.

    **Key Features:**
        - **Full Content Extraction:** Retrieves and parses all meaningful content (text, images, links, tables, code blocks).
        - **Markdown Conversion:** Converts HTML structure to well-formatted Markdown with preserved hierarchy.
        - **Content Fidelity:** Maintains 100% content fidelity without summarization or modification.

    **Edge Cases & Best Practices:**
        - Ensure URL is complete and accessible (not behind authentication or paywalls).
        - May not capture dynamically loaded content requiring JavaScript execution.
        - Large pages may take longer to process; consider timeout implications.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def web_fetch(
    url: Annotated[str, "Valid HTTP/HTTPS web address pointing to the target page. Must be complete and accessible."],
    ctx: Context = None
) -> str:
    await log_info(ctx, f"Begin Fetch: {url}", config.debug_enabled)

    result = await _call_tavily_extract(url)
    if result:
        await log_info(ctx, "Fetch Finished (Tavily)!", config.debug_enabled)
        return result

    await log_info(ctx, "Tavily unavailable or failed, trying Firecrawl...", config.debug_enabled)
    result = await _call_firecrawl_scrape(url, ctx)
    if result:
        await log_info(ctx, "Fetch Finished (Firecrawl)!", config.debug_enabled)
        return result

    await log_info(ctx, "Fetch Failed!", config.debug_enabled)
    if not config.tavily_api_key and not config.firecrawl_api_key:
        return "配置错误: TAVILY_API_KEY 和 FIRECRAWL_API_KEY 均未配置"
    return "提取失败: 所有提取服务均未能获取内容"


async def _call_tavily_map(url: str, instructions: str = None, max_depth: int = 1,
                           max_breadth: int = 20, limit: int = 50, timeout: int = 150) -> str:
    import httpx
    import json
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not api_key:
        return "配置错误: TAVILY_API_KEY 未配置，请设置环境变量 TAVILY_API_KEY"
    endpoint = f"{api_url.rstrip('/')}/map"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth, "limit": limit, "timeout": timeout}
    if instructions:
        body["instructions"] = instructions
    try:
        async with httpx.AsyncClient(timeout=float(timeout + 10)) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return json.dumps({
                "base_url": data.get("base_url", ""),
                "results": data.get("results", []),
                "response_time": data.get("response_time", 0)
            }, ensure_ascii=False, indent=2)
    except httpx.TimeoutException:
        return f"映射超时: 请求超过{timeout}秒"
    except httpx.HTTPStatusError as e:
        return f"HTTP错误: {e.response.status_code} - {e.response.text[:200]}"
    except Exception as e:
        return f"映射错误: {str(e)}"


@mcp.tool(
    name="web_map",
    description="""
    Maps a website's structure by traversing it like a graph, discovering URLs and generating a comprehensive site map.

    **Key Features:**
        - **Graph Traversal:** Explores website structure starting from root URL.
        - **Depth & Breadth Control:** Configure traversal limits to balance coverage and performance.
        - **Instruction Filtering:** Use natural language to focus crawler on specific content types.

    **Edge Cases & Best Practices:**
        - Start with low max_depth (1-2) for initial exploration, increase if needed.
        - Use instructions to filter for specific content (e.g., "only documentation pages").
        - Large sites may hit timeout limits; adjust timeout and limit parameters accordingly.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def web_map(
    url: Annotated[str, "Root URL to begin the mapping (e.g., 'https://docs.example.com')."],
    instructions: Annotated[str, "Natural language instructions for the crawler to filter or focus on specific content."] = "",
    max_depth: Annotated[int, Field(description="Maximum depth of mapping from the base URL.", ge=1, le=5)] = 1,
    max_breadth: Annotated[int, Field(description="Maximum number of links to follow per page.", ge=1, le=500)] = 20,
    limit: Annotated[int, Field(description="Total number of links to process before stopping.", ge=1, le=500)] = 50,
    timeout: Annotated[int, Field(description="Maximum time in seconds for the operation.", ge=10, le=150)] = 150
) -> str:
    result = await _call_tavily_map(url, instructions, max_depth, max_breadth, limit, timeout)
    return result


def _sanitize_diagnostic_text(value: str, api_key: str) -> str:
    if not value:
        return ""
    return value.replace(api_key, "***")[:500]


def _extract_probe_text_and_format(body: str) -> tuple[str, str]:
    import json

    chunks: list[str] = []
    detected_format = "unknown"

    def absorb(data: dict, transport: str) -> None:
        nonlocal detected_format
        if not isinstance(data, dict):
            return
        event_type = data.get("type", "")
        if event_type == "response.output_text.delta" or ("output_text" in event_type and "delta" in data):
            chunks.append(data.get("delta", "") or "")
            detected_format = "responses_sse" if transport == "sse" else "responses_json"
            return
        if event_type == "response.completed":
            text = GrokSearchProvider("", "")._extract_responses_text(data.get("response", {}) or {})
            if text:
                chunks.append(text)
                detected_format = "responses_sse" if transport == "sse" else "responses_json"
            return
        choices = data.get("choices", []) or []
        if choices:
            first = choices[0] or {}
            delta = first.get("delta", {}) or {}
            message = first.get("message", {}) or {}
            content = delta.get("content") or message.get("content") or first.get("text") or ""
            if isinstance(content, str) and content:
                chunks.append(content)
                detected_format = "chat_sse" if transport == "sse" else "chat_json"
                return
        if isinstance(data.get("output_text"), str):
            chunks.append(data["output_text"])
            detected_format = "responses_sse" if transport == "sse" else "responses_json"
            return
        if "output" in data:
            text = GrokSearchProvider("", "")._extract_responses_text(data)
            if text:
                chunks.append(text)
                detected_format = "responses_sse" if transport == "sse" else "responses_json"

    stripped = body.strip()
    for line in stripped.splitlines():
        raw = line.strip()
        if not raw.startswith("data:"):
            continue
        raw = raw[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            absorb(json.loads(raw), "sse")
        except json.JSONDecodeError:
            continue

    if chunks:
        return "".join(chunks).strip(), detected_format

    if stripped.startswith("{"):
        try:
            absorb(json.loads(stripped), "json")
        except json.JSONDecodeError:
            pass

    return "".join(chunks).strip(), detected_format


async def _probe_grok_endpoint(api_url: str, api_key: str, model: str, endpoint: str, attempt: int = 1) -> dict:
    import httpx
    import asyncio
    import time

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if endpoint == "responses":
        body = {
            "model": model,
            "input": "用一句中文回复：测试成功",
            "stream": True,
            "reasoning": {"effort": config.responses_reasoning_effort},
        }
    else:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "用一句中文回复：测试成功"}],
            "stream": True,
            "max_tokens": 64,
        }

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            response = await client.post(f"{api_url.rstrip('/')}/{endpoint}", headers=headers, json=body)
        elapsed_ms = round((time.time() - start) * 1000, 2)
        text, response_format = _extract_probe_text_and_format(response.text)
        result = {
            "endpoint": endpoint,
            "status_code": response.status_code,
            "ok": response.status_code < 400 and bool(text),
            "response_time_ms": elapsed_ms,
            "response_format": response_format,
            "sample": text[:80],
        }
        if not result["ok"]:
            result["error"] = _sanitize_diagnostic_text(response.text, api_key)
        if not result["ok"] and attempt < 2 and response.status_code in (408, 429, 500, 502, 503, 504):
            await asyncio.sleep(1)
            return await _probe_grok_endpoint(api_url, api_key, model, endpoint, attempt + 1)
        return result
    except Exception as exc:
        if attempt < 2:
            await asyncio.sleep(1)
            return await _probe_grok_endpoint(api_url, api_key, model, endpoint, attempt + 1)
        return {
            "endpoint": endpoint,
            "status_code": None,
            "ok": False,
            "response_time_ms": round((time.time() - start) * 1000, 2),
            "response_format": "error",
            "sample": "",
            "error": _sanitize_diagnostic_text(f"{type(exc).__name__}: {exc}", api_key),
        }


@mcp.tool(
    name="diagnose_grok_config",
    output_schema=None,
    description="""
    Diagnose Grok API configuration without exposing API keys.

    This tool checks each configured provider profile, including /models, the
    configured model, and chat/responses endpoint probes.
    """,
    meta={"version": "1.0.0", "author": "guda.studio"},
)
async def diagnose_grok_config(
    profile: Annotated[str, "Optional provider profile to diagnose: fast or deep. Empty checks all configured profiles."] = "",
) -> str:
    import json
    import httpx
    import time
    from urllib.parse import urlparse

    requested_profile = profile.strip().lower()
    profiles = config.grok_provider_profiles(include_incomplete=True)
    if requested_profile:
        profiles = [item for item in profiles if item.name == requested_profile]

    if not profiles:
        result = {
            "ok": False,
            "profiles": [],
            "recommendations": [
                "未发现 Grok Provider Profile。请至少配置 GROK_PROVIDER_FAST_* 或 GROK_PROVIDER_DEEP_*。"
            ],
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _diagnose_profile(item) -> dict:
        profile_result = {
            "name": item.name,
            "configured": item.configured,
            "complete": item.complete,
            "enabled": item.enabled,
            "api_url": item.api_url or "未配置",
            "api_key": config._mask_api_key(item.api_key) if item.api_key else "未配置",
            "endpoint": item.endpoint,
            "model": item.model or "未配置",
            "missing_fields": list(item.missing_fields),
            "models_endpoint": None,
            "endpoint_probes": [],
            "recommendations": [],
        }
        if not item.complete:
            profile_result["ok"] = False
            profile_result["recommendations"].append("该 profile 未配置完整，已跳过网络探测。")
            return profile_result

        parsed = urlparse(item.api_url)
        url_has_v1 = parsed.path.rstrip("/").endswith("/v1")
        recommended_url = item.api_url if url_has_v1 else f"{item.api_url}/v1"
        profile_result["url_has_v1_suffix"] = url_has_v1
        profile_result["recommended_api_url_if_needed"] = recommended_url

        headers = {"Authorization": f"Bearer {item.api_key}", "Content-Type": "application/json"}
        models_result = {
            "ok": False,
            "status_code": None,
            "response_time_ms": 0,
            "available_models_count": 0,
            "model_matched": False,
        }
        available_models: list[str] = []
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(f"{item.api_url}/models", headers=headers)
            models_result["status_code"] = response.status_code
            models_result["response_time_ms"] = round((time.time() - start) * 1000, 2)
            if response.status_code == 200:
                data = response.json()
                available_models = [model.get("id") for model in data.get("data", []) if isinstance(model, dict) and model.get("id")]
                models_result["ok"] = True
                models_result["available_models_count"] = len(available_models)
            else:
                models_result["error"] = _sanitize_diagnostic_text(response.text, item.api_key)
        except Exception as exc:
            models_result["response_time_ms"] = round((time.time() - start) * 1000, 2)
            models_result["error"] = _sanitize_diagnostic_text(f"{type(exc).__name__}: {exc}", item.api_key)

        if available_models:
            models_result["model_matched"] = item.model in available_models
            if not models_result["model_matched"]:
                profile_result["recommendations"].append("配置的模型不在 /models 返回列表中；请确认模型名或账号权限。")
        profile_result["models_endpoint"] = models_result

        endpoint_order = []
        for endpoint in [item.endpoint, "chat/completions", "responses"]:
            if endpoint not in endpoint_order:
                endpoint_order.append(endpoint)
        profile_result["endpoint_probes"] = [
            await _probe_grok_endpoint(item.api_url, item.api_key, item.model, endpoint)
            for endpoint in endpoint_order
        ]

        working = [probe for probe in profile_result["endpoint_probes"] if probe.get("ok")]
        if not url_has_v1:
            profile_result["recommendations"].append(f"当前 API URL 看起来不含 /v1；如果请求失败，优先尝试：{recommended_url}")
        if working:
            preferred = "chat/completions" if any(probe["endpoint"] == "chat/completions" for probe in working) else working[0]["endpoint"]
            if preferred != item.endpoint:
                profile_result["recommendations"].append(f"推荐将该 profile endpoint 改为 {preferred}")
        else:
            profile_result["recommendations"].append("没有 endpoint 返回可用文本；请检查 URL、Key、模型权限或服务商兼容性。")
        profile_result["ok"] = bool(working)
        return profile_result

    diagnosed_profiles = [await _diagnose_profile(item) for item in profiles]
    result = {
        "ok": any(item.get("ok") for item in diagnosed_profiles),
        "profiles": diagnosed_profiles,
        "strict_search_mode": config.strict_search_mode,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="get_config_info",
    output_schema=None,
    description="""
    Returns current Grok Search MCP server configuration without making network requests.

    **Key Features:**
        - **Configuration Check:** Shows Provider Profile completeness and current settings.
        - **Secret Safety:** API keys are automatically masked.

    **Edge Cases & Best Practices:**
        - Use diagnose_grok_config for live API connectivity tests.
        - Use this tool first when checking whether env vars were loaded.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def get_config_info() -> str:
    import json
    return json.dumps(config.get_config_info(), ensure_ascii=False, indent=2)


@mcp.tool(
    name="switch_model",
    output_schema=None,
    description="""
    Deprecated. Grok models are now bound to provider profiles. Update
    GROK_PROVIDER_FAST_MODEL or GROK_PROVIDER_DEEP_MODEL in your MCP config instead.
    """,
    meta={"version": "2.0.0", "author": "guda.studio"},
)
async def switch_model(
    model: Annotated[str, "Deprecated. Ignored; configure provider profile model env vars instead."]
) -> str:
    import json
    return json.dumps({
        "status": "deprecated",
        "message": (
            "switch_model 已废弃。Grok 模型现在绑定到 Provider Profile；"
            "请修改 GROK_PROVIDER_FAST_MODEL 或 GROK_PROVIDER_DEEP_MODEL。"
        ),
        "ignored_model": model,
    }, ensure_ascii=False, indent=2)


@mcp.tool(
    name="toggle_builtin_tools",
    output_schema=None,
    description="""
    Toggle Claude Code's built-in WebSearch and WebFetch tools on/off.

    **Key Features:**
        - **Tool Control:** Enable or disable Claude Code's native web tools.
        - **Project Scope:** Changes apply to current project's .claude/settings.json.
        - **Status Check:** Query current state without making changes.

    **Edge Cases & Best Practices:**
        - Use "on" to block built-in tools when preferring this MCP server's implementation.
        - Use "off" to restore Claude Code's native tools.
        - Use "status" to check current configuration without modification.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def toggle_builtin_tools(
    action: Annotated[str, "Action to perform: 'on' (block built-in), 'off' (allow built-in), or 'status' (check current state)."] = "status"
) -> str:
    import json

    # Locate project root
    root = Path.cwd()
    while root != root.parent and not (root / ".git").exists():
        root = root.parent

    settings_path = root / ".claude" / "settings.json"
    tools = ["WebFetch", "WebSearch"]

    # Load or initialize
    if settings_path.exists():
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
    else:
        settings = {"permissions": {"deny": []}}

    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    blocked = all(t in deny for t in tools)

    # Execute action
    if action in ["on", "enable"]:
        for t in tools:
            if t not in deny:
                deny.append(t)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        msg = "官方工具已禁用"
        blocked = True
    elif action in ["off", "disable"]:
        deny[:] = [t for t in deny if t not in tools]
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        msg = "官方工具已启用"
        blocked = False
    else:
        msg = f"官方工具当前{'已禁用' if blocked else '已启用'}"

    return json.dumps({
        "blocked": blocked,
        "deny_list": deny,
        "file": str(settings_path),
        "message": msg
    }, ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_intent",
    output_schema=None,
    description="""
    Phase 1 of search planning: Analyze user intent. Call this FIRST to create a session.
    Returns session_id for subsequent phases. Required flow:
    plan_intent → plan_complexity → plan_sub_query(×N) → plan_search_term(×N) → plan_tool_mapping(×N) → plan_execution

    Required phases depend on complexity: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.
    """,
)
async def plan_intent(
    thought: Annotated[str, "Reasoning for this phase"],
    core_question: Annotated[str, "Distilled core question in one sentence"],
    query_type: Annotated[str, "factual | comparative | exploratory | analytical"],
    time_sensitivity: Annotated[str, "realtime | recent | historical | irrelevant"],
    session_id: Annotated[str, "Empty for new session, or existing ID to revise"] = "",
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    domain: Annotated[str, "Specific domain if identifiable"] = "",
    premise_valid: Annotated[Optional[bool], "False if the question contains a flawed assumption"] = None,
    ambiguities: Annotated[str, "Comma-separated unresolved ambiguities"] = "",
    unverified_terms: Annotated[str, "Comma-separated external terms to verify"] = "",
    is_revision: Annotated[bool, "True to overwrite existing intent"] = False,
) -> str:
    import json
    data = {"core_question": core_question, "query_type": query_type, "time_sensitivity": time_sensitivity}
    if domain:
        data["domain"] = domain
    if premise_valid is not None:
        data["premise_valid"] = premise_valid
    if ambiguities:
        data["ambiguities"] = _split_csv(ambiguities)
    if unverified_terms:
        data["unverified_terms"] = _split_csv(unverified_terms)
    return json.dumps(planning_engine.process_phase(
        phase="intent_analysis", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_complexity",
    output_schema=None,
    description="Phase 2: Assess search complexity (1-3). Controls required phases: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.",
)
async def plan_complexity(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for complexity assessment"],
    level: Annotated[int, "Complexity 1-3"],
    estimated_sub_queries: Annotated[int, "Expected number of sub-queries"],
    estimated_tool_calls: Annotated[int, "Expected total tool calls"],
    justification: Annotated[str, "Why this complexity level"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    return json.dumps(planning_engine.process_phase(
        phase="complexity_assessment", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"level": level, "estimated_sub_queries": estimated_sub_queries,
                     "estimated_tool_calls": estimated_tool_calls, "justification": justification},
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_sub_query",
    output_schema=None,
    description="Phase 3: Add one sub-query. Call once per sub-query; data accumulates across calls. Set is_revision=true to replace all.",
)
async def plan_sub_query(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this sub-query"],
    id: Annotated[str, "Unique ID (e.g., 'sq1')"],
    goal: Annotated[str, "Sub-query goal"],
    expected_output: Annotated[str, "What success looks like"],
    boundary: Annotated[str, "What this excludes — mutual exclusion with siblings"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    depends_on: Annotated[str, "Comma-separated prerequisite IDs"] = "",
    tool_hint: Annotated[str, "web_search | web_fetch | web_map"] = "",
    is_revision: Annotated[bool, "True to replace all sub-queries"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    item = {"id": id, "goal": goal, "expected_output": expected_output, "boundary": boundary}
    if depends_on:
        item["depends_on"] = _split_csv(depends_on)
    if tool_hint:
        item["tool_hint"] = tool_hint
    return json.dumps(planning_engine.process_phase(
        phase="query_decomposition", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=item,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_search_term",
    output_schema=None,
    description="Phase 4: Add one search term. Call once per term; data accumulates. First call must set approach.",
)
async def plan_search_term(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this search term"],
    term: Annotated[str, "Search query (max 8 words)"],
    purpose: Annotated[str, "Sub-query ID this serves (e.g., 'sq1')"],
    round: Annotated[int, "Execution round: 1=broad, 2+=targeted follow-up"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    approach: Annotated[str, "broad_first | narrow_first | targeted (required on first call)"] = "",
    fallback_plan: Annotated[str, "Fallback if primary searches fail"] = "",
    is_revision: Annotated[bool, "True to replace all search terms"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    data = {"search_terms": [{"term": term, "purpose": purpose, "round": round}]}
    if approach:
        data["approach"] = approach
    if fallback_plan:
        data["fallback_plan"] = fallback_plan
    return json.dumps(planning_engine.process_phase(
        phase="search_strategy", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_tool_mapping",
    output_schema=None,
    description="Phase 5: Map a sub-query to a tool. Call once per mapping; data accumulates.",
)
async def plan_tool_mapping(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this mapping"],
    sub_query_id: Annotated[str, "Sub-query ID to map"],
    tool: Annotated[str, "web_search | web_fetch | web_map"],
    reason: Annotated[str, "Why this tool for this sub-query"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    params_json: Annotated[str, "Optional JSON string for tool-specific params"] = "",
    is_revision: Annotated[bool, "True to replace all mappings"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    item = {"sub_query_id": sub_query_id, "tool": tool, "reason": reason}
    if params_json:
        try:
            item["params"] = json.loads(params_json)
        except json.JSONDecodeError:
            pass
    return json.dumps(planning_engine.process_phase(
        phase="tool_selection", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=item,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_execution",
    output_schema=None,
    description="Phase 6: Define execution order. parallel_groups: semicolon-separated groups of comma-separated IDs (e.g., 'sq1,sq2;sq3').",
)
async def plan_execution(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for execution order"],
    parallel_groups: Annotated[str, "Parallel batches: 'sq1,sq2;sq3,sq4' (semicolon=groups, comma=IDs)"],
    sequential: Annotated[str, "Comma-separated IDs that must run in order"],
    estimated_rounds: Annotated[int, "Estimated execution rounds"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return json.dumps({"error": f"Session '{session_id}' not found. Call plan_intent first."})
    parallel = [_split_csv(g) for g in parallel_groups.split(";") if g.strip()] if parallel_groups else []
    seq = _split_csv(sequential)
    return json.dumps(planning_engine.process_phase(
        phase="execution_order", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"parallel": parallel, "sequential": seq, "estimated_rounds": estimated_rounds},
    ), ensure_ascii=False, indent=2)


def main():
    import signal
    import os
    import threading

    # 信号处理（仅主线程）
    if threading.current_thread() is threading.main_thread():
        def handle_shutdown(signum, frame):
            os._exit(0)
        signal.signal(signal.SIGINT, handle_shutdown)
        if sys.platform != 'win32':
            signal.signal(signal.SIGTERM, handle_shutdown)

    # Windows 父进程监控
    if sys.platform == 'win32':
        import time
        import ctypes
        parent_pid = os.getppid()

        def is_parent_alive(pid):
            """Windows 下检查进程是否存活"""
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return True
            exit_code = ctypes.c_ulong()
            result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return result and exit_code.value == STILL_ACTIVE

        def monitor_parent():
            while True:
                if not is_parent_alive(parent_pid):
                    os._exit(0)
                time.sleep(2)

        threading.Thread(target=monitor_parent, daemon=True).start()

    try:
        mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        pass
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
