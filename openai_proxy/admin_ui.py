"""管理台前端的静态文件挂载

前端是一组无构建的 ES 模块：浏览器缓存住旧的 js 文件时，用户看到的还是修复前的界面
（新旧脚本混在一起更容易出现看不懂的报错）。这里统一要求浏览器用之前先回源确认版本。
"""

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response


class AdminUiStatic(StaticFiles):
    """带 Cache-Control 的管理台静态文件目录

    no-cache 不等于「不缓存」，而是「可以缓存，但用之前必须回源确认没过期」：
    静态文件自带 ETag，未变化的文件仍然只走 304，不会反复全量下载。
    """

    cache_control = "no-cache"

    async def get_response(self, path: str, scope) -> Response:
        """复用 StaticFiles 的取文件与 304 逻辑，只补一个响应头"""
        response = await super().get_response(path, scope)
        # 只影响 /admin/ui 下的静态壳：/admin/* 数据接口本来就没有缓存语义
        response.headers["Cache-Control"] = self.cache_control
        return response
