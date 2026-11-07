"""透传转发 - 把任意第三方接口按前缀原样转发，并复用审计与用量统计"""

from openai_proxy.passthrough.config import PassthroughRoute, parse_routes
from openai_proxy.passthrough.manager import PassthroughManager

__all__ = ["PassthroughRoute", "PassthroughManager", "parse_routes"]
