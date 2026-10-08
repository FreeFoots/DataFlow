from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .errors import PipelineStageError
from .model_client import ModelClient


RouteName = Literal["data_qa", "database_query", "direct_response"]
ResponseType = Literal["answer", "clarification"]
logger = logging.getLogger(__name__)


@dataclass
class RetrievalIntent:
    """Schema 检索参数。"""

    retrieval_terms: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    time_expressions: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreparedRequest:
    """请求预处理结果。"""

    action: RouteName
    confidence: float
    reason: str
    standalone_query: str = ""
    rewritten: bool = False
    retrieval: RetrievalIntent = field(default_factory=RetrievalIntent)
    response: str = ""
    response_type: ResponseType = "answer"
    source: str = "model"


class RequestPreprocessor:
    """一次调用完成上下文聚合、意图判断、检索词提取或直接回复。"""

    def __init__(self, model_client: ModelClient) -> None:
        self.model_client = model_client

    def prepare(self, query: str, recent_context: str) -> PreparedRequest:
        system = """你是问数系统的请求预处理器，一次完成上下文聚合、意图判断和必要回复。

意图边界：
1. database_query：用户明确要求查询新的数据库数据，例如获取指标、明细、排名、统计或对比。
2. data_qa：用户明确要求解释或分析上下文中已经存在的查询结果、表格或数据，不需要查询新数据。
3. direct_response：除以上两类之外的请求，包括闲聊、一般交流、能力询问，以及无法确定是
   查询新数据还是分析已有结果的灰色地带。此类请求由你直接回答；如果缺少的信息会影响
   意图判断，则直接提出一个简短的自然语言问题。不要生成选项，不要假装已经查询数据。

上下文聚合：只在当前问题依赖上文时理解和补全语义，不要拼接无关历史。
仅当action=database_query时，将问题改写为可独立理解的standalone_query，并提取字段级
Schema检索信息。data_qa和direct_response不提取Schema信息。

只返回JSON：
{
  "action":"database_query|data_qa|direct_response",
  "confidence":0.0,
  "reason":"...",
  "standalone_query":"问数时填写，其他情况为空字符串",
  "rewritten":false,
  "response":"仅direct_response填写自然语言回答或澄清问题",
  "response_type":"answer|clarification",
  "retrieval":{
    "retrieval_terms":["用于BM25和Embedding的简短Schema检索词，不要写完整句子"],
    "metrics":[], "dimensions":[], "filters":[],
    "time_expressions":[], "operations":[]
  }
}
database_query必须填写standalone_query和retrieval，response为空。
data_qa必须清空standalone_query、response和retrieval。
direct_response必须填写response，并清空standalone_query和retrieval。不要猜表名。"""
        user = f"当前问题：{query}\n近期轻量上下文：{recent_context or '无'}"
        try:
            payload = self.model_client.chat_json(system, user)
        except RuntimeError as exc:
            # 预处理失败时终止下游查询，避免错误路由访问数据库。
            logger.warning(
                "route_fallback_used stage=request_preprocessing action=direct_response error=%s",
                exc,
            )
            return PreparedRequest(
                action="direct_response",
                confidence=0.0,
                reason=f"预处理模型不可用，已启用保守兜底：{exc}",
                response="大模型服务暂时不可用，请稍后重试。",
                source="model_unavailable_fallback",
            )
        try:
            return self._parse(payload, query)
        except (ValueError, KeyError, TypeError) as exc:
            raise PipelineStageError("request_preprocessing", str(exc)) from exc

    def _parse(self, payload: dict[str, Any], original_query: str) -> PreparedRequest:
        action = str(payload["action"])
        if action not in {"data_qa", "database_query", "direct_response"}:
            raise ValueError(f"不支持的路由结果：{action}")

        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.8))))
        reason = str(payload.get("reason") or "模型完成请求预处理")

        if action == "data_qa":
            return PreparedRequest(action="data_qa", confidence=confidence, reason=reason)

        if action == "direct_response":
            response = str(payload.get("response") or "").strip()
            if not response:
                raise ValueError("direct_response缺少response")
            response_type = str(payload.get("response_type") or "answer")
            if response_type not in {"answer", "clarification"}:
                raise ValueError(f"不支持的直接回复类型：{response_type}")
            return PreparedRequest(
                action="direct_response",
                confidence=confidence,
                reason=reason,
                response=response,
                response_type=response_type,
            )

        standalone = str(payload.get("standalone_query") or original_query).strip()[:800]
        raw = payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {}
        retrieval = RetrievalIntent(
            retrieval_terms=self._strings(raw.get("retrieval_terms")),
            metrics=self._strings(raw.get("metrics")),
            dimensions=self._strings(raw.get("dimensions")),
            filters=self._strings(raw.get("filters")),
            time_expressions=self._strings(raw.get("time_expressions")),
            operations=self._strings(raw.get("operations")),
        )
        return PreparedRequest(
            action="database_query",
            confidence=confidence,
            reason=reason,
            standalone_query=standalone,
            rewritten=bool(payload.get("rewritten", standalone != original_query.strip())),
            retrieval=retrieval,
        )

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
