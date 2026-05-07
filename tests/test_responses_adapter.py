"""
Tests for Responses API adapter, focusing on custom tools conversion.
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from openai_proxy.adapter.responses import ResponsesAdapter


@pytest.fixture
def adapter():
    """Create a ResponsesAdapter instance with mocked session store."""
    mock_store = AsyncMock()
    mock_store.get_history = AsyncMock(return_value=[])
    return ResponsesAdapter(session_store=mock_store)


class TestCustomToolConversion:
    """Test custom tool bidirectional conversion."""

    def test_convert_custom_to_function(self, adapter):
        """Test converting a custom tool to function format."""
        custom_tool = {
            "type": "custom",
            "name": "apply_patch",
            "description": "Apply a patch to the codebase",
            "format": {"type": "grammar", "syntax": "diff"}
        }
        converted_tools = {}

        result = adapter._convert_custom_to_function(custom_tool, converted_tools)

        assert result["type"] == "function"
        assert result["function"]["name"] == "apply_patch"
        # description 应该保持原样，不添加额外提示
        assert result["function"]["description"] == "Apply a patch to the codebase"
        # strict 应该是 False
        assert result["function"]["strict"] == False
        # parameters 应该有 title
        assert result["function"]["parameters"]["title"] == "ApplyPatchArgs"
        assert result["function"]["parameters"]["properties"]["input"]["type"] == "string"
        # input 属性应该包含 title
        assert result["function"]["parameters"]["properties"]["input"]["title"] == "Input"
        # input.description 应该是空字符串（因为没有 definition 字段）
        assert result["function"]["parameters"]["properties"]["input"]["description"] == ""
        assert "apply_patch" in converted_tools

    def test_convert_custom_without_format(self, adapter):
        """Test converting a custom tool without format field."""
        custom_tool = {
            "type": "custom",
            "name": "simple_tool",
            "description": "A simple custom tool"
        }
        converted_tools = {}

        result = adapter._convert_custom_to_function(custom_tool, converted_tools)

        assert result["type"] == "function"
        assert result["function"]["name"] == "simple_tool"
        assert "纯文本输入" not in result["function"]["description"]

    def test_convert_tools_mixed_types(self, adapter):
        """Test converting a mix of function and custom tools."""
        tools = [
            {
                "type": "function",
                "name": "search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}
            },
            {
                "type": "custom",
                "name": "exec_command",
                "description": "Execute a shell command",
                "format": {"type": "plain_text"}
            }
        ]
        converted_tools = {}

        result = adapter._convert_tools(tools, converted_tools)

        assert len(result) == 2
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search"
        assert result[1]["type"] == "function"
        assert result[1]["function"]["name"] == "exec_command"
        assert "exec_command" in converted_tools


class TestInputConversion:
    """Test input to messages conversion."""

    @pytest.mark.asyncio
    async def test_convert_multimodal_input(self, adapter):
        """Test converting multimodal input with images."""
        input_items = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What is in this image?"},
                    {"type": "input_image", "image_url": "https://example.com/image.jpg"}
                ]
            }
        ]

        messages = adapter._convert_input_to_messages(input_items)

        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert isinstance(messages[0]["content"], list)
        assert messages[0]["content"][0]["type"] == "text"
        assert messages[0]["content"][1]["type"] == "image_url"

    def test_convert_flat_custom_tool_call(self, adapter):
        """Test converting flat custom_tool_call format."""
        input_items = [
            {
                "type": "message",
                "role": "assistant",
                "call_id": "call_123",
                "name": "apply_patch",
                "input": "some diff content"
            }
        ]

        messages = adapter._convert_input_to_messages(input_items)

        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert "tool_calls" in messages[0]
        assert messages[0]["tool_calls"][0]["id"] == "call_123"
        assert messages[0]["tool_calls"][0]["function"]["name"] == "apply_patch"


class TestResponseBuilding:
    """Test response object building."""

    def test_build_response_with_custom_tool(self, adapter):
        """Test building response with custom tool call."""
        chat_response = {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "apply_patch",
                            "arguments": json.dumps({"input": "patch content"})
                        }
                    }]
                }
            }],
            "created": 1234567890,
            "model": "gpt-4",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}
        }

        # Register the custom tool conversion
        request_id = "req_test"
        adapter._request_custom_tools[request_id] = {
            "apply_patch": {"type": "custom", "name": "apply_patch"}
        }

        response_obj, new_id = adapter.build_response_object(chat_response, {}, request_id)

        assert response_obj["id"].startswith("resp_")
        assert len(response_obj["output"]) == 1
        assert response_obj["output"][0]["type"] == "custom_tool_call"
        assert response_obj["output"][0]["call_id"] == "call_abc"
        assert response_obj["output"][0]["name"] == "apply_patch"
        assert response_obj["output"][0]["input"] == "patch content"

    def test_build_response_with_text_format_conversion(self, adapter):
        """Test that text.format is properly handled."""
        # This is tested in convert_request, but we verify the round-trip here
        pass


class TestGPT5ParameterAdaptation:
    """Test GPT-5 specific parameter handling."""

    @pytest.mark.asyncio
    async def test_gpt5_temperature_override(self, adapter):
        """Test that GPT-5 temperature is forced to 1."""
        payload = {
            "model": "gpt-5",
            "input": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7
        }

        chat_payload, _ = await adapter.convert_request(payload)

        assert chat_payload["temperature"] == 1

    @pytest.mark.asyncio
    async def test_non_gpt5_temperature_preserved(self, adapter):
        """Test that non-GPT-5 models preserve their temperature."""
        payload = {
            "model": "gpt-4",
            "input": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7
        }

        chat_payload, _ = await adapter.convert_request(payload)

        assert chat_payload["temperature"] == 0.7


class TestStructuredOutputConversion:
    """Test structured output format conversion."""

    @pytest.mark.asyncio
    async def test_text_format_to_response_format(self, adapter):
        """Test converting text.format to response_format."""
        payload = {
            "model": "gpt-4",
            "input": [{"role": "user", "content": "Give me JSON"}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "MyOutput",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"]
                        }
                    }
                }
            }
        }

        chat_payload, _ = await adapter.convert_request(payload)

        assert "response_format" in chat_payload
        assert chat_payload["response_format"]["type"] == "json_schema"
        assert chat_payload["response_format"]["json_schema"]["name"] == "MyOutput"
        assert chat_payload["response_format"]["json_schema"]["strict"] is True
