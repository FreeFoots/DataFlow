from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FieldDocument:
    """字段级 Schema 索引文档。"""

    doc_id: str
    database_id: str
    table_id: str
    table_label: str
    field_name: str
    field_label: str
    field_type: str
    field_description: str
    field_role: str
    aliases: list[str]
    samples: list[Any]
    profile: str
    keyword_text: str
    semantic_text: str
    rerank_text: str
    dense_vector: list[float]
    schema_version: str
    content_hash: str
    active: bool = True
    relation_ids: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("dense_vector", None)
        # 前端使用 vector_text 属性。
        payload["vector_text"] = payload.pop("semantic_text")
        return payload


class LocalSchemaStore:
    """基于 JSON 文件的本地 Schema 存储。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.signature = ""
        self.embedding_source = ""
        self.documents: list[FieldDocument] = []

    def load(self, expected_signature: str) -> bool:
        if not self.path.exists():
            return False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("signature") != expected_signature:
                return False
            self.signature = expected_signature
            self.embedding_source = str(payload.get("embedding_source") or "")
            self.documents = [FieldDocument(**item) for item in payload["documents"]]
            return True
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def replace_all(
        self, signature: str, documents: list[FieldDocument], embedding_source: str
    ) -> None:
        self.signature = signature
        self.embedding_source = embedding_source
        self.documents = documents
        self._save()

    def upsert(self, documents: list[FieldDocument]) -> None:
        """按字段标识增量更新文档。"""
        indexed = {item.doc_id: item for item in self.documents}
        indexed.update({item.doc_id: item for item in documents})
        self.documents = list(indexed.values())
        self._save()

    def deactivate(self, doc_ids: list[str]) -> None:
        targets = set(doc_ids)
        for document in self.documents:
            if document.doc_id in targets:
                document.active = False
        self._save()

    def get(self, doc_id: str) -> FieldDocument | None:
        return next((item for item in self.documents if item.doc_id == doc_id and item.active), None)

    def active_documents(self) -> list[FieldDocument]:
        return [item for item in self.documents if item.active]

    def bm25_search(
        self,
        query: str,
        top_k: int,
        allowed_doc_ids: set[str] | None = None,
    ) -> list[tuple[int, float]]:
        """在 keyword_text 上执行 BM25 检索。"""
        documents = self.active_documents()
        candidates = [
            (index, document)
            for index, document in enumerate(documents)
            if allowed_doc_ids is None or document.doc_id in allowed_doc_ids
        ]
        query_tokens = self.tokenize(query)
        if not query_tokens or not candidates:
            return []
        tokenized = [self.tokenize(item.keyword_text) for _, item in candidates]
        average_length = sum(len(item) for item in tokenized) / max(1, len(tokenized))
        document_frequency = Counter(
            token for tokens in tokenized for token in set(tokens)
        )
        scores: list[tuple[int, float]] = []
        k1, b = 1.5, 0.75
        for candidate_index, tokens in enumerate(tokenized):
            document_index = candidates[candidate_index][0]
            frequencies = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                count = document_frequency[token]
                idf = math.log(1 + (len(candidates) - count + 0.5) / (count + 0.5))
                denominator = frequency + k1 * (
                    1 - b + b * len(tokens) / max(1.0, average_length)
                )
                score += idf * frequency * (k1 + 1) / denominator
            scores.append((document_index, score))
        maximum = max((score for _, score in scores), default=0.0) or 1.0
        return [
            (index, score / maximum)
            for index, score in sorted(scores, key=lambda item: item[1], reverse=True)[:top_k]
            if score > 0
        ]

    def dense_search(
        self,
        query_vector: list[float],
        top_k: int,
        allowed_doc_ids: set[str] | None = None,
    ) -> list[tuple[int, float]]:
        documents = self.active_documents()
        scores = [
            (
                index,
                sum(left * right for left, right in zip(query_vector, document.dense_vector)),
            )
            for index, document in enumerate(documents)
            if allowed_doc_ids is None or document.doc_id in allowed_doc_ids
        ]
        return sorted(scores, key=lambda item: item[1], reverse=True)[:top_k]

    def status(self) -> dict[str, Any]:
        return {
            "type": "local_hybrid_store",
            "milvus_compatible": True,
            "path": str(self.path),
            "document_count": len(self.active_documents()),
            "schema_version": self.signature[:12] if self.signature else None,
            "embedding_source": self.embedding_source or None,
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "signature": self.signature,
            "embedding_source": self.embedding_source,
            "documents": [asdict(item) for item in self.documents],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def tokenize(text: str) -> list[str]:
        words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
        tokens: list[str] = []
        for word in words:
            tokens.append(word)
            if re.fullmatch(r"[\u4e00-\u9fff]+", word):
                tokens.extend(word[index : index + 2] for index in range(len(word) - 1))
        return tokens
