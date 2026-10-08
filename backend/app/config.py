from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """加载项目内 .env，不覆盖系统已经提供的变量。"""
    env_path = path or BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


@dataclass(frozen=True)
class Settings:
    # 服务监听配置（可通过环境变量覆盖，便于本地、局域网及部署环境复用）
    server_host: str = os.getenv("SERVER_HOST", "127.0.0.1")
    server_port: int = int(os.getenv("SERVER_PORT", "8003"))
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174",
        ).split(",")
        if origin.strip()
    )
    api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv(
        "LLM_BASE_URL", "https://api.deepseek.com/v1"
    ).rstrip("/")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-flash")
    llm_thinking_format: str = os.getenv("LLM_THINKING_FORMAT", "auto")
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))
    llm_enable_thinking: bool = os.getenv(
        "LLM_ENABLE_THINKING", "false"
    ).lower() in {"1", "true", "yes", "on"}
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
    embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "")
    embedding_base_url: str = os.getenv(
        "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).rstrip("/")
    rerank_model: str = os.getenv("RERANK_MODEL", "qwen3-rerank")
    rerank_api_key: str = os.getenv("RERANK_API_KEY", "")
    rerank_base_url: str = os.getenv(
        "RERANK_BASE_URL", "https://dashscope.aliyuncs.com/compatible-api/v1"
    ).rstrip("/")
    timeout: int = int(os.getenv("LLM_TIMEOUT", "180"))
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
    schema_recall_threshold: float = float(os.getenv("SCHEMA_RECALL_THRESHOLD", "0.55"))
    bm25_top_k: int = int(os.getenv("BM25_TOP_K", "30"))
    dense_top_k: int = int(os.getenv("DENSE_TOP_K", "30"))
    rrf_top_k: int = int(os.getenv("RRF_TOP_K", "40"))
    max_schema_fields: int = int(os.getenv("MAX_SCHEMA_FIELDS", "20"))
    max_saved_memories: int = int(os.getenv("MAX_SAVED_MEMORIES", "20"))
    mcp_max_tool_calls: int = int(os.getenv("MCP_MAX_TOOL_CALLS", "3"))
    short_term_summary_trigger_tokens: int = int(
        os.getenv("SHORT_TERM_SUMMARY_TRIGGER_TOKENS", "12000")
    )
    short_term_summary_enabled: bool = os.getenv(
        "SHORT_TERM_SUMMARY_ENABLED", "true"
    ).lower() in {"1", "true", "yes", "on"}
    short_term_summary_batch_tokens: int = int(
        os.getenv("SHORT_TERM_SUMMARY_BATCH_TOKENS", "6000")
    )
    short_term_min_recent_turns: int = int(
        os.getenv("SHORT_TERM_MIN_RECENT_TURNS", "5")
    )
    session_archive_enabled: bool = os.getenv(
        "SESSION_ARCHIVE_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    session_archive_path: str = os.getenv(
        "SESSION_ARCHIVE_PATH", str(BASE_DIR / "data" / "session_archive.db")
    )
    context_table_row_limit: int = int(os.getenv("CONTEXT_TABLE_ROW_LIMIT", "50"))
    route_context_turns: int = int(os.getenv("ROUTE_CONTEXT_TURNS", "6"))

    @property
    def session_archive_file(self) -> Path:
        path = Path(self.session_archive_path)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def chat_url(self) -> str:
        return f"{self.llm_base_url}/chat/completions"

    @property
    def thinking_format(self) -> str:
        if self.llm_thinking_format != "auto":
            return self.llm_thinking_format
        hostname = urlparse(self.llm_base_url).hostname or ""
        if hostname == "api.deepseek.com":
            return "deepseek"
        if hostname.endswith(".aliyuncs.com"):
            return "enable_thinking"
        return "none"

    @property
    def embedding_key(self) -> str:
        return self.embedding_api_key or self.api_key

    @property
    def rerank_key(self) -> str:
        return self.rerank_api_key or self.api_key

    @property
    def embeddings_url(self) -> str:
        base = self.embedding_base_url or self.llm_base_url
        return f"{base}/embeddings"

    @property
    def rerank_url(self) -> str:
        return f"{self.rerank_base_url}/reranks"

    def public_status(self) -> dict[str, object]:
        return {
            "api_key_configured": bool(self.api_key),
            "llm_model": self.llm_model,
            "llm_enable_thinking": self.llm_enable_thinking,
            "llm_thinking_format": self.thinking_format,
            "llm_max_tokens": self.llm_max_tokens,
            "embedding_model": self.embedding_model,
            "embedding_api_key_configured": bool(self.embedding_key),
            "embedding_dimensions": self.embedding_dimensions,
            "rerank_model": self.rerank_model,
            "rerank_api_key_configured": bool(self.rerank_key),
            "semantic_fallbacks": False,
            "route_availability_fallback": "direct_response_no_database",
            "schema_recall_threshold": self.schema_recall_threshold,
            "bm25_top_k": self.bm25_top_k,
            "dense_top_k": self.dense_top_k,
            "rrf_top_k": self.rrf_top_k,
            "max_schema_fields": self.max_schema_fields,
            "max_saved_memories": self.max_saved_memories,
            "mcp_max_tool_calls": self.mcp_max_tool_calls,
            "short_term_memory": {
                "summary_enabled": self.short_term_summary_enabled,
                "summary_trigger_tokens": self.short_term_summary_trigger_tokens,
                "summary_batch_tokens": self.short_term_summary_batch_tokens,
                "min_recent_turns": self.short_term_min_recent_turns,
                "archive_enabled": self.session_archive_enabled,
                "table_row_limit": self.context_table_row_limit,
            },
            "route_context_turns": self.route_context_turns,
        }


settings = Settings()
