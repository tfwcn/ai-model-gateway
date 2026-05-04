"""
ToolCallConverter 单元测试

测试工具调用格式转换器的各种功能，包括：
- is_complete_format() 完整性检测
- convert_to_standard_format() 格式转换
- is_non_standard_format() 非标准格式检测
"""

import pytest
from openai_proxy.utils.tool_call_converter import ToolCallConverter


class TestIsCompleteFormat:
    """测试 is_complete_format() 方法"""

    def test_complete_json_object(self):
        """测试完整的 JSON 对象"""
        content = '{"name": "get_weather", "parameters": {"location": "here"}}'
        assert ToolCallConverter.is_complete_format(content) is True

    def test_complete_json_array(self):
        """测试完整的 JSON 数组"""
        content = '[{"name": "test1"}, {"name": "test2"}]'
        assert ToolCallConverter.is_complete_format(content) is True

    def test_incomplete_json_missing_closing_brace(self):
        """测试不完整的 JSON - 缺少闭合括号"""
        content = '{"name": "get_weather", "parameters": {"location": "here"'
        assert ToolCallConverter.is_complete_format(content) is False

    def test_incomplete_json_truncated(self):
        """测试不完整的 JSON - 截断"""
        content = '{"name": "test", "param'
        assert ToolCallConverter.is_complete_format(content) is False

    def test_incomplete_json_only_opening_brace(self):
        """测试不完整的 JSON - 只有开始括号"""
        content = '{"'
        assert ToolCallConverter.is_complete_format(content) is False

    def test_complete_xml_invoke(self):
        """测试完整的 XML invoke 标签"""
        content = '<invoke name="test_tool">{"arg": "value"}</invoke>'
        assert ToolCallConverter.is_complete_format(content) is True

    def test_complete_xml_minimax(self):
        """测试完整的 Minimax XML 标签"""
        content = '<minimax:tool_call><invoke name="test">{}</invoke></minimax:tool_call>'
        assert ToolCallConverter.is_complete_format(content) is True

    def test_incomplete_xml_missing_closing_tag(self):
        """测试不完整的 XML - 缺少闭合标签"""
        content = '<invoke name="test_tool">{"arg": "value"}'
        assert ToolCallConverter.is_complete_format(content) is False

    def test_incomplete_xml_unclosed_tag(self):
        """测试不完整的 XML - 未闭合的标签"""
        content = '<invoke name="test">'
        assert ToolCallConverter.is_complete_format(content) is False

    def test_empty_string(self):
        """测试空字符串"""
        assert ToolCallConverter.is_complete_format("") is False

    def test_none_input(self):
        """测试 None 输入"""
        assert ToolCallConverter.is_complete_format(None) is False

    def test_whitespace_only(self):
        """测试纯空白字符"""
        assert ToolCallConverter.is_complete_format("   \n\t  ") is False

    def test_plain_text(self):
        """测试普通文本"""
        content = "今天天气真好"
        assert ToolCallConverter.is_complete_format(content) is False

    def test_text_starting_with_brace(self):
        """测试以 { 开头的普通文本（不是有效 JSON）"""
        content = '{"今天天气真好"}'
        # 这不是有效的 JSON，应该返回 False
        assert ToolCallConverter.is_complete_format(content) is False

    def test_json_with_whitespace(self):
        """测试包含空白字符的完整 JSON"""
        content = '  {\n  "name": "test"\n}  '
        assert ToolCallConverter.is_complete_format(content) is True

    def test_nested_json(self):
        """测试嵌套的完整 JSON"""
        content = '{"outer": {"inner": {"deep": "value"}}}'
        assert ToolCallConverter.is_complete_format(content) is True


class TestIsNonStandardFormat:
    """测试 is_non_standard_format() 方法"""

    def test_nvidia_json_format(self):
        """测试 NVIDIA JSON 格式"""
        content = '{"name": "get_weather", "parameters": {"location": "here"}}'
        assert ToolCallConverter.is_non_standard_format(content) is True

    def test_minimax_xml_format(self):
        """测试 Minimax XML 格式"""
        content = '<minimax:tool_call><invoke name="test">{}</invoke></minimax:tool_call>'
        assert ToolCallConverter.is_non_standard_format(content) is True

    def test_invoke_xml_format(self):
        """测试 invoke XML 格式"""
        content = '<invoke name="test_tool">{"arg": "value"}</invoke>'
        assert ToolCallConverter.is_non_standard_format(content) is True

    def test_plain_text(self):
        """测试普通文本"""
        content = "这是一个普通的回复"
        assert ToolCallConverter.is_non_standard_format(content) is False

    def test_empty_content(self):
        """测试空内容"""
        assert ToolCallConverter.is_non_standard_format("") is False

    def test_none_content(self):
        """测试 None"""
        assert ToolCallConverter.is_non_standard_format(None) is False


class TestConvertToStandardFormat:
    """测试 convert_to_standard_format() 方法"""

    def test_convert_nvidia_json(self):
        """测试转换 NVIDIA JSON 格式"""
        content = '{"name": "get_weather", "parameters": {"location": "Beijing"}}'
        tool_calls, remaining = ToolCallConverter.convert_to_standard_format(content)
        
        assert len(tool_calls) == 1
        assert tool_calls[0]["type"] == "function"
        assert tool_calls[0]["function"]["name"] == "get_weather"
        assert remaining == ""

    def test_convert_minimax_xml(self):
        """测试转换 Minimax XML 格式"""
        content = '<invoke name="search">{"query": "test"}</invoke>'
        tool_calls, remaining = ToolCallConverter.convert_to_standard_format(content)
        
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "search"
        assert remaining == ""

    def test_existing_tool_calls_unchanged(self):
        """测试已有的 tool_calls 不被修改"""
        existing = [{"id": "call_123", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
        content = "some content"
        
        tool_calls, remaining = ToolCallConverter.convert_to_standard_format(
            content, existing_tool_calls=existing
        )
        
        assert tool_calls == existing
        assert remaining == content

    def test_no_conversion_for_plain_text(self):
        """测试普通文本不转换"""
        content = "这是一个普通的回复"
        tool_calls, remaining = ToolCallConverter.convert_to_standard_format(content)
        
        assert len(tool_calls) == 0
        assert remaining == content

    def test_empty_content(self):
        """测试空内容"""
        tool_calls, remaining = ToolCallConverter.convert_to_standard_format("")
        
        assert len(tool_calls) == 0
        assert remaining == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
