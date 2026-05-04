"""
StreamingToolCallBuffer 单元测试

测试流式工具调用缓冲器的各种功能
"""

import pytest
from openai_proxy.utils.streaming_tool_call_buffer import StreamingToolCallBuffer
from openai_proxy.utils.tool_call_converter import ToolCallConverter


class TestEarlyDetection:
    """测试早期启发式检测"""

    def test_detect_json_feature(self):
        """测试检测到 JSON 特征"""
        buffer = StreamingToolCallBuffer()
        assert buffer._looks_like_non_standard('{"name": "test"}') is True

    def test_detect_xml_invoke(self):
        """测试检测到 XML invoke 标签"""
        buffer = StreamingToolCallBuffer()
        assert buffer._looks_like_non_standard('<invoke name="test">') is True

    def test_detect_xml_minimax(self):
        """测试检测到 Minimax XML"""
        buffer = StreamingToolCallBuffer()
        assert buffer._looks_like_non_standard('<minimax:tool_call>') is True

    def test_plain_text_not_detected(self):
        """测试普通文本不被检测为非标准"""
        buffer = StreamingToolCallBuffer()
        assert buffer._looks_like_non_standard('今天天气真好') is False

    def test_empty_string_not_detected(self):
        """测试空字符串不被检测"""
        buffer = StreamingToolCallBuffer()
        assert buffer._looks_like_non_standard('') is False


class TestProcessChunk:
    """测试 chunk 处理"""

    @pytest.fixture
    def buffer(self):
        return StreamingToolCallBuffer()

    def test_standard_format_forwarded_immediately(self, buffer):
        """测试标准格式立即转发"""
        chunk = {
            "choices": [{
                "delta": {
                    "content": "普通文本",
                    "role": "assistant"
                }
            }]
        }
        
        events = buffer.process_chunk(chunk, ToolCallConverter)
        
        # 应该立即返回一个事件
        assert len(events) == 1
        assert "data:" in events[0]
        assert "普通文本" in events[0]

    def test_existing_tool_calls_forwarded(self, buffer):
        """测试已有的 tool_calls 直接转发"""
        chunk = {
            "choices": [{
                "delta": {
                    "tool_calls": [{"id": "call_123", "type": "function"}],
                    "role": "assistant"
                }
            }]
        }
        
        events = buffer.process_chunk(chunk, ToolCallConverter)
        
        assert len(events) == 1

    def test_nvidia_json_conversion(self, buffer):
        """测试 NVIDIA JSON 格式转换"""
        # 第一个 chunk 触发检测
        chunk1 = {
            "choices": [{
                "delta": {
                    "content": '{"',
                    "role": "assistant"
                }
            }]
        }
        events1 = buffer.process_chunk(chunk1, ToolCallConverter)
        assert len(events1) == 0  # 开始缓冲
        
        # 第二个 chunk 继续累积
        chunk2 = {
            "choices": [{
                "delta": {
                    "content": 'name": "get_weather", "parameters": {"location": "Beijing"}}',
                    "role": "assistant"
                }
            }]
        }
        events2 = buffer.process_chunk(chunk2, ToolCallConverter)
        
        # 应该完成转换并返回事件
        assert len(events2) > 0
        assert "tool_calls" in events2[0]

    def test_timeout_protection(self):
        """测试超时保护"""
        buffer = StreamingToolCallBuffer(timeout_seconds=0.1)  # 设置很短的超时
        
        # 第一个 chunk 触发检测
        chunk1 = {
            "choices": [{
                "delta": {
                    "content": '{"incomplete"',
                    "role": "assistant"
                }
            }]
        }
        buffer.process_chunk(chunk1, ToolCallConverter)
        
        # 等待超时
        import time
        time.sleep(0.2)
        
        # 下一个 chunk 应该触发降级
        chunk2 = {
            "choices": [{
                "delta": {
                    "content": "more content",
                    "role": "assistant"
                }
            }]
        }
        events = buffer.process_chunk(chunk2, ToolCallConverter)
        
        # 应该降级并发送缓冲内容
        assert len(events) > 0
        assert "incomplete" in events[0]

    def test_memory_limit_protection(self):
        """测试内存限制保护"""
        buffer = StreamingToolCallBuffer(max_buffer_size=50)  # 很小的限制
        
        # 发送大内容触发内存限制
        chunk = {
            "choices": [{
                "delta": {
                    "content": '{"' + 'x' * 100,  # 超过 50 字节
                    "role": "assistant"
                }
            }]
        }
        
        # 第一个 chunk 触发检测
        buffer.process_chunk(chunk, ToolCallConverter)
        
        # 第二个 chunk 应该触发内存限制
        chunk2 = {
            "choices": [{
                "delta": {
                    "content": "more",
                    "role": "assistant"
                }
            }]
        }
        events = buffer.process_chunk(chunk2, ToolCallConverter)
        
        # 应该降级
        assert len(events) > 0

    def test_reset_buffer(self):
        """测试重置缓冲器"""
        buffer = StreamingToolCallBuffer()
        
        # 触发检测
        chunk = {
            "choices": [{
                "delta": {
                    "content": '{"test"',
                    "role": "assistant"
                }
            }]
        }
        buffer.process_chunk(chunk, ToolCallConverter)
        
        assert buffer.detected_non_standard is True
        
        # 重置
        buffer.reset()
        
        assert buffer.detected_non_standard is False
        assert buffer.content_buffer == ""
        assert buffer.buffer_start_time is None


class TestEventGeneration:
    """测试事件生成"""

    def test_generate_tool_call_events(self):
        """测试生成 tool_call 事件"""
        buffer = StreamingToolCallBuffer()
        
        tool_calls = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"location": "Beijing"}'
                }
            }
        ]
        
        events = buffer._generate_tool_call_events(tool_calls)
        
        assert len(events) == 1
        assert "call_abc123" in events[0]
        assert "get_weather" in events[0]

    def test_format_text_chunk(self):
        """测试格式化文本 chunk"""
        buffer = StreamingToolCallBuffer()
        
        event = buffer._format_text_chunk("测试文本")
        
        assert "data:" in event
        assert "测试文本" in event


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
