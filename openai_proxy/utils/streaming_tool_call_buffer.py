"""
流式工具调用缓冲器

实现早期检测 + 智能缓冲策略，用于处理流式响应中的非标准工具调用格式。
"""

import time
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class StreamingToolCallBuffer:
    """
    流式工具调用缓冲器
    
    采用早期启发式检测 + 完整性验证 + 智能缓冲策略：
    1. 早期检测：通过简单特征快速识别潜在的非标准格式
    2. 最小缓冲：仅在检测到非标准特征后才开始缓冲
    3. 快速降级：误判时快速放弃转换，恢复流式转发
    4. 完整性验证：缓冲后仍需验证格式完整性才执行转换
    """
    
    def __init__(self, timeout_seconds: float = 5.0, max_buffer_size: int = 10240):
        """
        初始化缓冲器
        
        Args:
            timeout_seconds: 缓冲超时时间（秒），默认 5 秒
            max_buffer_size: 最大缓冲区大小（字节），默认 10KB
        """
        self.content_buffer = ""
        self.detected_non_standard = False  # 是否检测到非标准特征
        self.converted = False
        self.buffer_start_time: Optional[float] = None
        self.timeout_seconds = timeout_seconds
        self.max_buffer_size = max_buffer_size
    
    def _looks_like_non_standard(self, content: str) -> bool:
        """
        早期启发式检测：判断内容是否可能是非标准格式
        
        Args:
            content: 内容字符串
            
        Returns:
            True 如果看起来像非标准格式
        """
        stripped = content.strip()
        
        # JSON 特征：以 { 开头
        if stripped.startswith('{'):
            return True
        
        # XML 特征：包含 <invoke 或 <minimax
        if '<invoke' in content or '<minimax' in content:
            return True
        
        return False
    
    def process_chunk(self, chunk_data: dict, tool_call_converter) -> List[str]:
        """
        处理单个流式 chunk
        
        Args:
            chunk_data: SSE chunk 的解析数据
            tool_call_converter: ToolCallConverter 实例
            
        Returns:
            List[str]: 需要发送的 SSE 事件列表（可能为空、单个或多个）
        """
        from openai_proxy.utils.tool_call_converter import ToolCallConverter
        
        delta = chunk_data.get('choices', [{}])[0].get('delta', {})
        content = delta.get('content', '')
        tool_calls = delta.get('tool_calls', [])
        
        # 如果已有标准 tool_calls，直接转发
        if tool_calls:
            return [self._format_chunk(chunk_data)]
        
        # 如果没有 content，直接转发
        if not content:
            return [self._format_chunk(chunk_data)]
        
        # 【关键优化】早期检测：仅在没有检测过时执行
        if not self.detected_non_standard:
            if self._looks_like_non_standard(content):
                self.detected_non_standard = True
                self.buffer_start_time = time.time()
                logger.debug("Detected potential non-standard format")
            else:
                # 标准格式，立即转发并停止检测
                return [self._format_chunk(chunk_data)]
        
        # 已标记为非标准，开始缓冲
        if self.detected_non_standard and not self.converted:
            # 超时保护
            if self.buffer_start_time and (time.time() - self.buffer_start_time) > self.timeout_seconds:
                logger.warning("Buffer timeout, falling back to streaming")
                self.converted = True  # 标记为已完成（放弃转换）
                return [self._format_text_chunk(self.content_buffer), self._format_chunk(chunk_data)]
            
            # 内存保护
            if len(self.content_buffer) > self.max_buffer_size:
                logger.warning("Buffer size exceeded, falling back to streaming")
                self.converted = True
                return [self._format_text_chunk(self.content_buffer), self._format_chunk(chunk_data)]
            
            # 累积 content
            self.content_buffer += content
            
            # 尝试检测和转换
            if ToolCallConverter.is_complete_format(self.content_buffer):
                try:
                    converted_tool_calls, remaining_content = ToolCallConverter.convert_to_standard_format(
                        self.content_buffer, []
                    )
                    
                    if converted_tool_calls and len(converted_tool_calls) > 0:
                        self.converted = True
                        # 生成 tool_call 事件
                        events = self._generate_tool_call_events(converted_tool_calls)
                        
                        # 如果有剩余内容（普通文本），立即流式发送
                        if remaining_content:
                            events.append(self._format_text_chunk(remaining_content))
                        
                        logger.info(
                            f"Tool call converted successfully | "
                            f"buffer_time={time.time() - self.buffer_start_time:.2f}s | "
                            f"tool_calls_count={len(converted_tool_calls)}"
                        )
                        
                        return events
                    else:
                        # 看起来像非标准但转换失败 → 放弃缓冲，转回流式
                        logger.warning("Conversion failed, falling back to streaming")
                        self.converted = True
                        return [self._format_text_chunk(self.content_buffer), self._format_chunk(chunk_data)]
                        
                except Exception as e:
                    logger.error(f"Conversion error: {e}, falling back to streaming")
                    self.converted = True
                    return [self._format_text_chunk(self.content_buffer), self._format_chunk(chunk_data)]
            
            # 还不完整，继续缓冲
            return []  # 暂时不发送
        
        # 已转换完成或放弃转换，正常流式转发
        return [self._format_chunk(chunk_data)]
    
    def _format_chunk(self, chunk_data: dict) -> str:
        """将 chunk 数据格式化为 SSE 事件"""
        import json
        return f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
    
    def _format_text_chunk(self, text: str) -> str:
        """将文本内容格式化为 SSE 事件"""
        import json
        chunk = {
            "choices": [{
                "delta": {
                    "content": text,
                    "role": "assistant"
                },
                "index": 0,
                "finish_reason": None
            }]
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    
    def _generate_tool_call_events(self, tool_calls: List[Dict]) -> List[str]:
        """
        生成 tool_calls 的 SSE 事件
        
        Args:
            tool_calls: 转换后的标准 tool_calls 列表
            
        Returns:
            List[str]: SSE 事件列表
        """
        import json
        events = []
        
        for idx, tool_call in enumerate(tool_calls):
            event = {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": idx,
                            "id": tool_call.get("id", ""),
                            "type": tool_call.get("type", "function"),
                            "function": {
                                "name": tool_call["function"]["name"],
                                "arguments": tool_call["function"]["arguments"]
                            }
                        }],
                        "role": "assistant"
                    },
                    "index": 0,
                    "finish_reason": None
                }]
            }
            events.append(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
        
        return events
    
    def reset(self):
        """重置缓冲器状态"""
        self.content_buffer = ""
        self.detected_non_standard = False
        self.converted = False
        self.buffer_start_time = None
