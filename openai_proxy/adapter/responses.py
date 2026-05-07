import json
import logging
import uuid
from typing import Dict, List, Any, Optional
from ..utils.session import SessionStore
from ..utils.tool_call_converter import ToolCallConverter

logger = logging.getLogger(__name__)


class ResponsesAdapter:
    """
    OpenAI Responses API 到 Chat Completions API 的协议转换器。
    负责将 /v1/responses 的请求格式转换为 /v1/chat/completions 兼容的格式。
    """

    def __init__(self, session_store: Optional[SessionStore] = None):
        self.session_store = session_store
        # 流式工具调用状态缓存：{index: {"call_id": str, "name": str}}
        self._streaming_tool_call_state: Dict[int, Dict[str, str]] = {}
        # 请求级别的 custom tools 转换记录：{request_id: {tool_name: original_tool}}
        self._request_custom_tools: Dict[str, Dict[str, dict]] = {}

    async def convert_request(self, responses_payload: dict) -> dict:
        """
        将 Responses API 请求体转换为 Chat API 请求体。
        """

        # 初始化流式工具调用状态
        self._streaming_tool_call_state = {}
        # 生成请求ID，用于隔离并发请求的转换记录
        request_id = str(uuid.uuid4())
        # 创建请求级别的 custom tools 转换记录
        converted_custom_tools: Dict[str, dict] = {}
        # 存储到实例字典中，供响应转换时使用
        self._request_custom_tools[request_id] = converted_custom_tools

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

        # GPT-5 参数适配
        is_gpt5 = model and (model.startswith("gpt-5") or model == "gpt-5")
        if "temperature" in responses_payload:
            temp = responses_payload["temperature"]
            # GPT-5 要求 temperature 必须为 1 或不传
            if is_gpt5 and temp != 1:
                logger.warning(f"Model {model} requires temperature=1, overriding from {temp}")
                chat_payload["temperature"] = 1
            else:
                chat_payload["temperature"] = temp
        elif is_gpt5:
            # 如果没传 temperature，GPT-5 默认为 1
            chat_payload["temperature"] = 1

        if "top_p" in responses_payload:
            chat_payload["top_p"] = responses_payload["top_p"]

        # 处理 reasoning.effort
        if "reasoning" in responses_payload and isinstance(responses_payload["reasoning"], dict):
            effort = responses_payload["reasoning"].get("effort")
            if effort:
                # 记录 reasoning effort，下游 Chat API 可能不支持，但至少记录下来
                logger.info(f"Reasoning effort: {effort}")
        elif is_gpt5:
            # 如果没有显式指定 reasoning.effort，根据模型类型推断
            # 这里简化处理，默认使用 minimal
            logger.info("Auto-setting reasoning.effort to 'minimal' for GPT-5")

        # 处理工具选择
        if "tool_choice" in responses_payload:
            chat_payload["tool_choice"] = responses_payload["tool_choice"]

        # 处理工具定义 - 将 Responses API tools 转换为 Chat API tools
        if "tools" in responses_payload:
            chat_payload["tools"] = self._convert_tools(responses_payload["tools"], converted_custom_tools)

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
        """
        if not event_line.startswith("data: "):
            return event_line

        try:
            data_str = event_line[6:]  # 去掉 "data: " 前缀
            if data_str.strip() == "[DONE]":
                # 清理流式工具调用状态（流式响应结束）
                self._streaming_tool_call_state.clear()
                return "event: response.completed\ndata: {\"status\": \"completed\"}\n\n"

            data = json.loads(data_str)

            choices = data.get("choices", [])
            if not choices:
                return None

            delta = choices[0].get("delta", {})

            content = delta.get("content")
            tool_calls = delta.get("tool_calls")

            # 处理文本内容
            if content is not None:
                # 转换为 Responses API 的文本增量事件
                response_event = {
                    "type": "response.output_text.delta",
                    "delta": content,
                    "output_index": 0,
                    "content_index": 0
                }
                return f"event: response.output_text.delta\ndata: {json.dumps(response_event)}\n\n"

            # 处理工具调用
            if tool_calls is not None and isinstance(tool_calls, list):
                events = []
                for i, tc in enumerate(tool_calls):
                    if not isinstance(tc, dict):
                        continue

                    # 提取 index（用于状态缓存的key）
                    index = tc.get("index", 0)

                    # 检测是否包含完整的元数据（第一个chunk）
                    has_complete_info = "id" in tc or ("function" in tc and isinstance(tc["function"], dict) and "name" in tc["function"])

                    if has_complete_info:
                        # 第一个chunk：建立状态缓存
                        call_id = tc.get("id", "")
                        name = tc.get("function", {}).get("name", "") if isinstance(tc.get("function"), dict) else ""

                        # 存入状态缓存
                        self._streaming_tool_call_state[index] = {
                            "call_id": call_id,
                            "name": name
                        }
                    else:
                        # 后续chunks：尝试从缓存获取
                        pass

                    # 优先使用缓存状态，降级到当前chunk
                    cached_state = self._streaming_tool_call_state.get(index, {})
                    call_id = cached_state.get("call_id") or tc.get("id", "")
                    name = cached_state.get("name") or (tc.get("function", {}).get("name", "") if isinstance(tc.get("function"), dict) else "")
                    arguments = tc.get("function", {}).get("arguments", "") if isinstance(tc.get("function"), dict) else ""

                    # 构建 Responses API 的工具调用增量事件
                    tool_call_event = {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments
                    }
                    events.append(f"event: response.function_call_arguments.delta\ndata: {json.dumps(tool_call_event)}\n\n")

                # 返回所有工具调用事件
                if events:
                    return "".join(events)

            # 兼容非标准平台：检查content是否包含非标准格式的工具调用
            if content is not None and isinstance(content, str) and ToolCallConverter.is_non_standard_format(content):
                # 转换为标准格式并生成事件
                converted_tool_calls, _ = ToolCallConverter.convert_to_standard_format(content, [])
                if converted_tool_calls:
                    logger.debug(f"检测到非标准工具调用格式，已转换为标准格式")
                    # 为每个转换后的工具调用生成事件
                    events = []
                    for tc in converted_tool_calls:
                        function_info = tc.get("function", {})
                        tool_call_event = {
                            "type": "response.function_call_arguments.delta",
                            "output_index": 0,
                            "call_id": tc.get("id", ""),
                            "name": function_info.get("name", ""),
                            "arguments": function_info.get("arguments", "{}")
                        }
                        events.append(f"event: response.function_call_arguments.delta\ndata: {json.dumps(tool_call_event)}\n\n")
                    return "".join(events)

        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.debug(f"Failed to parse stream event: {event_line[:100]} Error: {e}")

        return None

    def build_response_object(self, chat_response: dict, original_request: dict, request_id: str = "") -> tuple:
        """
        将上游 Chat API 的响应包装为 Responses API 的响应对象。
        """
        import uuid

        choices = chat_response.get("choices", [])
        content_text = ""
        tool_calls = []

        if choices and choices[0].get("message"):
            message = choices[0]["message"]
            content_text = message.get("content", "")
            tool_calls = message.get("tool_calls") or []

            # 使用转换器处理非标准格式
            if not tool_calls and content_text:
                converted_tool_calls, remaining_content = ToolCallConverter.convert_to_standard_format(
                    content=content_text,
                    existing_tool_calls=tool_calls
                )
                if converted_tool_calls:

                    tool_calls = converted_tool_calls
                    content_text = remaining_content

        new_response_id = f"resp_{uuid.uuid4().hex}"

        # 确保 usage 字段有效，如果缺失或包含 null 值则提供默认值
        usage = chat_response.get("usage")
        if usage is None:
            usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0
            }
        else:
            # 转换 Chat Completions API 格式到 Responses API 格式
            # Chat API 使用 prompt_tokens/completion_tokens
            # Responses API 使用 input_tokens/output_tokens
            input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
            output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")

            usage = {
                "input_tokens": input_tokens if input_tokens is not None else 0,
                "output_tokens": output_tokens if output_tokens is not None else 0,
            }
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

        # 构建 output 数组
        output_items = []

        # 如果有文本内容，添加消息
        if content_text:
            output_items.append({
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content_text
                    }
                ]
            })

        # 如果有工具调用，添加工具调用项
        # 获取请求级别的 custom tools 转换记录
        converted_custom_tools = self._request_custom_tools.get(request_id, {})

        for i, tc in enumerate(tool_calls):
            function_info = tc.get("function", {})
            tool_name = function_info.get("name", "")
            arguments_str = function_info.get("arguments", "{}")

            # 检查这个工具是否原本是被转换的 custom tool
            if tool_name in converted_custom_tools:
                # 这是 custom tool，需要从 arguments JSON 中提取 input 字段
                try:
                    import json
                    arguments_obj = json.loads(arguments_str)
                    # 提取 input 字段的值（纯文本）
                    input_value = arguments_obj.get("input", arguments_str)
                    logger.debug(f"🔄 [REVERSE CONVERT] Function call '{tool_name}' -> Custom tool call")
                    logger.debug(f"   Arguments: {arguments_str}")
                    logger.debug(f"   Extracted input: {input_value}")
                except (json.JSONDecodeError, AttributeError) as e:
                    # 如果解析失败，直接使用原始 arguments
                    input_value = arguments_str
                    logger.warning(f"JSON 解析失败: {e}，使用原始字符串")

                # 转换为 custom_tool_call 格式，使用提取的 input 值
                output_items.append({
                    "type": "custom_tool_call",
                    "call_id": tc.get("id", ""),
                    "name": tool_name,
                    "input": input_value if isinstance(input_value, str) else json.dumps(input_value)
                })
                logger.debug(f"Converted function_call back to custom_tool_call: {tool_name}")
            else:
                # 这是普通的 function tool
                output_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": tool_name,
                    "arguments": arguments_str
                })

        response_obj = {
            "id": new_response_id,
            "object": "response",
            "created_at": int(chat_response.get("created", 0)),
            "model": chat_response.get("model", original_request.get("model")),
            "status": "completed",
            "output": output_items,
            "usage": usage
        }

        # 清理流式工具调用状态（非流式响应结束）
        self._streaming_tool_call_state.clear()

        # 清理请求级别的 custom tools 转换记录，避免内存泄漏
        if request_id and request_id in self._request_custom_tools:
            del self._request_custom_tools[request_id]
            logger.debug(f"Cleaned up custom tools record for request: {request_id}")

        return response_obj, new_response_id
