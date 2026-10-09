from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from datetime import date
import json

from ..errors import PipelineStageError
from ..model_client import ModelClient
from ..models import Clarification, RequestUnderstanding


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
    mode: Literal["query", "analysis"] = "query"
    understanding: RequestUnderstanding = field(default_factory=RequestUnderstanding)
    clarification: dict[str, Any] | None = None


class RequestUnderstandingAgent:
    """一次调用完成上下文聚合、意图判断、检索词提取或直接回复。"""

    def __init__(self, model_client: ModelClient) -> None:
        self.model_client = model_client

    def prepare(self, query: str, recent_context: str, clarifications: list[dict[str, str]] | None = None) -> PreparedRequest:
        system = """你是问数系统的请求预处理器，一次完成上下文聚合、意图判断和必要回复。

意图边界：
1. database_query：用户明确要求查询新的数据库数据，例如获取指标、明细、排名、统计或对比。
   缺少业务口径不会改变查询意图；明确查询/对比但缺少激活观察窗口时仍用database_query，让后续流程澄清。
2. data_qa：用户明确要求解释或分析上下文中已经存在的查询结果、表格或数据，不需要查询新数据。
   需要补查数据库、定位变化贡献、验证解释或分步骤调查时，使用database_query且mode=analysis。
3. direct_response：闲聊、一般交流、能力询问等不需要数据的请求，直接回答。
   如果用户想了解业务情况但目的不明确，可以暂用此路由，并通过下面的需求澄清暂停。

需求理解（面向不懂分析术语的用户）：
- 先理解用户真正想知道什么，而不是要求用户补齐一套技术字段。专业名词、SQL、表名均不需要用户提供。
- 结合相关上文、用户选中的方向及自由补充，能够确定目标和关键范围时，status=ready，正常执行。
  用简洁业务语言整理standard_request，保留目标、指标/对象、已给定的时间/比较范围、必要口径和期望结果。
  明确的问题不需要再确认；无需为了措辞不专业而追问，也不要仅凭confidence分数决定追问。
- 如果“看看运营怎么样”“哪个渠道好”等存在多种实质不同的目的，或者关键范围/时间/指标口径无法确定，
  status=needs_clarification。先用summary表达已理解的部分，再提出一个最有帮助的通俗问题；
  可以给出2–3个不同的建议方向，每个description说明选择后会看什么。若已有合理改写草案，可展示在standard_request，
  但它只是待确认建议，不代表已经获准执行。完全不知道目标时也可以只提问、不提供选项。
- 不得擅自补上用户未确认的日期、激活窗口、目标阈值、因果结论或渠道优劣评价标准。
  例如“哪个渠道好”可以建议看新增人数、激活效果或投放成本；“最近”不能无说明地变成某个固定月。
- 已确认的信息不要反复询问。自由补充优先于先前建议；仍有关键缺口时再询问下一项，
  用户纠正目标时按新目标整理，不将选项编号当作业务需求。不要假装已经查询数据。

上下文聚合：只在当前问题依赖上文时理解和补全语义，不要拼接无关历史。
当action=database_query且需求清楚时，将standard_request填入standalone_query，并提取字段级
Schema检索信息。data_qa也整理standard_request，但不提取Schema信息；direct_response普通回答不必整理需求。
需要澄清时清空standalone_query和retrieval，等待用户补充后重新理解。

只返回JSON：
{
  "action":"database_query|data_qa|direct_response",
  "mode":"query|analysis",
  "confidence":0.0,
  "reason":"...",
  "standalone_query":"问数时填写，其他情况为空字符串",
  "rewritten":false,
  "response":"仅direct_response填写自然语言回答或澄清问题",
  "response_type":"answer|clarification",
  "understanding":{"summary":"一句话描述用户目的", "standard_request":"清楚的需求或待确认建议，无则为空", "status":"ready|needs_clarification"},
  "clarification":null,
  "retrieval":{
    "retrieval_terms":["用于BM25和Embedding的简短Schema检索词，不要写完整句子"],
    "metrics":[], "dimensions":[], "filters":[],
    "time_expressions":[], "operations":[]
  }
}
status=ready时database_query必须填写standalone_query和retrieval，response为空；简单统计、排名或一次查询的对比用mode=query；需根据结果继续补查/下钻的复杂分析用mode=analysis。
data_qa必须清空standalone_query、response和retrieval。
direct_response普通回答必须填写response，并清空standalone_query和retrieval。
status=needs_clarification时，clarification必须是：
{"question":"通俗的确认问题", "reason":"这一选择为什么会改变分析", "options":[{"id":"方向编号", "label":"简短业务方向", "description":"选择后的分析内容", "recommended":false}]}。
options可以为空，不超过3项；建议要作为选择呈现，不能自动选中。不要猜表名。"""
        user = json.dumps({"current_date": date.today().isoformat(), "query": query,
                           "recent_context": recent_context or "无", "clarifications": clarifications or []}, ensure_ascii=False)
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

        raw_understanding = payload.get("understanding") or {}
        understanding = RequestUnderstanding.model_validate(raw_understanding)
        raw_clarification = payload.get("clarification")
        legacy_question = action == "direct_response" and payload.get("response_type") == "clarification"
        if understanding.status == "needs_clarification" or raw_clarification or legacy_question:
            if not isinstance(raw_clarification, dict):
                raw_clarification = {"question": payload.get("response", ""), "reason": reason, "options": []}
            question = Clarification.model_validate({**raw_clarification, "parameter": "request_purpose", "allow_free_text": True})
            question.question = question.question.strip()
            for option in question.options:
                option.id = option.id.strip()
                option.label = option.label.strip()
                option.description = option.description.strip()
            if not question.question.strip() or len(question.options) > 3:
                raise ValueError("需求澄清需要有效问题及至多三个建议方向")
            ids = [option.id for option in question.options]
            if len(set(ids)) != len(ids) or any(not key.strip() for key in ids):
                raise ValueError("需求澄清方向编号必须唯一且非空")
            if any(not option.label.strip() or not option.description.strip() for option in question.options):
                raise ValueError("需求澄清方向必须包含业务描述")
            understanding.status = "needs_clarification"
            return PreparedRequest(action=action, confidence=confidence, reason=reason,
                                   mode="analysis" if payload.get("mode") == "analysis" else "query",
                                   response=question.question, response_type="clarification", understanding=understanding,
                                   clarification=question.model_dump(mode="json"))

        if action == "data_qa":
            understanding.standard_request = understanding.standard_request.strip() or original_query.strip()
            return PreparedRequest(action="data_qa", confidence=confidence, reason=reason, understanding=understanding)

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

        standalone = str(understanding.standard_request or payload.get("standalone_query") or original_query).strip()[:800]
        understanding.standard_request = standalone
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
            mode="analysis" if payload.get("mode") == "analysis" else "query",
            confidence=confidence,
            reason=reason,
            standalone_query=standalone,
            rewritten=bool(payload.get("rewritten", standalone != original_query.strip())),
            retrieval=retrieval,
            understanding=understanding,
        )

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
