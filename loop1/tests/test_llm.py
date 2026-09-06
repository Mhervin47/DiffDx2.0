"""Regression test for the missing-'choices'-key bug found live: some
free-tier/OpenRouter models occasionally return HTTP 200 with a malformed
or content-filtered body that has no "choices" at all. Before the fix,
`data["choices"][0]["message"]` raised a raw KeyError that leaked all the
way up to the caller instead of being retried like the adjacent
empty-content case."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from loop1.llm import _RetryableHTTPError, _call_llm_raw


def _fake_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = json.dumps(body)
    resp.headers = {}
    return resp


def test_missing_choices_key_raises_retryable_not_keyerror():
    response = _fake_response(200, {"error": {"message": "content filtered"}})
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.post.return_value = response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with pytest.raises(_RetryableHTTPError, match="No 'choices' in response"):
            _call_llm_raw("groq/some-model", [{"role": "user", "content": "hi"}])


def test_empty_choices_list_raises_retryable_not_indexerror():
    response = _fake_response(200, {"choices": []})
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.post.return_value = response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with pytest.raises(_RetryableHTTPError, match="No 'choices' in response"):
            _call_llm_raw("groq/some-model", [{"role": "user", "content": "hi"}])


def test_normal_response_still_returns_content():
    response = _fake_response(200, {
        "choices": [{"message": {"content": "hello there"}}],
        "usage": {"prompt_tokens": 10},
    })
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.post.return_value = response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        content, prompt_tokens = _call_llm_raw("groq/some-model", [{"role": "user", "content": "hi"}])
        assert content == "hello there"
        assert prompt_tokens == 10
