from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app.config import Settings
from app.model_client import ModelClient
from app.retrieval import SchemaIndex


class ModelClientTest(unittest.TestCase):
    def config(self, **changes):
        return replace(
            Settings(),
            **{
                "api_key": "test-chat-secret",
                "embedding_api_key": "test-embedding-secret",
                "rerank_api_key": "test-rerank-secret",
                "llm_base_url": "https://api.deepseek.com/v1",
                "llm_thinking_format": "auto",
                "llm_enable_thinking": False,
                "embedding_dimensions": 2,
                "max_retries": 0,
                **changes,
            },
        )

    def test_provider_specific_thinking_parameters(self):
        for base, expected in [
            ("https://api.deepseek.com/v1", "deepseek"),
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "enable_thinking"),
            ("http://127.0.0.1:11434/v1", "none"),
        ]:
            with self.subTest(base=base):
                client = ModelClient(self.config(llm_base_url=base))
                response = {"choices": [{"message": {"content": "ok"}}]}
                with patch.object(client, "_post", return_value=response) as post:
                    self.assertEqual(client.chat("system", "user"), "ok")
                payload = post.call_args.args[1]
                if expected == "deepseek":
                    self.assertEqual(payload["thinking"], {"type": "disabled"})
                    self.assertNotIn("enable_thinking", payload)
                elif expected == "enable_thinking":
                    self.assertIs(payload["enable_thinking"], False)
                    self.assertNotIn("thinking", payload)
                else:
                    self.assertNotIn("thinking", payload)
                    self.assertNotIn("enable_thinking", payload)

    def test_each_endpoint_uses_its_own_credential(self):
        client = ModelClient(self.config())
        captured = []
        responses = [
            {"choices": [{"message": {"content": "ok"}}]},
            {"data": [{"index": 0, "embedding": [3, 4]}]},
            {"results": [{"index": 0, "relevance_score": 0.9}]},
        ]

        def respond(request, **kwargs):
            captured.append(request.get_header("Authorization"))
            return io.BytesIO(json.dumps(responses.pop(0)).encode())

        with patch("app.model_client.urllib.request.urlopen", side_effect=respond):
            client.chat("system", "user")
            self.assertEqual(client.embed(["text"]), [[0.6, 0.8]])
            self.assertEqual(client.rerank("query", ["text"], 1), [(0, 0.9)])
        self.assertEqual(captured, [
            "Bearer test-chat-secret",
            "Bearer test-embedding-secret",
            "Bearer test-rerank-secret",
        ])

    def test_legacy_shared_key_and_public_status(self):
        config = self.config(embedding_api_key="", rerank_api_key="")
        self.assertEqual(config.embedding_key, config.api_key)
        self.assertEqual(config.rerank_key, config.api_key)
        self.assertNotIn(config.api_key, json.dumps(config.public_status()))

    def test_embedding_dimension_mismatch_is_rejected(self):
        client = ModelClient(self.config())
        with patch.object(client, "_post", return_value={
            "data": [{"index": 0, "embedding": [1, 2, 3]}]
        }):
            with self.assertRaisesRegex(RuntimeError, "维度"):
                client.embed(["text"])

    def test_invalid_json_is_regenerated_once_with_original_request(self):
        client = ModelClient(self.config())
        with patch.object(client, "chat", side_effect=['{"sql": "broken}', '{"sql": "SELECT 1"}']) as chat:
            self.assertEqual(client.chat_json("system", "original query"), {"sql": "SELECT 1"})
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(client.json_retry_count, 1)
        self.assertIn("original query", chat.call_args.args[1])
        self.assertIn('{"sql": "broken}', chat.call_args.args[1])

    def test_json_regeneration_has_a_fixed_limit(self):
        client = ModelClient(self.config())
        with patch.object(client, "chat", return_value="invalid") as chat:
            with self.assertRaisesRegex(RuntimeError, "JSON"):
                client.chat_json("system", "query")
        self.assertEqual(chat.call_count, 2)
        with self.assertRaisesRegex(RuntimeError, "对象"):
            ModelClient._parse_json("[]")

    def test_empty_or_truncated_chat_output_is_rejected(self):
        client = ModelClient(self.config())
        for content, finish in [(None, "stop"), ("", "stop"), ('{"ok":', "length")]:
            with self.subTest(content=content, finish=finish):
                with patch.object(client, "_post", return_value={
                    "choices": [{"message": {"content": content}, "finish_reason": finish}]
                }):
                    with self.assertRaises(RuntimeError):
                        client.chat("system", "user")

    def test_http_errors_redact_credentials(self):
        client = ModelClient(self.config())
        error = HTTPError(
            client.config.chat_url, 401, "Unauthorized", {},
            io.BytesIO(b"invalid test-chat-secret test-embedding-secret test-rerank-secret"),
        )
        with patch("app.model_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                client.chat("system", "user")
        self.assertNotIn("test-chat-secret", str(caught.exception))
        self.assertNotIn("test-embedding-secret", str(caught.exception))
        self.assertNotIn("test-rerank-secret", str(caught.exception))

    def test_index_signature_tracks_embedding_configuration(self):
        original = SchemaIndex(config=self.config())._schema_signature()
        for changes in [
            {"embedding_dimensions": 3},
            {"embedding_model": "another-model"},
            {"embedding_base_url": "http://127.0.0.1:8001/v1"},
        ]:
            self.assertNotEqual(
                original, SchemaIndex(config=self.config(**changes))._schema_signature()
            )


if __name__ == "__main__":
    unittest.main()
