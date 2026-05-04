"""
工具调用格式转换器

负责将各种非标准格式的工具调用响应转换为标准的 OpenAI tool_calls 格式。
支持以下格式：
1. NVIDIA JSON 格式: {"name": "...", "parameters": {...}}
2. Minimax XML 格式: <minimax:tool_call><invoke name="...">...</invoke>
3. 其他自定义格式

流式支持：
- is_complete_format(): 检测累积内容是否是完整的非标准格式，用于流式响应的智能缓冲
- 配合 StreamingToolCallBuffer 实现早期检测和最小缓冲策略
"""

import json
import logging
import re
import uuid
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class ToolCallConverter:
    """工具调用格式转换器"""

    @staticmethod
    def convert_to_standard_format(
        content: str,
        existing_tool_calls: Optional[List[Dict]] = None
    ) -> tuple[List[Dict], str]:
        """
        将非标准格式的工具调用转换为标准 OpenAI 格式

        Args:
            content: 响应内容字符串
            existing_tool_calls: 已有的 tool_calls 数组（如果存在则直接返回）

        Returns:
            (tool_calls, remaining_content): 标准化的 tool_calls 和剩余内容
        """
        # 如果已有标准格式的 tool_calls，直接返回
        if existing_tool_calls and len(existing_tool_calls) > 0:
            return existing_tool_calls, content

        # 如果没有内容，返回空列表
        if not content or not isinstance(content, str):
            return [], content

        # 尝试各种格式转换
        tool_calls = []

        # 1. 尝试 JSON 格式（NVIDIA 等）
        tool_calls = ToolCallConverter._try_json_format(content)
        if tool_calls:
            logger.debug("成功转换 JSON 格式工具调用")
            return tool_calls, ""  # 清空 content，因为已提取到 tool_calls

        # 2. 尝试 XML 格式（Minimax 等）
        tool_calls = ToolCallConverter._try_xml_format(content)
        if tool_calls:
            logger.debug("成功转换 XML 格式工具调用")
            return tool_calls, ""  # 清空 content

        # 3. 都不匹配，返回原样
        return [], content

    @staticmethod
    def _try_json_format(content: str) -> Optional[List[Dict]]:
        """
        尝试解析 JSON 格式的工具调用

        支持的格式：
        - {"name": "...", "parameters": {...}}
        - {"type": "function", "name": "...", "parameters": {...}}
        """
        try:
            parsed = json.loads(content)

            # 检查是否是单个工具调用的 JSON 对象
            if isinstance(parsed, dict):
                # 情况1: 包含 name 和 parameters 字段
                if "name" in parsed and "parameters" in parsed:
                    return [ToolCallConverter._create_standard_tool_call(
                        name=parsed["name"],
                        arguments=json.dumps(parsed.get("parameters", {}))
                    )]

                # 情况2: 包含 type, name, parameters 字段
                if parsed.get("type") == "function" and "name" in parsed and "parameters" in parsed:
                    return [ToolCallConverter._create_standard_tool_call(
                        name=parsed["name"],
                        arguments=json.dumps(parsed.get("parameters", {}))
                    )]

            # 检查是否是工具调用数组
            if isinstance(parsed, list):
                tool_calls = []
                for item in parsed:
                    if isinstance(item, dict) and "name" in item:
                        tool_calls.append(ToolCallConverter._create_standard_tool_call(
                            name=item["name"],
                            arguments=json.dumps(item.get("parameters", {}))
                        ))
                if tool_calls:
                    return tool_calls

        except json.JSONDecodeError:
            pass

        return None

    @staticmethod
    def _try_xml_format(content: str) -> Optional[List[Dict]]:
        """
        尝试解析 XML 格式的工具调用

        支持的格式：
        - <minimax:tool_call><invoke name="...">...</invoke></minimax:tool_call>
        - <invoke name="...">...</invoke>
        """
        # 查找所有 invoke 标签
        invoke_pattern = r'<invoke\s+name="([^"]+)"[^>]*>(.*?)</invoke>'
        matches = re.findall(invoke_pattern, content, re.DOTALL)

        if not matches:
            return None

        tool_calls = []
        for name, args_content in matches:
            # 尝试解析参数（可能是 JSON 或其他格式）
            arguments = ToolCallConverter._extract_arguments(args_content)
            tool_calls.append(ToolCallConverter._create_standard_tool_call(
                name=name,
                arguments=arguments
            ))

        return tool_calls if tool_calls else None

    @staticmethod
    def _extract_arguments(args_content: str) -> str:
        """
        从 XML 内容中提取参数字符串

        Args:
            args_content: XML 标签内的内容

        Returns:
            JSON 格式的参数串
        """
        # 如果内容是空的或只有空白，返回空对象
        if not args_content or not args_content.strip():
            return "{}"

        # 尝试解析为 JSON
        try:
            parsed = json.loads(args_content.strip())
            return json.dumps(parsed)
        except json.JSONDecodeError:
            pass

        # 如果不是 JSON，尝试提取键值对
        # 简单处理：将整个内容作为字符串参数
        return json.dumps({"raw_content": args_content.strip()})

    @staticmethod
    def _create_standard_tool_call(name: str, arguments: str) -> Dict:
        """
        创建标准 OpenAI 格式的 tool_call 对象

        Args:
            name: 工具名称
            arguments: 参数字符串（JSON 格式）

        Returns:
            标准格式的 tool_call 对象
        """
        return {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments
            }
        }

    @staticmethod
    def is_non_standard_format(content: str) -> bool:
        """
        检测内容是否包含非标准格式的工具调用

        Args:
            content: 响应内容

        Returns:
            True 如果检测到非标准格式
        """
        if not content or not isinstance(content, str):
            return False

        # 检查 JSON 格式
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "name" in parsed and "parameters" in parsed:
                return True
        except json.JSONDecodeError:
            pass

        # 检查 XML 格式
        if "<minimax:tool_call>" in content or "<invoke" in content:
            return True

        return False

    @staticmethod
    def is_complete_format(content: str) -> bool:
        """
        检测内容是否是完整的非标准格式（JSON 或 XML）
        
        用于流式响应中判断累积的内容是否可以安全转换。

        Args:
            content: 累积的响应内容

        Returns:
            True 如果内容是完整的 JSON 或 XML 格式
        """
        if not content or not isinstance(content, str):
            return False

        stripped = content.strip()
        if not stripped:
            return False

        # JSON 完整性检测：尝试解析
        try:
            json.loads(stripped)
            return True
        except json.JSONDecodeError:
            pass

        # XML 完整性检测：检查是否有闭合标签
        # 支持 <invoke>...</invoke> 和 <minimax:tool_call>...</minimax:tool_call>
        if "<invoke" in stripped and "</invoke>" in stripped:
            return True
        if "<minimax:tool_call>" in stripped and "</minimax:tool_call>" in stripped:
            return True

        return False
