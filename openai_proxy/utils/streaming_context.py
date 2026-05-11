"""
Streaming Context - 流式响应上下文管理器

统一管理所有流式响应状态,替代分散的 _streaming_state、_streaming_tool_call_state 
和 _request_custom_tools 字典,降低内存泄漏风险并简化请求隔离逻辑。

设计动机:
- 重构前,ResponsesAdapter 类中维护了三套独立的状态字典,职责不清且容易导致内存泄漏
- 将所有流式相关状态集中到 StreamingContext 中,实现单一职责原则
- 通过 reset() 和 cleanup() 方法显式控制生命周期,防止内存泄漏
- 提取 custom tool input 转换逻辑,消除三处重复代码

使用示例:
    context = StreamingContext(request_id="req-123")
    seq = context.next_sequence()
    context.response_id = "resp-456"
    # ... 处理流式响应 ...
    context.cleanup()  # 清理可变状态
"""
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StreamingContext:
    """
    流式响应上下文管理器
    
    封装所有流式相关状态,提供:
    - 统一的初始化和清理接口
    - 序列号自增管理
    - Custom tool input 提取逻辑
    - 防止内存泄漏的状态清理机制
    
    与旧实现相比的优势:
    - 单一职责: 所有流式相关状态集中管理,避免字典 key 拼写错误
    - 生命周期清晰: 通过 reset() 和 cleanup() 方法显式控制
    - 类型安全: 使用类型注解定义字段,IDE 可以提供更好的代码补全
    - 易于测试: 可以独立于 ResponsesAdapter 进行单元测试
    
    字段映射关系:
        原 _streaming_state          → StreamingContext 属性
          sequence_number            → self.sequence_number: int
          response_id                → self.response_id: Optional[str]
          item_id                    → self.item_id: Optional[str]
          has_sent_initial_events    → self.has_sent_initial_events: bool
          accumulated_text           → self.accumulated_text: str
          usage                      → self.usage: Optional[Dict]
          request_id                 → self.request_id: str (构造函数传入)
          has_tool_calls             → self.has_tool_calls: bool
        
        原 _streaming_tool_call_state → StreamingContext.tool_call_states: Dict[int, Dict]
        原 _request_custom_tools      → StreamingContext.custom_tools_map: Dict[str, dict]
    
    使用示例:
        context = StreamingContext(request_id="req-123")
        seq = context.next_sequence()
        context.response_id = "resp-456"
        # ... 处理流式响应 ...
        context.cleanup()  # 清理可变状态
    """
    
    def __init__(self, request_id: str):
        """
        初始化流式上下文
        
        Args:
            request_id: 请求唯一标识符（在整个生命周期中保持不变）
        """
        self.request_id = request_id
        
        # 基本状态字段（对应原 _streaming_state）
        self.sequence_number: int = 0
        self.response_id: Optional[str] = None
        self.item_id: Optional[str] = None
        self.has_sent_initial_events: bool = False
        self.accumulated_text: str = ""
        self.usage: Optional[Dict[str, int]] = None
        self.has_tool_calls: bool = False
        self.model_name: str = "unknown"  # 从请求或上游响应动态获取
        
        # 工具调用状态（对应原 _streaming_tool_call_state）
        self.tool_call_states: Dict[int, Dict[str, str]] = {}
        
        # Custom tools 映射（对应原 _request_custom_tools）
        self.custom_tools_map: Dict[str, dict] = {}
    
    def next_sequence(self) -> int:
        """
        获取下一个序列号并自增
        
        Returns:
            当前序列号（从 0 开始）
        """
        seq = self.sequence_number
        self.sequence_number += 1
        return seq
    
    def reset(self):
        """
        重置为初始状态，保留 request_id
        
        用于在同一连接中处理多个请求时复用上下文对象。
        """
        self.sequence_number = 0
        self.response_id = None
        self.item_id = None
        self.has_sent_initial_events = False
        self.accumulated_text = ""
        self.usage = None
        self.has_tool_calls = False
        self.model_name = "unknown"
        self.tool_call_states.clear()
        self.custom_tools_map.clear()
    
    def cleanup(self):
        """
        清理可变状态，防止内存泄漏
        
        在流式响应完成后调用，清空可能占用大量内存的字典。
        注意：此方法不会重置基本状态字段，因为响应已经结束。
        """
        self.tool_call_states.clear()
        self.custom_tools_map.clear()
        logger.debug(f"StreamingContext cleaned up for request {self.request_id}")
    
    def extract_custom_tool_input(self, arguments: str) -> Any:
        """
        从 custom tool arguments 中提取 input 字段
        
        Custom tool 的参数格式为 {"input": <actual_input>}，此方法提取内部的 input 值。
        如果 arguments 不是有效的 JSON 或不包含 input 字段，则返回原始 arguments。
        
        Args:
            arguments: 工具调用的参数字符串（JSON 格式）
            
        Returns:
            提取的 input 值，或原始 arguments
        """
        try:
            args_obj = json.loads(arguments) if arguments else {}
            if isinstance(args_obj, dict):
                return args_obj.get("input", arguments)
            else:
                return arguments
        except (json.JSONDecodeError, AttributeError) as e:
            logger.debug(f"Failed to parse custom tool arguments: {e}")
            return arguments
    
    def register_custom_tool(self, tool_name: str, tool_config: dict):
        """
        注册 custom tool 配置
        
        Args:
            tool_name: 工具名称
            tool_config: 工具配置字典
        """
        self.custom_tools_map[tool_name] = tool_config
        logger.debug(f"Registered custom tool: {tool_name}")
    
    def is_custom_tool(self, tool_name: str) -> bool:
        """
        判断是否为 custom tool
        
        Args:
            tool_name: 工具名称
            
        Returns:
            如果是 custom tool 返回 True
        """
        return tool_name in self.custom_tools_map
    
    def get_state_summary(self) -> Dict[str, Any]:
        """
        获取状态摘要（用于调试和日志）
        
        Returns:
            包含关键状态字段的字典
        """
        return {
            "request_id": self.request_id,
            "sequence_number": self.sequence_number,
            "response_id": self.response_id,
            "item_id": self.item_id,
            "has_sent_initial_events": self.has_sent_initial_events,
            "accumulated_text_length": len(self.accumulated_text),
            "has_tool_calls": self.has_tool_calls,
            "model_name": self.model_name,
            "tool_call_count": len(self.tool_call_states),
            "custom_tool_count": len(self.custom_tools_map),
        }
