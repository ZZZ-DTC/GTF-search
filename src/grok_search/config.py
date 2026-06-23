import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GrokProviderProfile:
    name: str
    api_url: str = ""
    api_key: str = ""
    endpoint: str = "chat/completions"
    model: str = ""
    enabled: bool = True
    missing_fields: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.api_url or self.api_key or self.model)

    @property
    def complete(self) -> bool:
        return self.enabled and not self.missing_fields


class Config:
    _instance = None
    _SETUP_COMMAND = (
        "Configure at least one Grok provider profile, e.g. "
        "GROK_PROVIDER_FAST_API_URL/GROK_PROVIDER_FAST_API_KEY/"
        "GROK_PROVIDER_FAST_MODEL."
    )
    _DEFAULT_GUDA_BASE_URL = "https://code.guda.studio"
    _PROFILE_NAMES = ("fast", "deep")

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_file = None
        return cls._instance

    @property
    def config_file(self) -> Path:
        if self._config_file is None:
            config_dir = Path.home() / ".config" / "grok-search"
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                config_dir = Path.cwd() / ".grok-search"
                config_dir.mkdir(parents=True, exist_ok=True)
            self._config_file = config_dir / "config.json"
        return self._config_file

    def _load_config_file(self) -> dict:
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_config_file(self, config_data: dict) -> None:
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            raise ValueError(f"无法保存配置文件: {str(e)}")

    @property
    def debug_enabled(self) -> bool:
        return os.getenv("GROK_DEBUG", "false").lower() in ("true", "1", "yes")

    @property
    def retry_max_attempts(self) -> int:
        return int(os.getenv("GROK_RETRY_MAX_ATTEMPTS", "3"))

    @property
    def retry_multiplier(self) -> float:
        return float(os.getenv("GROK_RETRY_MULTIPLIER", "1"))

    @property
    def retry_max_wait(self) -> int:
        return int(os.getenv("GROK_RETRY_MAX_WAIT", "10"))

    @property
    def default_extra_sources(self) -> int:
        return max(0, int(os.getenv("GROK_SEARCH_DEFAULT_EXTRA_SOURCES", "1")))

    @property
    def max_extra_sources(self) -> int:
        return max(0, int(os.getenv("GROK_SEARCH_MAX_EXTRA_SOURCES", "10")))

    @property
    def tavily_ratio(self) -> float:
        raw = float(os.getenv("GROK_SEARCH_TAVILY_RATIO", "0.7"))
        return min(1.0, max(0.0, raw))

    @property
    def firecrawl_min_total(self) -> int:
        return max(1, int(os.getenv("GROK_SEARCH_FIRECRAWL_MIN_TOTAL", "3")))

    @property
    def guda_base_url(self) -> str:
        return os.getenv("GUDA_BASE_URL", self._DEFAULT_GUDA_BASE_URL)

    @property
    def guda_api_key(self) -> str | None:
        return os.getenv("GUDA_API_KEY")

    @property
    def tavily_enabled(self) -> bool:
        return os.getenv("TAVILY_ENABLED", "true").lower() in ("true", "1", "yes")

    @property
    def tavily_api_url(self) -> str:
        url = os.getenv("TAVILY_API_URL")
        if not url and self.guda_api_key:
            return f"{self.guda_base_url}/tavily"
        return url or "https://api.tavily.com"

    @property
    def tavily_api_key(self) -> str | None:
        return os.getenv("TAVILY_API_KEY") or self.guda_api_key

    @property
    def firecrawl_api_url(self) -> str:
        url = os.getenv("FIRECRAWL_API_URL")
        if not url and self.guda_api_key:
            return f"{self.guda_base_url}/firecrawl"
        return url or "https://api.firecrawl.dev/v2"

    @property
    def firecrawl_api_key(self) -> str | None:
        return os.getenv("FIRECRAWL_API_KEY") or self.guda_api_key

    @property
    def log_level(self) -> str:
        return os.getenv("GROK_LOG_LEVEL", "INFO").upper()

    @property
    def log_dir(self) -> Path:
        log_dir_str = os.getenv("GROK_LOG_DIR", "logs")
        log_dir = Path(log_dir_str)
        if log_dir.is_absolute():
            return log_dir

        home_log_dir = Path.home() / ".config" / "grok-search" / log_dir_str
        try:
            home_log_dir.mkdir(parents=True, exist_ok=True)
            return home_log_dir
        except OSError:
            pass

        cwd_log_dir = Path.cwd() / log_dir_str
        try:
            cwd_log_dir.mkdir(parents=True, exist_ok=True)
            return cwd_log_dir
        except OSError:
            pass

        tmp_log_dir = Path("/tmp") / "grok-search" / log_dir_str
        tmp_log_dir.mkdir(parents=True, exist_ok=True)
        return tmp_log_dir

    @property
    def default_search_mode(self) -> str:
        mode = os.getenv("GROK_SEARCH_MODE", "fast").strip().lower()
        if mode in ("fast", "deep", "auto"):
            return mode
        return "fast"

    @property
    def strict_search_mode(self) -> bool:
        return os.getenv("GROK_STRICT_SEARCH_MODE", "false").lower() in ("true", "1", "yes")

    @property
    def responses_reasoning_effort(self) -> str:
        return os.getenv("GROK_RESPONSES_REASONING_EFFORT", "none")

    def normalize_endpoint(self, endpoint: str | None) -> str:
        raw = (endpoint or "chat/completions").strip().lower()
        if raw in ("chat", "chat_completions", "chat/completions"):
            return "chat/completions"
        if raw in ("response", "responses", "v1/responses"):
            return "responses"
        return raw.strip("/") or "chat/completions"

    def _apply_model_suffix(self, model: str, api_url: str) -> str:
        if "openrouter" in api_url and ":online" not in model:
            return f"{model}:online"
        return model

    def _profile_from_env(self, name: str) -> GrokProviderProfile:
        prefix = f"GROK_PROVIDER_{name.upper()}_"
        api_url = os.getenv(f"{prefix}API_URL", "").strip().rstrip("/")
        api_key = os.getenv(f"{prefix}API_KEY", "")
        endpoint = self.normalize_endpoint(os.getenv(f"{prefix}ENDPOINT", "chat/completions"))
        model = os.getenv(f"{prefix}MODEL", "").strip()
        enabled = os.getenv(f"{prefix}ENABLED", "true").lower() in ("true", "1", "yes")

        missing: list[str] = []
        if not api_url:
            missing.append(f"{prefix}API_URL")
        if not api_key:
            missing.append(f"{prefix}API_KEY")
        if not model:
            missing.append(f"{prefix}MODEL")

        if model and api_url:
            model = self._apply_model_suffix(model, api_url)

        return GrokProviderProfile(
            name=name,
            api_url=api_url,
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            enabled=enabled,
            missing_fields=tuple(missing),
        )

    def grok_provider_profiles(self, include_incomplete: bool = False) -> list[GrokProviderProfile]:
        profiles = [self._profile_from_env(name) for name in self._PROFILE_NAMES]
        if include_incomplete:
            return [profile for profile in profiles if profile.configured]
        return [profile for profile in profiles if profile.complete]

    def resolve_grok_provider(self, search_mode: str) -> tuple[GrokProviderProfile, str]:
        requested = search_mode if search_mode in self._PROFILE_NAMES else self.default_search_mode
        if requested not in self._PROFILE_NAMES:
            requested = "fast"

        profiles = {profile.name: profile for profile in self.grok_provider_profiles()}
        if not profiles:
            raise ValueError(
                "Grok Provider Profile 未配置。请至少配置一组 "
                "GROK_PROVIDER_FAST_* 或 GROK_PROVIDER_DEEP_*。"
            )

        if requested in profiles:
            return profiles[requested], "requested_profile"

        if self.strict_search_mode:
            raise ValueError(f"Grok provider profile '{requested}' 未配置，且 GROK_STRICT_SEARCH_MODE=true。")

        fallback_name = "fast" if "fast" in profiles else "deep"
        return profiles[fallback_name], "only_configured_profile"

    @staticmethod
    def _mask_api_key(key: str | None) -> str:
        if not key or len(key) <= 8:
            return "***"
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    def _profile_info(self, profile: GrokProviderProfile) -> dict:
        return {
            "name": profile.name,
            "configured": profile.configured,
            "complete": profile.complete,
            "enabled": profile.enabled,
            "api_url": profile.api_url or "未配置",
            "api_key": self._mask_api_key(profile.api_key) if profile.api_key else "未配置",
            "endpoint": profile.endpoint,
            "model": profile.model or "未配置",
            "missing_fields": list(profile.missing_fields),
        }

    def get_config_info(self) -> dict:
        profiles = [self._profile_info(profile) for profile in self.grok_provider_profiles(include_incomplete=True)]
        complete_profiles = [profile for profile in profiles if profile["complete"]]
        config_status = "✅ 配置完整" if complete_profiles else "❌ Grok Provider Profile 未配置完整"

        return {
            "GUDA_BASE_URL": self.guda_base_url,
            "GUDA_API_KEY": self._mask_api_key(self.guda_api_key) if self.guda_api_key else "未配置",
            "GROK_SEARCH_MODE": self.default_search_mode,
            "GROK_STRICT_SEARCH_MODE": self.strict_search_mode,
            "GROK_PROVIDER_PROFILES": profiles,
            "GROK_DEBUG": self.debug_enabled,
            "GROK_LOG_LEVEL": self.log_level,
            "GROK_LOG_DIR": str(self.log_dir),
            "TAVILY_API_URL": self.tavily_api_url,
            "TAVILY_ENABLED": self.tavily_enabled,
            "TAVILY_API_KEY": self._mask_api_key(self.tavily_api_key) if self.tavily_api_key else "未配置",
            "FIRECRAWL_API_URL": self.firecrawl_api_url,
            "FIRECRAWL_API_KEY": self._mask_api_key(self.firecrawl_api_key) if self.firecrawl_api_key else "未配置",
            "config_status": config_status,
        }


config = Config()
