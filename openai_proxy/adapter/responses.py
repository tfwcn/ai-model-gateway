import json
import logging
import uuid
from typing import Dict, List, Any, Optional
from ..utils.session import SessionStore
from ..utils.tool_call_converter import ToolCallConverter
from ..utils.streaming_context import StreamingContext

logger = logging.getLogger(__name__)


class ResponsesAdapter:
    """
    OpenAI Responses API 到 Chat Completions API 的协议转换器。
    负责将 /v1/responses 的请求格式转换为 /v1/chat/completions 兼容的格式。
    """

    def __init__(self, session_store: Optional[SessionStore] = None):
        self.session_store = session_store
        # 流式响应上下文（统一管理所有流式状态）
        self.context: Optional[StreamingContext] = None

    async def convert_request(self, responses_payload: dict) -> dict:
        """
        将 Responses API 请求体转换为 Chat API 请求体。
        """
        # 生成请求ID
        request_id = str(uuid.uuid4())

        # 创建流式上下文
        self.context = StreamingContext(request_id=request_id)

        # 保存模型名称（从请求中提取）
        self.context.model_name = responses_payload.get("model", "unknown")

        # 创建请求级别的 custom tools 转换记录（存储在 context 中）
        converted_custom_tools: Dict[str, dict] = {}

        # 1. 提取基础字段
        model = responses_payload.get("model")
        stream = responses_payload.get("stream", False)

        # 2. 提取并处理历史记录 (previous_response_id)
        prev_id = responses_payload.get("previous_response_id")
        history = []
        if prev_id and self.session_store:
            history = await self.session_store.get_history(prev_id)

        # 3. 转换当前 input 为 messages
        input_items = responses_payload.get("input", [])
        current_messages = self._convert_input_to_messages(input_items)

        # 4. 合并历史消息与当前消息
        messages = history + current_messages

        # 5. 处理系统指令 (instructions)
        instructions = responses_payload.get("instructions")
        if instructions:
            # 如果已有历史消息，通常不需要重复插入 system prompt，除非业务要求
            if not history:
                messages.insert(0, {"role": "system", "content": instructions})
            else:
                # 可选：如果需要在每次请求都强化指令，可以再次插入
                pass

        # 6. 构建 Chat API 请求体
        chat_payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }

        # 7. 映射其他可选参数 (字段名标准化)
        if "max_output_tokens" in responses_payload:
            chat_payload["max_tokens"] = responses_payload["max_output_tokens"]

        if "temperature" in responses_payload:
            chat_payload["temperature"] = responses_payload["temperature"]

        if "top_p" in responses_payload:
            chat_payload["top_p"] = responses_payload["top_p"]

        # 处理工具选择
        if "tool_choice" in responses_payload:
            chat_payload["tool_choice"] = responses_payload["tool_choice"]

        # 处理工具定义 - 将 Responses API tools 转换为 Chat API tools
        if "tools" in responses_payload:
            chat_payload["tools"] = self._convert_tools(responses_payload["tools"], converted_custom_tools)
            # 将 custom tools 注册到 context 中
            for tool_name in converted_custom_tools:
                self.context.register_custom_tool(tool_name, converted_custom_tools[tool_name])

        # 处理结构化输出：text.format (Responses) -> response_format (Chat)
        if "text" in responses_payload and isinstance(responses_payload["text"], dict):
            text_config = responses_payload["text"]
            if "format" in text_config:
                fmt = text_config["format"]
                # 检查是否是 json_schema 格式
                if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
                    json_schema = fmt.get("json_schema", {})
                    # 构建 Chat API 的 response_format
                    chat_payload["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": json_schema.get("name", "Output"),
                            "strict": json_schema.get("strict", True),
                            "schema": json_schema.get("schema", {})
                        }
                    }
                    logger.info(f"Converted text.format to response_format with schema name: {json_schema.get('name', 'Output')}")

        return chat_payload, request_id

    def _convert_custom_to_function(self, custom_tool: dict, converted_custom_tools: Dict[str, dict]) -> dict:
        """
        将 custom tool 转换为 function tool 格式。

        Custom Tool (Responses API):
        {
            "type": "custom",
            "name": "apply_patch",
            "description": "...",
            "format": {"type": "grammar", ...}
        }

        Function Tool (Chat API):
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": "...",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "description": "<format.definition 内容>"
                        }
                    },
                    "required": ["input"]
                }
            }
        }
        """
        name = custom_tool.get("name", "")
        description = custom_tool.get("description", "")
        has_format = "format" in custom_tool

        # 记录被转换的 custom tool,用于响应时的反向转换
        converted_custom_tools[name] = custom_tool

        logger.debug(f"🔧 [TOOL CONVERT] Custom Tool '{name}' -> Function Tool")
        logger.debug(f"   Original: {json.dumps(custom_tool, indent=2, ensure_ascii=False)}")

        # 构建 input 字段的 description
        # 如果存在 format.definition，直接使用它作为 input.description
        if has_format and "format" in custom_tool and "definition" in custom_tool["format"]:
            input_description = custom_tool["format"]["definition"]
            logger.debug(f"Using format.definition as input description for: {name}")
        else:
            # 没有 format.definition 时，使用空字符串或简单说明
            input_description = ""

        # 构建 function tool
        chat_tool = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "strict": False,
                "parameters": {
                    "type": "object",
                    "title": f"{name.title().replace('_', '')}Args",
                    "properties": {
                        "input": {
                            "type": "string",
                            "title": "Input",
                            "description": input_description
                        }
                    },
                    "required": ["input"]
                }
            }
        }

        logger.debug(f"   Converted: {json.dumps(chat_tool, indent=2, ensure_ascii=False)}")

        return chat_tool

    def _convert_tools(self, response_tools: list, converted_custom_tools: Dict[str, dict]) -> list:
        """
        将 Responses API 的 tools 格式转换为 Chat API 的 tools 格式。

        Responses API 格式:
        {
            "type": "function",
            "name": "exec_command",
            "description": "...",
            "parameters": {...}
        }

        Chat API 格式:
        {
            "type": "function",
            "function": {
                "name": "exec_command",
                "description": "...",
                "parameters": {...}
            }
        }
        """
        chat_tools = []
        for tool in response_tools:
            if not isinstance(tool, dict):
                continue

            tool_type = tool.get("type", "function")

            if tool_type == "function":
                # 提取 function 相关字段
                function_def = {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                }

                # 处理 parameters
                if "parameters" in tool:
                    function_def["parameters"] = tool["parameters"]

                # 构建 Chat API 格式
                chat_tool = {
                    "type": "function",
                    "function": function_def
                }
                chat_tools.append(chat_tool)
            elif tool_type == "custom":
                # 验证必需字段
                if not tool.get("name") or not tool.get("description"):
                    logger.warning(f"Invalid custom tool: missing name or description, skipping")
                    continue

                # 转换 custom tool 为 function tool
                chat_tool = self._convert_custom_to_function(tool, converted_custom_tools)
                chat_tools.append(chat_tool)
                logger.info(f"✓ Converted custom tool to function tool: {tool.get('name')}")
            else:
                # 其他类型的工具直接传递（如果有）
                logger.warning(f"Unknown tool type: {tool_type}, passing through")
                chat_tools.append(tool)

        return chat_tools

    def _convert_input_to_messages(self, input_items: list) -> list:
        """
        将 Responses API 的 input 数组转换为 Chat API 的 messages 数组。
        支持多种格式：
        1. 完整格式: {"type": "message", "role": "user", "content": "..."}
        2. 简化格式: {"role": "user", "content": "..."}
        3. 带 output 字段的格式: {"role": "assistant", "output": [{"type": "custom_tool_call", ...}]}
        4. 多模态内容: {"role": "user", "content": [{"type": "input_image", ...}]}
        5. 顶层 custom_tool_call: {"type": "custom_tool_call", "call_id": "...", "name": "...", "input": "..."}
        6. 顶层 custom_tool_call_output: {"type": "custom_tool_call_output", "call_id": "...", "output": "..."}
        7. 顶层 function_call: {"type": "function_call", "call_id": "...", "name": "...", "arguments": "..."}
        8. 顶层 function_call_output: {"type": "function_call_output", "call_id": "...", "output": "..."}
        """
        messages = []
        for item in input_items:
            if not isinstance(item, dict):
                continue

            # 兼容简化格式：如果没有type字段但有role字段，直接当作message处理
            item_type = item.get("type")
            role = item.get("role")
            content = item.get("content", "")

            # 处理带 output 字段的格式（Responses API 标准格式）
            if "output" in item and isinstance(item["output"], list):
                # 遍历 output 数组中的每个元素
                for output_item in item["output"]:
                    if not isinstance(output_item, dict):
                        continue

                    output_type = output_item.get("type")

                    # 处理 custom_tool_call
                    if output_type == "custom_tool_call":
                        call_id = output_item.get("call_id")
                        name = output_item.get("name", "")
                        raw_input = output_item.get("input", "")
                        if call_id:
                            messages.append({
                                "role": "assistant",
                                "tool_calls": [{
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": raw_input
                                    }
                                }]
                            })

                    # 处理 custom_tool_call_output
                    elif output_type == "custom_tool_call_output":
                        call_id = output_item.get("call_id")
                        output = output_item.get("output", "")
                        if call_id:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": output
                            })

                    # 处理 function_call
                    elif output_type == "function_call":
                        call_id = output_item.get("call_id")
                        name = output_item.get("name", "")
                        arguments = output_item.get("arguments", "{}")
                        if call_id:
                            messages.append({
                                "role": "assistant",
                                "tool_calls": [{
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": arguments
                                    }
                                }]
                            })

                    # 处理 function_call_output
                    elif output_type == "function_call_output":
                        call_id = output_item.get("call_id")
                        output = output_item.get("output", "")
                        if call_id:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": output
                            })

                # 已经处理完 output 数组，跳过后续的逻辑
                continue

            # 如果是简化格式（有role但没有type）
            if role and not item_type:
                if role in ["user", "assistant", "system"]:
                    # 检查 content 是否为多模态数组
                    if isinstance(content, list):
                        converted_content = self._convert_content_array(content)
                        messages.append({"role": role, "content": converted_content})
                    else:
                        messages.append({"role": role, "content": content})
                continue

            # 处理消息类型
            if item_type == "message":
                # 检查是否是 assistant message 且包含工具调用信息（扁平格式）
                if role == "assistant":
                    # SDK 可能发送扁平格式的 custom_tool_call
                    # 格式: {"type": "message", "role": "assistant", "content": "...",
                    #        "call_id": "...", "name": "...", "input": "..."}
                    call_id = item.get("call_id")
                    name = item.get("name")
                    tool_input = item.get("input")

                    if call_id and name and tool_input is not None:
                        # 这是扁平格式的 custom_tool_call，转换为 Chat API 格式
                        messages.append({
                            "role": "assistant",
                            "tool_calls": [{
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": tool_input if isinstance(tool_input, str) else json.dumps(tool_input)
                                }
                            }]
                        })
                        logger.info(f"Converted flat custom_tool_call to tool_calls: {name}")
                        continue

                    # 检查是否是 custom_tool_call_output（扁平格式）
                    output_value = item.get("output")
                    if call_id and output_value is not None and not name:
                        # 这是扁平格式的 custom_tool_call_output
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": output_value if isinstance(output_value, str) else json.dumps(output_value)
                        })
                        logger.info(f"Converted flat custom_tool_call_output to tool result")
                        continue

                # 普通的文本消息或多模态消息
                if role in ["user", "assistant", "system"]:
                    if isinstance(content, list):
                        converted_content = self._convert_content_array(content)
                        messages.append({"role": role, "content": converted_content})
                    else:
                        messages.append({"role": role, "content": content})

            # 处理工具调用 (function_call) - 转换为 assistant 消息
            elif item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name", "")
                arguments = item.get("arguments", "{}")
                if call_id:
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [{
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments
                            }
                        }]
                    })

            # 处理工具调用结果 (function_call_output)
            elif item_type == "function_call_output":
                call_id = item.get("call_id")
                output = item.get("output", "")
                if call_id:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output
                    })

            # 处理顶层的 custom_tool_call（独立元素格式）
            elif item_type == "custom_tool_call":
                call_id = item.get("call_id")
                name = item.get("name", "")
                raw_input = item.get("input", "")
                if call_id:
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [{
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": raw_input if isinstance(raw_input, str) else json.dumps(raw_input)
                            }
                        }]
                    })
                    logger.info(f"Converted top-level custom_tool_call to tool_calls: {name}")

            # 处理顶层的 custom_tool_call_output（独立元素格式）
            elif item_type == "custom_tool_call_output":
                call_id = item.get("call_id")
                output = item.get("output", "")
                if call_id:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output if isinstance(output, str) else json.dumps(output)
                    })
                    logger.info(f"Converted top-level custom_tool_call_output to tool result")

        return messages

    def _convert_content_array(self, content_array: list) -> list:
        """
        将 Responses API 的 content 数组转换为 Chat API 兼容的格式。
        主要处理：
        - input_text -> text
        - input_image -> image_url
        - output_text -> text
        """
        converted = []
        for part in content_array:
            if not isinstance(part, dict):
                continue

            part_type = part.get("type")

            if part_type == "input_text" or part_type == "output_text":
                # 转换为 Chat API 的 text 类型
                converted.append({
                    "type": "text",
                    "text": part.get("text", "")
                })
            elif part_type == "input_image":
                # 转换为 Chat API 的 image_url 类型
                image_url = part.get("image_url")
                if image_url:
                    converted.append({
                        "type": "image_url",
                        "image_url": {
                            "url": image_url if isinstance(image_url, str) else image_url.get("url", "")
                        }
                    })
            else:
                # 其他类型直接透传
                converted.append(part)

        return converted

    def convert_stream_event(self, event_line: str) -> Optional[str]:
        """
        将上游 Chat API 的 SSE 事件转换为 Responses API 格式的事件。
        按照 OpenAI Responses API 标准协议发送完整的事件序列。
        """
        # 确保 context 已初始化（如果没有通过 convert_request 初始化）
        if self.context is None:
            request_id = str(uuid.uuid4())
            self.context = StreamingContext(request_id=request_id)

        # 处理空行或空白行
        if not event_line or not event_line.strip():
            return None

        # 必须以 "data:" 开头（支持多种格式）
        stripped_line = event_line.strip()
        if not stripped_line.lower().startswith("data:"):
            logger.debug(f"Skipping non-data line: {event_line[:100]}...")
            return None

        try:
            # 提取数据内容并去除前后空格
            data_str = stripped_line[5:].strip()
            logger.debug(f"Parsing SSE data: {data_str[:200]}...")  # 只记录前200字符

            if data_str == "[DONE]":
                # 流式响应结束，发送完成事件序列
                return self._build_completion_events()

            # 尝试解析 JSON
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error in SSE data: {e}")
                logger.error(f"Problematic data: {data_str}")
                logger.error(f"Data length: {len(data_str)}, Error position: {e.pos}")
                # 遇到非正常事件时，打印错误并跳过
                return None

            # 提取 response_id（从第一个chunk中）
            if not self.context.response_id:
                self.context.response_id = data.get("id", f"resp_{uuid.uuid4().hex}")

            # 生成 item_id（如果还没有）
            if not self.context.item_id:
                self.context.item_id = f"msg_{uuid.uuid4().hex}"

            choices = data.get("choices", [])
            if not choices:
                # 检查是否有 usage 信息（在最后一个chunk中）
                if "usage" in data:
                    usage_data = data["usage"]
                    self.context.usage = {
                        "input_tokens": usage_data.get("prompt_tokens") or usage_data.get("input_tokens") or 0,
                        "output_tokens": usage_data.get("completion_tokens") or usage_data.get("output_tokens") or 0,
                    }
                    self.context.usage["total_tokens"] = (
                        self.context.usage["input_tokens"] +
                        self.context.usage["output_tokens"]
                    )
                return None

            choice = choices[0]
            delta = choice.get("delta", {})
            finish_reason = choice.get("finish_reason")

            content = delta.get("content")
            tool_calls = delta.get("tool_calls")

            events = []

            # 如果是第一个内容块（文本或工具调用），发送初始事件序列
            if not self.context.has_sent_initial_events and (content is not None or tool_calls is not None):
                # 判断是纯文本还是包含工具调用
                has_tool_calls = tool_calls is not None and isinstance(tool_calls, list) and len(tool_calls) > 0
                initial_events = self._build_initial_events(for_tool_call=has_tool_calls)
                logger.info(f"Building initial events for {'tool_call' if has_tool_calls else 'text'}, count: {len(initial_events)}")
                events.extend(initial_events)
                self.context.has_sent_initial_events = True

            # 处理文本内容
            if content is not None:
                # 累积文本
                self.context.accumulated_text += content

                # 发送文本增量事件（使用正确的命名：response.text.delta）
                seq = self.context.next_sequence()
                delta_event = {
                    "type": "response.text.delta",
                    "item_id": self.context.item_id,
                    "delta": content,
                    "sequence_number": seq
                }
                events.append(f"event: response.text.delta\ndata: {json.dumps(delta_event)}\n\n")

            # 处理工具调用
            if tool_calls is not None and isinstance(tool_calls, list):
                # 标记发生了工具调用
                self.context.has_tool_calls = True

                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue

                    index = tc.get("index", 0)

                    # 提取完整信息（id, name）
                    has_complete_info = "id" in tc or ("function" in tc and isinstance(tc["function"], dict) and "name" in tc["function"])

                    if has_complete_info:
                        call_id = tc.get("id", "")
                        name = tc.get("function", {}).get("name", "") if isinstance(tc.get("function"), dict) else ""
                        # 初始化或更新缓存状态
                        if index not in self.context.tool_call_states:
                            self.context.tool_call_states[index] = {"call_id": call_id, "name": name}
                        else:
                            # 更新非空字段
                            if call_id:
                                self.context.tool_call_states[index]["call_id"] = call_id
                            if name:
                                self.context.tool_call_states[index]["name"] = name

                    # 从缓存中获取最新状态
                    cached_state = self.context.tool_call_states.get(index, {})
                    call_id = cached_state.get("call_id", "")
                    name = cached_state.get("name", "")
                    arguments = tc.get("function", {}).get("arguments", "") if isinstance(tc.get("function"), dict) else ""

                    # 累积参数（delta 是增量，需要拼接到之前的参数上）
                    if index not in self.context.tool_call_states:
                        self.context.tool_call_states[index] = {"call_id": call_id, "name": name, "arguments": arguments}
                    else:
                        # 追加新的参数增量
                        existing_args = self.context.tool_call_states[index].get("arguments", "")
                        self.context.tool_call_states[index]["arguments"] = existing_args + arguments

                    # 检查是否是 custom tool
                    is_custom_tool = self.context.is_custom_tool(name)

                    seq = self.context.next_sequence()

                    if is_custom_tool:
                        # Custom tool: 使用 custom_tool_call_input 事件
                        # 注意：custom tool 的 arguments 是 {"input": "..."} 格式，需要提取 input 字段
                        input_value = self.context.extract_custom_tool_input(arguments)

                        tool_call_event = {
                            "type": "response.custom_tool_call_input.delta",
                            "item_id": self.context.item_id,
                            "delta": input_value if isinstance(input_value, str) else str(input_value),
                            "sequence_number": seq
                        }
                        events.append(f"event: response.custom_tool_call_input.delta\ndata: {json.dumps(tool_call_event)}\n\n")
                    else:
                        # Function tool: 使用 function_call_arguments 事件
                        tool_call_event = {
                            "type": "response.function_call_arguments.delta",
                            "item_id": self.context.item_id,  # 使用 item_id 而不是 call_id
                            "delta": arguments,  # 使用 delta 字段（增量）
                            "sequence_number": seq
                        }
                        events.append(f"event: response.function_call_arguments.delta\ndata: {json.dumps(tool_call_event)}\n\n")

            # 如果有 finish_reason，说明这是最后一个chunk，需要发送完成事件
            if finish_reason:
                events.extend(self._build_finish_events())

            return "".join(events) if events else None

        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(
                f"Failed to parse stream event. "
                f"Error: {e}. "
                f"Event line (first 500 chars): {event_line[:500]}. "
                f"Full event line length: {len(event_line)}. "
                f"Full event line: {event_line}",
                exc_info=True
            )

        return None

    def build_response_object(self, chat_response: dict, responses_payload: dict, request_id: str) -> tuple:
        """
        将上游 Chat API 的非流式响应转换为 Responses API 格式。

        Chat API Response:
        {
            "id": "chatcmpl-xxx",
            "choices": [{
                "message": {"role": "assistant", "content": "...", "tool_calls": [...]},
                "finish_reason": "stop"
            }],
            "usage": {...}
        }

        Responses API Response:
        {
            "id": "resp_xxx",
            "object": "response",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "..."}]
            }],
            "usage": {...}
        }
        """
        # 生成新的 response ID
        new_id = f"resp_{uuid.uuid4().hex}"

        # 提取 Chat API 响应内容
        choices = chat_response.get("choices", [])
        usage = chat_response.get("usage", {})

        # 构建 output 数组
        output_items = []
        for choice in choices:
            message = choice.get("message", {})
            role = message.get("role", "assistant")
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            if tool_calls:
                # 工具调用响应
                for tc in tool_calls:
                    # 检查是否是 custom tool（需要从转换记录中查找）
                    function_info = tc.get("function", {})
                    name = function_info.get("name", "")
                    arguments = function_info.get("arguments", "{}")

                    # 尝试从 context 中判断是否为 custom tool
                    is_custom_tool = self.context.is_custom_tool(name) if self.context else False

                    if is_custom_tool:
                        # 这是 custom tool，使用 extract_custom_tool_input 提取 input 字段
                        input_value = self.context.extract_custom_tool_input(arguments)
                        logger.debug(f"🔄 [REVERSE CONVERT] Function call '{name}' -> Custom tool call")
                        logger.debug(f"   Arguments: {arguments}")
                        logger.debug(f"   Extracted input: {input_value}")

                        # 转换为 custom_tool_call 格式，使用提取的 input 值
                        output_items.append({
                            "type": "custom_tool_call",
                            "call_id": tc.get("id", f"call_{uuid.uuid4().hex}"),
                            "name": name,
                            "input": input_value if isinstance(input_value, str) else json.dumps(input_value)
                        })
                        logger.debug(f"Converted function_call back to custom_tool_call: {name}")
                    else:
                        # 普通 function_call
                        output_items.append({
                            "type": "function_call",
                            "call_id": tc.get("id", f"call_{uuid.uuid4().hex}"),
                            "name": name,
                            "arguments": arguments
                        })
            elif content:
                # 文本消息响应
                output_items.append({
                    "type": "message",
                    "role": role,
                    "content": [
                        {
                            "type": "output_text",
                            "text": content
                        }
                    ]
                })

        # 构建完整的 Responses API 响应对象
        response_obj = {
            "id": new_id,
            "object": "response",
            "created_at": int(__import__('time').time()),
            "model": chat_response.get("model", responses_payload.get("model", "unknown")),
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "instructions": responses_payload.get("instructions"),
            "max_output_tokens": responses_payload.get("max_output_tokens"),
            "metadata": {},
            "output": output_items,
            "parallel_tool_calls": False,
            "previous_response_id": responses_payload.get("previous_response_id"),
            "reasoning": {},
            "temperature": responses_payload.get("temperature", 1.0),
            "text": {},
            "tool_choice": responses_payload.get("tool_choice", "auto"),
            "tools": responses_payload.get("tools", []),
            "top_p": responses_payload.get("top_p", 1.0),
            "usage": {
                "input_tokens": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                "output_tokens": usage.get("completion_tokens") or usage.get("output_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0
            },
            "user": None
        }

        # 清理请求级别的转换记录（通过 context.cleanup 自动完成）
        if self.context:
            self.context.cleanup()

        return response_obj, new_id

    def _build_initial_events(self, for_tool_call: bool = False) -> List[str]:
        """构建初始事件序列（response.queued, created, in_progress, output_item.added, content_part.added）

        Args:
            for_tool_call: 如果是为工具调用构建初始事件，则使用 function_call 类型
        """
        events = []
        response_id = self.context.response_id
        item_id = self.context.item_id

        # 1. response.queued (第一个事件)
        seq = self.context.next_sequence()
        queued_event = {
            "type": "response.queued",
            "id": response_id,
            "object": "response",
            "model": self.context.model_name,
            "status": "queued",
            "sequence_number": seq
        }
        events.append(f"event: response.queued\ndata: {json.dumps(queued_event)}\n\n")

        # 2. response.created
        seq = self.context.next_sequence()
        created_event = {
            "type": "response.created",
            "id": response_id,
            "object": "response",
            "model": self.context.model_name,
            "status": "in_progress",
            "sequence_number": seq
        }
        events.append(f"event: response.created\ndata: {json.dumps(created_event)}\n\n")

        # 3. response.in_progress
        seq = self.context.next_sequence()
        in_progress_event = {
            "type": "response.in_progress",
            "sequence_number": seq
        }
        events.append(f"event: response.in_progress\ndata: {json.dumps(in_progress_event)}\n\n")

        # 4. response.output_item.added
        seq = self.context.next_sequence()
        if for_tool_call:
            # 工具调用类型的 output_item
            item_added_event = {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": item_id,
                    "type": "function_call",
                    "call_id": "",  # 将在后续 delta 事件中填充
                    "name": "",
                    "arguments": "",
                    "status": "in_progress"
                },
                "sequence_number": seq
            }
        else:
            # 文本消息类型的 output_item
            item_added_event = {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "status": "in_progress"
                },
                "sequence_number": seq
            }
        events.append(f"event: response.output_item.added\ndata: {json.dumps(item_added_event)}\n\n")

        # 5. 对于文本类型，发送 response.content_part.added
        if not for_tool_call:
            seq = self.context.next_sequence()
            part_added_event = {
                "type": "response.content_part.added",
                "item_id": item_id,
                "content_index": 0,
                "part": {
                    "type": "output_text",
                    "text": ""
                },
                "sequence_number": seq
            }
            events.append(f"event: response.content_part.added\ndata: {json.dumps(part_added_event)}\n\n")

        return events

    def _build_finish_events(self) -> List[str]:
        """构建完成事件序列（text.done, content_part.done, output_item.done）"""
        events = []
        item_id = self.context.item_id
        accumulated_text = self.context.accumulated_text
        has_tool_calls = self.context.has_tool_calls

        if has_tool_calls:
            # 工具调用的完成事件
            seq = self.context.next_sequence()

            # 收集所有工具调用的信息，并检查是否是 custom tool
            tool_calls_info = []
            for index, state in sorted(self.context.tool_call_states.items()):
                name = state.get("name", "")
                arguments = state.get("arguments", "")

                # 检查是否是 custom tool
                is_custom_tool = self.context.is_custom_tool(name)

                if is_custom_tool:
                    # Custom tool: 提取 input 字段
                    input_value = self.context.extract_custom_tool_input(arguments)

                    tool_calls_info.append({
                        "id": state.get("call_id", ""),
                        "type": "custom_tool_call",
                        "name": name,
                        "input": input_value if isinstance(input_value, str) else str(input_value)
                    })
                else:
                    # Function tool
                    tool_calls_info.append({
                        "id": state.get("call_id", ""),
                        "type": "function_call",
                        "name": name,
                        "arguments": arguments
                    })

            # 构建 output_item.done 事件（使用第一个工具的信息）
            first_tool = tool_calls_info[0] if tool_calls_info else {}
            item_done_event = {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": item_id,
                    "type": first_tool.get("type", "function_call"),
                    "call_id": first_tool.get("id", ""),
                    "name": first_tool.get("name", ""),
                    **({"input": first_tool.get("input", "")} if first_tool.get("type") == "custom_tool_call" else {"arguments": first_tool.get("arguments", "")}),
                    "status": "completed"
                },
                "sequence_number": seq
            }
            events.append(f"event: response.output_item.done\ndata: {json.dumps(item_done_event)}\n\n")
        else:
            # 文本消息的完成事件
            # 1. response.text.done (使用正确的命名)
            seq = self.context.next_sequence()
            text_done_event = {
                "type": "response.text.done",
                "item_id": item_id,
                "content_index": 0,
                "text": accumulated_text,
                "sequence_number": seq
            }
            events.append(f"event: response.text.done\ndata: {json.dumps(text_done_event)}\n\n")

            # 2. response.content_part.done
            seq = self.context.next_sequence()
            part_done_event = {
                "type": "response.content_part.done",
                "item_id": item_id,
                "content_index": 0,
                "sequence_number": seq
            }
            events.append(f"event: response.content_part.done\ndata: {json.dumps(part_done_event)}\n\n")

            # 3. response.output_item.done
            seq = self.context.next_sequence()
            item_done_event = {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": accumulated_text
                        }
                    ]
                },
                "sequence_number": seq
            }
            events.append(f"event: response.output_item.done\ndata: {json.dumps(item_done_event)}\n\n")

        return events

    def _build_completion_events(self) -> str:
        """构建最终完成事件（response.completed + [DONE]）"""
        events = []
        response_id = self.context.response_id
        usage = self.context.usage or {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0
        }

        # response.completed
        seq = self.context.next_sequence()

        # 构建 output 数组（根据是否有工具调用决定类型）
        has_tool_calls = self.context.has_tool_calls
        if has_tool_calls and self.context.tool_call_states:
            # 有工具调用，检查是否是 custom tool
            output_items = []
            for index, state in sorted(self.context.tool_call_states.items()):
                name = state.get("name", "")
                arguments = state.get("arguments", "")

                # 检查是否是 custom tool
                is_custom_tool = self.context.is_custom_tool(name)

                if is_custom_tool:
                    # Custom tool: 提取 input 字段
                    input_value = self.context.extract_custom_tool_input(arguments)

                    output_items.append({
                        "id": self.context.item_id,
                        "type": "custom_tool_call",
                        "call_id": state.get("call_id", ""),
                        "name": name,
                        "input": input_value if isinstance(input_value, str) else str(input_value),
                        "status": "completed"
                    })
                else:
                    # Function tool
                    output_items.append({
                        "id": self.context.item_id,
                        "type": "function_call",
                        "call_id": state.get("call_id", ""),
                        "name": name,
                        "arguments": arguments,
                        "status": "completed"
                    })
        else:
            # 没有工具调用，输出 message 类型的 output
            output_items = [
                {
                    "id": self.context.item_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": self.context.accumulated_text
                        }
                    ],
                    "status": "completed"
                }
            ]

        # 构建 Response 对象（嵌套在 response 字段中）
        response_obj = {
            "id": response_id,
            "object": "response",
            "created_at": int(__import__('time').time()),
            "model": self.context.model_name,
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": None,
            "metadata": {},
            "output": output_items,
            "parallel_tool_calls": False,
            "previous_response_id": None,
            "reasoning": {},
            "temperature": 1.0,
            "text": {},
            "tool_choice": "auto",
            "tools": [],
            "top_p": 1.0,
            "usage": usage,
            "user": None
        }

        # 构建 completed 事件（包含 response 字段）
        completed_event = {
            "type": "response.completed",
            "response": response_obj,
            "sequence_number": seq
        }
        events.append(f"event: response.completed\ndata: {json.dumps(completed_event)}\n\n")

        # [DONE] 标记
        events.append("data: [DONE]\n\n")

        # 清理状态
        self.context.cleanup()

        return "".join(events)
