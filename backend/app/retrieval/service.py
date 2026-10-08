from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, Settings, settings
from ..database import RELATIONS, SCHEMA, physical_table_name
from ..errors import PipelineStageError
from ..model_client import ModelClient
from ..querying.duckdb_engine import DuckDbEngine
from ..security import AccessScope
from .store import FieldDocument, LocalSchemaStore


SYNONYMS = {
    # 当前短视频运营库没有订单销售额/收入字段，不将“销售额”错误映射到投放消耗。
    "target_amount": ["销售目标", "目标额", "业绩目标"],
    "order_id": ["订单数", "笔数", "订单量"],
    "region": ["地区", "区域", "大区"],
    "category": ["品类", "类别", "产品类型"],
    "order_date": ["时间", "日期", "下单时间"],
    "status": ["状态", "支付状态", "退款", "取消"],
    "customer_name": ["客户", "客户名称", "企业名称"],
    "customer_level": ["客户等级", "客户层级", "客户级别"],
    "product_name": ["产品", "商品", "产品名称"],
}


class SchemaIndex:
    """字段级混合检索服务。"""

    def __init__(
        self,
        model_client: ModelClient | None = None,
        config: Settings | None = None,
        index_path: Path | None = None,
    ) -> None:
        self.config = config or settings
        self.model_client = model_client or ModelClient(self.config)
        self.index_path = index_path or BASE_DIR / "data" / "schema_store.json"
        self.store = LocalSchemaStore(self.index_path)
        self.data_engine = DuckDbEngine()
        self.embedding_source = "not_built"
        self.last_build_error: str | None = None

    @property
    def documents(self) -> list[FieldDocument]:
        return self.store.active_documents()

    def ensure_built(self, *, force: bool = False) -> None:
        signature = self._schema_signature()
        if (
            not force
            and self.store.signature == signature
            and self.store.documents
            and self.store.embedding_source == self.config.embedding_model
            and self.model_client.enabled
        ):
            self.embedding_source = self.store.embedding_source
            return
        if not force and self.store.load(signature):
            source = self.store.embedding_source
            # 仅加载由当前 Embedding 模型生成的索引。
            if source == self.config.embedding_model and self.model_client.enabled:
                self.embedding_source = source
                return

        raw_documents = self._build_raw_documents()
        semantic_texts = [item["semantic_text"] for item in raw_documents]
        try:
            vectors = self.model_client.embed(semantic_texts)
            self.embedding_source = self.config.embedding_model
            self.last_build_error = None
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            self.last_build_error = str(exc)
            raise PipelineStageError("schema_embedding", str(exc)) from exc

        documents: list[FieldDocument] = []
        for raw, vector in zip(raw_documents, vectors):
            content_hash = hashlib.sha256(
                json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            documents.append(
                FieldDocument(
                    **raw,
                    dense_vector=vector,
                    schema_version=signature[:12],
                    content_hash=content_hash,
                )
            )
        self.store.replace_all(signature, documents, self.embedding_source)

    def retrieve(
        self,
        query: str,
        *,
        retrieval_terms: list[str] | None = None,
        threshold: float | None = None,
        access_scope: AccessScope | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_built()
        terms = list(dict.fromkeys(retrieval_terms or []))
        retrieval_text = " ".join(terms).strip()
        recall_threshold = max(
            0.0,
            min(1.0, threshold if threshold is not None else self.config.schema_recall_threshold),
        )

        scope = (
            access_scope
            if isinstance(access_scope, AccessScope)
            else AccessScope.from_dict(access_scope)
            if access_scope is not None
            else None
        )
        allowed_doc_ids = (
            {
                document.doc_id
                for document in self.documents
                if scope and scope.allows_table(document.database_id, document.table_id)
            }
            if scope
            else None
        )
        if allowed_doc_ids == set():
            return self._empty_retrieval(query, terms, recall_threshold)

        # BM25 与向量召回使用同一组检索词。
        bm25 = (
            self.store.bm25_search(
                retrieval_text,
                self.config.bm25_top_k,
                allowed_doc_ids,
            )
            if retrieval_text
            else []
        )
        dense = (
            self.store.dense_search(
                self._query_vector(retrieval_text),
                self.config.dense_top_k,
                allowed_doc_ids,
            )
            if retrieval_text
            else []
        )
        bm25_rank = {index: rank for rank, (index, _) in enumerate(bm25, 1)}
        dense_rank = {index: rank for rank, (index, _) in enumerate(dense, 1)}
        bm25_scores = dict(bm25)
        dense_scores = dict(dense)

        fused: dict[int, float] = {}
        for rank_map in (bm25_rank, dense_rank):
            for index, rank in rank_map.items():
                fused[index] = fused.get(index, 0.0) + 1 / (60 + rank)
        rrf = sorted(fused.items(), key=lambda item: item[1], reverse=True)[: self.config.rrf_top_k]
        candidate_indexes = [index for index, _ in rrf]

        if not candidate_indexes:
            return self._empty_retrieval(query, terms, recall_threshold)

        rerank_source = self.config.rerank_model
        try:
            texts = [self.documents[index].rerank_text for index in candidate_indexes]
            result = self.model_client.rerank(query, texts, len(texts))
            rerank_scores = {
                candidate_indexes[local_index]: max(0.0, min(1.0, float(score)))
                for local_index, score in result
                if 0 <= local_index < len(candidate_indexes)
            }
            if not rerank_scores:
                raise RuntimeError("Rerank返回为空")
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise PipelineStageError("schema_rerank", str(exc)) from exc

        # Rerank 分数低于阈值的字段不进入 Schema 图。
        ranked = sorted(
            ((index, rerank_scores.get(index, 0.0)) for index in candidate_indexes),
            key=lambda item: item[1],
            reverse=True,
        )
        selected = [item for item in ranked if item[1] >= recall_threshold][: self.config.max_schema_fields]

        hits = [
            self._hit(index, score, bm25_rank, dense_rank, bm25_scores, dense_scores, fused)
            for index, score in selected
        ]
        low_confidence = [
            {
                "doc_id": self.documents[index].doc_id,
                "table_id": self.documents[index].table_id,
                "table_label": self.documents[index].table_label,
                "field_name": self.documents[index].field_name,
                "field_label": self.documents[index].field_label,
                "score": round(float(score), 6),
            }
            for index, score in ranked[:8]
        ]
        return {
            "query": query,
            "retrieval_terms": terms,
            "embedding_source": self.embedding_source,
            "rerank_source": rerank_source,
            "threshold": recall_threshold,
            "bm25_count": len(bm25),
            "dense_count": len(dense),
            "rrf_count": len(candidate_indexes),
            "candidate_count": len(candidate_indexes),
            "selected_count": len(hits),
            "hits": hits,
            "table_candidates": self._table_candidates(hits),
            "low_confidence_candidates": low_confidence,
        }

    def include_workspace(
        self,
        retrieval: dict[str, Any],
        workspace: dict[str, Any],
        access_scope: AccessScope | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """将用户确认的字段加入检索结果。"""
        scope = (
            access_scope
            if isinstance(access_scope, AccessScope)
            else AccessScope.from_dict(access_scope)
            if access_scope is not None
            else None
        )
        confirmed_tables = set(workspace.get("confirmed_schema_tables") or [])
        confirmed_fields = {
            (item.get("tableId"), item.get("name"))
            for item in workspace.get("schema_fields", [])
            if item.get("name")
        }
        hits = list(retrieval.get("hits", []))
        known = {hit["doc_id"] for hit in hits}
        for document in self.documents:
            explicit_field = (document.table_id, document.field_name) in confirmed_fields
            legacy_field = (None, document.field_name) in confirmed_fields
            if document.table_id not in confirmed_tables and not explicit_field and not legacy_field:
                continue
            if scope and not scope.allows_table(document.database_id, document.table_id):
                if document.table_id in confirmed_tables or explicit_field:
                    raise PipelineStageError(
                        "schema_access_control",
                        "用户确认内容包含无权访问的Schema字段",
                    )
                continue
            if document.doc_id in known:
                hit = next(hit for hit in hits if hit["doc_id"] == document.doc_id)
                hit.update({
                    "source": "user_confirmed",
                    "selection_reason": "user_confirmed",
                    "score": 1.0,
                    "rerank_score": 1.0,
                })
                continue
            hits.append({
                **document.public(),
                "score": 1.0,
                "bm25_rank": None,
                "dense_rank": None,
                "rrf_score": 0.0,
                "rerank_score": 1.0,
                "keyword_score": 0.0,
                "vector_score": 0.0,
                "selection_reason": "user_confirmed",
                "source": "user_confirmed",
            })
            known.add(document.doc_id)
        retrieval["hits"] = hits
        retrieval["selected_count"] = len(hits)
        retrieval["table_candidates"] = self._table_candidates(hits)
        return retrieval

    def _empty_retrieval(
        self,
        query: str,
        terms: list[str],
        threshold: float,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "retrieval_terms": terms,
            "embedding_source": self.embedding_source,
            "rerank_source": self.config.rerank_model,
            "threshold": threshold,
            "bm25_count": 0,
            "dense_count": 0,
            "rrf_count": 0,
            "candidate_count": 0,
            "selected_count": 0,
            "hits": [],
            "table_candidates": [],
            "low_confidence_candidates": [],
        }

    def status(self) -> dict[str, Any]:
        return {
            "built": bool(self.documents),
            "document_count": len(self.documents),
            "embedding_source": self.embedding_source,
            "last_build_error": self.last_build_error,
            "threshold": self.config.schema_recall_threshold,
            "store": self.store.status(),
        }

    def _build_raw_documents(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        database_names = sorted({table.get("database", "short_video_ops") for table in SCHEMA})
        for database in database_names:
            tables = [
                item
                for item in SCHEMA
                if item.get("database", "short_video_ops") == database
            ]
            prebuilt = [
                table
                for table in tables
                if all(field.get("data_profile") and field.get("index_content") for field in table["fields"])
            ]
            for table in prebuilt:
                self._append_table_documents(output, None, table)
            dynamic = [table for table in tables if table not in prebuilt]
            if not dynamic:
                continue
            # 每个数据库复用一个 DuckDB 连接构建字段文档。
            with self.data_engine.connect(database) as connection:
                for table in dynamic:
                    self._append_table_documents(output, connection, table)
        return output

    def _append_table_documents(
        self, output: list[dict[str, Any]], connection: Any, table: dict[str, Any]
    ) -> None:
        """根据 CSV 样例和字段分布构建索引文档。"""
        sql_table_name = physical_table_name(table)
        for field in table["fields"]:
            stored_profile = field.get("data_profile")
            if isinstance(stored_profile, dict):
                samples = list(stored_profile.get("sample_values") or [])
                profile = self._stored_profile_text(stored_profile)
            else:
                samples = [
                    DuckDbEngine._json_value(row[0])
                    for row in connection.execute(
                        f'SELECT DISTINCT "{field["name"]}" FROM "{sql_table_name}" '
                        f'WHERE "{field["name"]}" IS NOT NULL LIMIT 5'
                    ).fetchall()
                ]
                profile = self._field_profile(connection, sql_table_name, field)
            aliases = list(dict.fromkeys([
                *field.get("aliases", []), *SYNONYMS.get(field["name"], [])
            ]))
            samples_text = "、".join(str(item) for item in samples)
            relations = [
                f"{item['left_table']}.{item['left_field']}={item['right_table']}.{item['right_field']}"
                for item in RELATIONS
                if table["id"] in {item["left_table"], item["right_table"]}
                and field["name"] in {item["left_field"], item["right_field"]}
            ]
            default_keyword_text = " ".join([
                table.get("database", "short_video_ops"), table.get("domain", ""),
                table["id"], sql_table_name, table["label"], *table.get("business_terms", []),
                field["name"], field["label"], field.get("description", ""),
                *aliases, samples_text,
            ])
            default_semantic_text = (
                f"{field['label']}（{field['name']}）属于{table['label']}。"
                f"表用途：{table['description']}。字段含义：{field.get('description', '')}。"
                f"业务表达：{'、'.join(aliases)}。数据特征：{profile}。样例：{samples_text}。"
            )
            default_rerank_text = (
                f"数据库字段 {sql_table_name}.{field['name']}；中文名：{field['label']}；"
                f"字段含义：{field.get('description', '')}；角色：{field.get('role', '')}；"
                f"所属表：{table['label']}；表业务：{table['description']}；"
                f"同义词：{'、'.join(aliases)}；样例：{samples_text}；分布：{profile}；"
                f"关联：{'、'.join(relations) or '无'}。"
            )
            index_content = field.get("index_content") or {}
            keyword_text = str(index_content.get("keyword_text") or default_keyword_text)
            semantic_text = str(index_content.get("vector_text") or default_semantic_text)
            rerank_text = str(index_content.get("rerank_text") or default_rerank_text)
            output.append({
                "doc_id": f"{table['id']}.{field['name']}",
                "database_id": table.get("database", "short_video_ops"),
                "table_id": table["id"],
                "table_label": table["label"],
                "field_name": field["name"],
                "field_label": field["label"],
                "field_type": field["type"],
                "field_description": field.get("description", ""),
                "field_role": field.get("role", ""),
                "aliases": aliases,
                "samples": samples,
                "profile": profile,
                "keyword_text": keyword_text,
                "semantic_text": semantic_text,
                "rerank_text": rerank_text,
                "relation_ids": relations,
            })

    @staticmethod
    def _stored_profile_text(profile: dict[str, Any]) -> str:
        parts = [
            f"空值率 {float(profile.get('null_ratio') or 0):.2%}",
            f"不同值 {profile.get('distinct_count', 0)} 个",
        ]
        if profile.get("min") is not None:
            parts.append(f"范围 {profile.get('min')} 至 {profile.get('max')}")
        if profile.get("avg") is not None:
            parts.append(f"平均值 {profile.get('avg')}")
        if profile.get("unit"):
            parts.append(f"单位 {profile.get('unit')}")
        return "，".join(parts)

    @staticmethod
    def _field_profile(connection: Any, table_id: str, field: dict[str, Any]) -> str:
        table = f'"{table_id}"'
        column = f'"{field["name"]}"'
        if field["type"] in {"数值", "整数"}:
            row = connection.execute(
                f"SELECT MIN({column}), MAX({column}), AVG({column}) FROM {table}"
            ).fetchone()
            average = round(row[2], 2) if row[2] is not None else "无"
            return f"最小值 {row[0]}，最大值 {row[1]}，平均值 {average}"
        if field["type"] == "日期":
            row = connection.execute(f"SELECT MIN({column}), MAX({column}) FROM {table}").fetchone()
            return f"范围 {row[0]} 至 {row[1]}"
        count = connection.execute(f"SELECT COUNT(DISTINCT {column}) FROM {table}").fetchone()[0]
        return f"约 {count} 个不同值"

    def _query_vector(self, text: str) -> list[float]:
        try:
            return self.model_client.embed([text])[0]
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise PipelineStageError("query_embedding", str(exc)) from exc

    def _hit(
        self,
        index: int,
        score: float,
        bm25_rank: dict[int, int],
        dense_rank: dict[int, int],
        bm25_scores: dict[int, float],
        dense_scores: dict[int, float],
        fused: dict[int, float],
    ) -> dict[str, Any]:
        return {
            **self.documents[index].public(),
            "score": round(float(score), 6),
            "rerank_score": round(float(score), 6),
            "bm25_rank": bm25_rank.get(index),
            "dense_rank": dense_rank.get(index),
            "rrf_score": round(fused.get(index, 0.0), 8),
            "keyword_score": round(bm25_scores.get(index, 0.0), 6),
            "vector_score": round(dense_scores.get(index, 0.0), 6),
            "selection_reason": "rerank_threshold",
            "source": "retrieval",
        }

    @staticmethod
    def _table_candidates(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for hit in hits:
            item = grouped.setdefault(hit["table_id"], {
                "table_id": hit["table_id"],
                "table_label": hit["table_label"],
                "score": 0.0,
                "field_count": 0,
            })
            item["score"] = max(item["score"], float(hit.get("score", 0)))
            item["field_count"] += 1
        return sorted(grouped.values(), key=lambda item: item["score"], reverse=True)

    def _schema_signature(self) -> str:
        payload = json.dumps(
            {
                "store_version": "csv_duckdb_v2",
                "embedding_model": self.config.embedding_model,
                "embedding_dimensions": self.config.embedding_dimensions,
                "embedding_base_url": self.config.embedding_base_url,
                "schema": SCHEMA,
                "relations": RELATIONS,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
