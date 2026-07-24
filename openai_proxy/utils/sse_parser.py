"""
SSE Event Parser - 统一的 SSE 事件解析和缓冲管理工具类

提供跨 chunk 的事件拼接和标准化功能,消除 service.py 中重复的 SSE 缓冲区管理逻辑。

设计动机:
- 在重构前,/v1/chat/completions 和 /v1/responses 两个端点各自实现了几乎相同的
  SSE 事件缓冲区管理逻辑(约 80% 代码重复)
- 将此逻辑提取为独立工具类后,消除了重复代码,提高了可维护性和可测试性
- 支持事件格式标准化,统一 event: 和 data: 行的格式

字节级缓冲:
- 内部使用 bytes 缓冲区,避免多字节 UTF-8 字符被分拆到两个 chunk 时产生乱码
- feed() 接收 bytes,按 b'\\n\\n' 分割出完整事件后统一解码

使用示例:
    parser = SSEEventParser(normalize=True)
    for chunk in stream:        # chunk: bytes
        events = parser.feed(chunk)
        for event in events:
            process_event(event)
"""
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class SSEEventParser:
    """
    SSE 事件解析器

    负责从流式响应中提取完整的 SSE 事件,支持:
    - 跨 chunk 的事件拼接(处理不完整的事件)
    - 事件格式标准化(统一 event: 和 data: 行格式)
    - 空事件过滤
    - 字节级缓冲防止 UTF-8 截断乱码

    与旧实现相比的优势:
    - 封装了缓冲区管理逻辑,调用方无需关心事件边界处理
    - 返回完整事件列表而非逐个 yield,简化调用方逻辑
    - 支持可选的标准化处理,提高事件格式一致性
    - 使用 bytes 缓冲区避免跨 chunk 的 UTF-8 字符被截断

    使用示例:
        parser = SSEEventParser(normalize=True)
        for chunk in stream:
            events = parser.feed(chunk)
            for event in events:
                process_event(event)
    """

    def __init__(self, normalize: bool = True):
        """
        初始化 SSE 事件解析器

        Args:
            normalize: 是否对事件进行标准化处理（统一 event/data 行格式）
        """
        self.buffer = b""
        self.normalize = normalize

    def feed(self, chunk: bytes) -> List[str]:
        """
        输入数据块并返回完整的事件列表

        该方法会将新数据以 bytes 形式追加到内部缓冲区，然后按 b'\\n\\n'
        分割出完整的事件。只有完整的事件才会被解码为 str，避免了多字节
        UTF-8 字符被分拆到两个 chunk 时产生乱码。

        如果最后一个事件不完整（缓冲区不以 b'\\n\\n' 结尾），则保留在
        缓冲区中等待下一个 chunk。

        Args:
            chunk: 原始数据块（bytes，来自上游 HTTP 响应）

        Returns:
            完整事件的列表（已标准化，如果启用 normalize）
        """
        if isinstance(chunk, str):
            chunk = chunk.encode('utf-8')

        # 将新数据追加到缓冲区
        self.buffer += chunk

        # 按双换行符分割事件
        parts = self.buffer.split(b'\n\n')

        # 判断最后一个事件是否完整
        if self.buffer.endswith(b'\n\n'):
            complete_events_bytes = parts
            self.buffer = b""
        else:
            complete_events_bytes = parts[:-1]
            self.buffer = parts[-1]

        # 处理完整事件
        result = []
        for event_bytes in complete_events_bytes:
            event_bytes = event_bytes.strip()
            if not event_bytes:
                continue

            try:
                event = event_bytes.decode('utf-8')
            except UnicodeDecodeError:
                event = event_bytes.decode('utf-8', errors='replace')
                logger.warning("Decoded SSE event with replacement characters due to invalid UTF-8")

            # 如果需要，进行标准化
            if self.normalize:
                event = self._standardize_event(event)

            result.append(event)

        return result

    def _standardize_event(self, event: str) -> str:
        """
        标准化 SSE 事件格式

        确保事件中的每一行都遵循标准格式：
        - event: <event_name>
        - data: <json_data>

        处理常见的格式问题：
        - 缺少冒号后的空格
        - 多余的空格
        - 不一致的大小写

        Args:
            event: 原始事件字符串

        Returns:
            标准化后的事件字符串
        """
        lines = event.split('\n')
        standardized_lines = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 标准化 event: 行
            if line.lower().startswith('event:'):
                event_name = line[6:].strip()
                standardized_lines.append(f"event: {event_name}")
            # 标准化 data: 行
            elif line.lower().startswith('data:'):
                data_content = line[5:].strip()
                standardized_lines.append(f"data: {data_content}")
            # 其他行（如 id:、retry: 等）保持不变
            else:
                standardized_lines.append(line)

        return '\n'.join(standardized_lines)

    def reset(self):
        """重置解析器状态，清空缓冲区"""
        self.buffer = b""

    def get_buffer_size(self) -> int:
        """获取当前缓冲区大小（字节数）"""
        return len(self.buffer)
