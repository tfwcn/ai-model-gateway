import json
import logging
from typing import Dict, List, Any, Optional
from ..utils.session import RedisSessionStore

logger = logging.getLogger(__name__)


class ResponsesAdapter:
    """
    OpenAI Responses API 到 Chat Completions API 的协议转换器。
    负责将 /v1/responses 的请求格式转换为 /v1/chat/completions 兼容的格式。
    """

    def __init__(self, session_store: Optional[RedisSessionStore] = None):
        self.session_store = session_store or RedisSessionStore()
        # 流式工具调用状态缓存：{index: {"call_id": str, "name": str}}
        self._streaming_tool_call_state: Dict[int, Dict[str, str]] = {}

    async def convert_request(self, responses_payload: dict) -> dict:
        """
        将 Responses API 请求体转换为 Chat API 请求体。
        """
        # 初始化流式工具调用状态
        self._streaming_tool_call_state = {}
        
        # 1. 提取基础字段
        model = responses_payload.get("model")
        stream = responses_payload.get("stream", False)
        
        logger.debug(f"DEBUG: 原始请求: {responses_payload}")
        
        # 2. 提取并处理历史记录 (previous_response_id)
        prev_id = responses_payload.get("previous_response_id")
        history = []
        if prev_id and self.session_store:
            history = await self.session_store.get_history(prev_id)
        
        # 3. 转换当前 input 为 messages
        input_items = responses_payload.get("input", [])
        logger.debug(f"DEBUG: input_items (完整): {json.dumps(input_items, ensure_ascii=False, indent=2)}")
        current_messages = self._convert_input_to_messages(input_items)
        logger.debug(f"DEBUG: current_messages: {current_messages}")
        
        # 4. 合并历史消息与当前消息
        messages = history + current_messages
        logger.debug(f"DEBUG: 最终messages: {messages}")
        
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
            chat_payload["tools"] = self._convert_tools(responses_payload["tools"])
            logger.debug(f"DEBUG: 转换了 {len(chat_payload['tools'])} 个工具")
            
        return chat_payload

    def _convert_tools(self, response_tools: list) -> list:
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
            else:
                # 其他类型的工具直接传递（如果有）
                chat_tools.append(tool)
        
        return chat_tools

    def _convert_input_to_messages(self, input_items: list) -> list:
        """
        将 Responses API 的 input 数组转换为 Chat API 的 messages 数组。
        支持两种格式：
        1. 完整格式: {"type": "message", "role": "user", "content": "..."}
        2. 简化格式: {"role": "user", "content": "..."}
        """
        messages = []
        for item in input_items:
            if not isinstance(item, dict):
                continue
            
            # 兼容简化格式：如果没有type字段但有role字段，直接当作message处理
            item_type = item.get("type")
            role = item.get("role")
            content = item.get("content", "")
            
            # 如果是简化格式（有role但没有type）
            if role and not item_type:
                if role in ["user", "assistant", "system"]:
                    messages.append({"role": role, "content": content})
                continue
            
            # 处理消息类型
            if item_type == "message":
                if role in ["user", "assistant", "system"]:
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
                    
        return messages

    def convert_stream_event(self, event_line: str) -> Optional[str]:
        """
        将上游 Chat API 的 SSE 事件转换为 Responses API 格式的事件。
        """
        if not event_line.startswith("data: "):
            return event_line
            
        try:
            data_str = event_line[6:] # 去掉 "data: " 前缀
            if data_str.strip() == "[DONE]":
                # 清理流式工具调用状态（流式响应结束）
                logger.debug(f"DEBUG: 清理流式工具调用状态（流式响应完成）")
                self._streaming_tool_call_state.clear()
                return "event: response.completed\ndata: {\"status\": \"completed\"}\n\n"
            
            data = json.loads(data_str)
            logger.debug(f"DEBUG Stream: 收到数据 keys={list(data.keys())}")
            
            choices = data.get("choices", [])
            if not choices:
                return None
                
            delta = choices[0].get("delta", {})
            logger.debug(f"DEBUG Stream: delta keys={list(delta.keys()) if isinstance(delta, dict) else 'N/A'}")
            
            content = delta.get("content")
            tool_calls = delta.get("tool_calls")
            
            # 处理文本内容
            if content is not None:
                logger.debug(f"DEBUG Stream: 有content，长度={len(content)}")
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
                logger.debug(f"DEBUG Stream: 有tool_calls，数量={len(tool_calls)}")
                events = []
                for i, tc in enumerate(tool_calls):
                    logger.debug(f"DEBUG Stream: tool_call[{i}]完整数据={tc}")
                    
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
                        logger.debug(f"DEBUG Stream: 建立状态缓存 index={index}, call_id={call_id}, name={name}")
                    else:
                        # 后续chunks：尝试从缓存获取
                        logger.debug(f"DEBUG Stream: 尝试从缓存获取 index={index}")
                    
                    # 优先使用缓存状态，降级到当前chunk
                    cached_state = self._streaming_tool_call_state.get(index, {})
                    call_id = cached_state.get("call_id") or tc.get("id", "")
                    name = cached_state.get("name") or (tc.get("function", {}).get("name", "") if isinstance(tc.get("function"), dict) else "")
                    arguments = tc.get("function", {}).get("arguments", "") if isinstance(tc.get("function"), dict) else ""
                    
                    if cached_state:
                        logger.debug(f"DEBUG Stream: 使用缓存状态 index={index}, call_id={call_id}, name={name}")
                    
                    # 构建 Responses API 的工具调用增量事件
                    tool_call_event = {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments
                    }
                    logger.debug(f"DEBUG Stream: 返回tool_call_event keys={list(tool_call_event.keys())}")
                    events.append(f"event: response.function_call_arguments.delta\ndata: {json.dumps(tool_call_event)}\n\n")
                
                # 返回所有工具调用事件
                if events:
                    return "".join(events)
                
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.debug(f"Failed to parse stream event: {event_line[:100]} Error: {e}")
            
        return None

    def build_response_object(self, chat_response: dict, original_request: dict) -> dict:
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
        for tc in tool_calls:
            function_info = tc.get("function", {})
            output_items.append({
                "type": "function_call",
                "call_id": tc.get("id", ""),
                "name": function_info.get("name", ""),
                "arguments": function_info.get("arguments", "{}")
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
        logger.debug(f"DEBUG: 清理流式工具调用状态（非流式响应完成）")
        self._streaming_tool_call_state.clear()
        
        return response_obj, new_response_id
