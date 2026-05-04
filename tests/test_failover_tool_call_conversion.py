"""
FailoverManager 工具调用转换集成测试

测试非流式和流式响应中的工具调用格式转换功能
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from openai_proxy.model.failover import ModelFailoverManager
from openai_proxy.models import ModelConfig


class TestNonStreamToolCallConversion:
    """测试非流式响应的工具调用转换"""

    @pytest.fixture
    def failover_manager(self):
        """创建 FailoverManager 实例"""
        models = {
            "test_platform": [
                ModelConfig(
                    name="test_model",
                    model="test-model-v1",
                    base_url="https://api.test.com",
                    api_key="test-key",
                    timeout=30,
                    enabled=True
                )
            ]
        }
        return ModelFailoverManager(models)

    @pytest.mark.asyncio
    async def test_convert_nvidia_json_format(self, failover_manager):
        """测试 NVIDIA JSON 格式的转换"""
        # Mock HTTP 响应
        mock_response = MagicMock()
        mock_response.status = 200
        
        async def mock_json():
            return {
                "choices": [{
                    "message": {
                        "content": '{"name": "get_weather", "parameters": {"location": "Beijing"}}',
                        "role": "assistant"
                    }
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
            }
        
        mock_response.json = mock_json
        
        # 创建异步上下文管理器
        async_mock_response = AsyncMock()
        async_mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        async_mock_response.__aexit__ = AsyncMock(return_value=None)

        # Mock session.post
        with patch.object(failover_manager, 'get_session') as mock_get_session:
            mock_session = AsyncMock()
            mock_session.post = MagicMock(return_value=async_mock_response)
            mock_get_session.return_value = mock_session

            # 调用方法
            request_data = {
                "model": "test-model",
                "messages": [{"role": "user", "content": "天气如何？"}]
            }
            
            success, result = await failover_manager.call_model_non_stream(
                failover_manager.models["test_platform"][0],
                request_data
            )

            # 验证
            assert success is True
            assert "choices" in result
            message = result["choices"][0]["message"]
            
            # 验证 tool_calls 被正确转换
            assert "tool_calls" in message
            assert len(message["tool_calls"]) == 1
            assert message["tool_calls"][0]["function"]["name"] == "get_weather"
            
            # 验证 content 被清空
            assert message["content"] == ""

    @pytest.mark.asyncio
    async def test_convert_minimax_xml_format(self, failover_manager):
        """测试 Minimax XML 格式的转换"""
        # Mock HTTP 响应
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "choices": [{
                "message": {
                    "content": '<invoke name="search">{"query": "test"}</invoke>',
                    "role": "assistant"
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # Mock session.post
        with patch.object(failover_manager, 'get_session') as mock_get_session:
            mock_session = AsyncMock()
            mock_session.post = AsyncMock(return_value=mock_response)
            mock_get_session.return_value = mock_session

            # 调用方法
            request_data = {
                "model": "test-model",
                "messages": [{"role": "user", "content": "搜索一下"}]
            }
            
            success, result = await failover_manager.call_model_non_stream(
                failover_manager.models["test_platform"][0],
                request_data
            )

            # 验证
            assert success is True
            message = result["choices"][0]["message"]
            
            # 验证 tool_calls 被正确转换
            assert "tool_calls" in message
            assert len(message["tool_calls"]) == 1
            assert message["tool_calls"][0]["function"]["name"] == "search"

    @pytest.mark.asyncio
    async def test_standard_format_unchanged(self, failover_manager):
        """测试标准格式不被修改"""
        existing_tool_calls = [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "arguments": "{}"
                }
            }
        ]
        
        # Mock HTTP 响应（已有标准 tool_calls）
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "choices": [{
                "message": {
                    "content": "some content",
                    "role": "assistant",
                    "tool_calls": existing_tool_calls
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # Mock session.post
        with patch.object(failover_manager, 'get_session') as mock_get_session:
            mock_session = AsyncMock()
            mock_session.post = AsyncMock(return_value=mock_response)
            mock_get_session.return_value = mock_session

            # 调用方法
            request_data = {
                "model": "test-model",
                "messages": [{"role": "user", "content": "测试"}]
            }
            
            success, result = await failover_manager.call_model_non_stream(
                failover_manager.models["test_platform"][0],
                request_data
            )

            # 验证
            assert success is True
            message = result["choices"][0]["message"]
            
            # 验证 tool_calls 保持不变
            assert message["tool_calls"] == existing_tool_calls
            # 验证 content 保持不变
            assert message["content"] == "some content"

    @pytest.mark.asyncio
    async def test_plain_text_unchanged(self, failover_manager):
        """测试普通文本不被转换"""
        # Mock HTTP 响应（普通文本）
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "choices": [{
                "message": {
                    "content": "这是一个普通的回复",
                    "role": "assistant"
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # Mock session.post
        with patch.object(failover_manager, 'get_session') as mock_get_session:
            mock_session = AsyncMock()
            mock_session.post = AsyncMock(return_value=mock_response)
            mock_get_session.return_value = mock_session

            # 调用方法
            request_data = {
                "model": "test-model",
                "messages": [{"role": "user", "content": "你好"}]
            }
            
            success, result = await failover_manager.call_model_non_stream(
                failover_manager.models["test_platform"][0],
                request_data
            )

            # 验证
            assert success is True
            message = result["choices"][0]["message"]
            
            # 验证没有 tool_calls
            assert "tool_calls" not in message or len(message.get("tool_calls", [])) == 0
            # 验证 content 保持不变
            assert message["content"] == "这是一个普通的回复"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
