"""管理台前端单元测试 - 静态壳挂载、鉴权边界、接口契约

前端只是 /admin/* 接口的另一个客户端：
- 静态壳（HTML/JS/CSS）不含敏感数据，允许无密钥加载
- 数据接口仍然一律要求 GATEWAY_API_KEY
- 不进入 lifespan：只验证挂载、路由与字段契约
"""

import os
import re
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from openai_proxy.audit.config import AuditConfig
from openai_proxy.audit.manager import AuditManager
from openai_proxy.audit.query import UsageQueryService
from openai_proxy.audit.record import AuditAttempt
from openai_proxy.audit.store import SqliteAuditStore
from openai_proxy.service import OpenAIProxyService

_KEY = "test-gateway-key"
_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

_CONFIG = (
    'example-platform:\n  baseUrl: "https://api.example.com/v1"\n'
    '  apiKey: "${EXAMPLE_PLATFORM_API_KEY}"\n  models:\n    - "model-a"\n'
)

# 静态检查时不当作「本模块函数」的关键字与浏览器全局
_JS_GLOBALS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "new", "await",
    "do", "else", "let", "const", "var", "of", "in", "yield", "delete", "void", "instanceof",
    "fetch", "encodeURIComponent", "decodeURIComponent", "parseInt", "parseFloat", "isNaN",
    "getComputedStyle", "setTimeout", "setInterval", "clearTimeout", "alert", "confirm", "prompt",
    "structuredClone", "require", "super",
}


def _strip_js_comments(text: str) -> str:
    """去掉 JS 注释：注释里写的 read() / values() 不是调用，会被静态检查误报

    字符串里的 // 不是注释（URL 里全是），所以逐字符扫而不是直接用正则。
    """
    out: list = []
    index, size, quote = 0, len(text), ""
    while index < size:
        char = text[index]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < size:
                out.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = size if end < 0 else end + 2
            out.append("\n")
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = size if end < 0 else end
            continue
        out.append(char)
        index += 1
    return "".join(out)


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    """管理接口默认要求密钥（鉴权边界由本组测试自己覆盖）"""
    monkeypatch.setenv("GATEWAY_API_KEY", _KEY)


def _attempt(key: str, ts: str, model: str, outcome: str) -> AuditAttempt:
    """构造一条已收尾的审计行（示例平台/示例模型）"""
    attempt = AuditAttempt(
        attempt_key=key,
        gateway_request_id=key.rsplit("-", 1)[0],
        endpoint="chat_completions",
        stream=True,
        selector="all",
        platform=model.split("|", 1)[0],
        model_name=model,
        model_id=model.split("|", 1)[-1],
        attempt_seq=int(key.rsplit("-", 1)[1]),
        ts=ts,
    )
    if outcome == "failure":
        attempt.mark_failure(http_status=429)
    elif outcome == "aborted":
        attempt.mark_aborted()
    else:
        attempt.mark_success({
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
            "choices": [{"message": {"content": "ok"}}],
        })
    return attempt


def _client(tmp_path, monkeypatch, **env) -> TestClient:
    """构造一个不跑 lifespan 的客户端：预置配置文件、审计库与备份目录"""
    config_path = tmp_path / "models.yaml"
    config_path.write_text(_CONFIG, encoding="utf-8")

    monkeypatch.setenv("CONFIG_BACKUP_DIR", str(tmp_path / "backups"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    db_path = str(tmp_path / "audit.db")
    store = SqliteAuditStore(db_path)
    store.init_schema()
    store.insert_many([
        _attempt("r1-1", "2026-01-01T10:00:00.000+00:00", "example-platform|model-a", "success").to_row(),
        _attempt("r2-1", "2026-01-01T10:01:00.000+00:00", "example-platform|model-a", "failure").to_row(),
        _attempt("r2-2", "2026-01-01T10:01:05.000+00:00", "example-platform|model-a", "success").to_row(),
    ])

    service = OpenAIProxyService(config_file=str(config_path))
    service.audit = AuditManager(AuditConfig(db_path=db_path))
    service.audit.store = store
    service.audit.query_service = UsageQueryService(store)
    # 不启动后台刷盘任务：本组测试只读
    return TestClient(service.create_app())


def _headers() -> dict:
    return {"Authorization": f"Bearer {_KEY}"}


class TestShell:
    """静态壳"""

    @pytest.mark.unit
    def test_前端壳不需要密钥即可加载(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        response = client.get("/admin/ui/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "管理台" in response.text

    @pytest.mark.unit
    def test_脚本与样式可以加载(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        for path in ("/admin/ui/style.css", "/admin/ui/js/app.js", "/admin/ui/js/api.js",
                     "/admin/ui/js/ui.js", "/admin/ui/js/charts.js"):
            response = client.get(path)
            assert response.status_code == 200, path

    @pytest.mark.unit
    def test_静态壳要求浏览器回源确认版本(self, tmp_path, monkeypatch):
        """前端没有构建步骤：浏览器缓存住旧脚本就会混用新旧代码"""
        client = _client(tmp_path, monkeypatch)

        for path in ("/admin/ui/", "/admin/ui/js/app.js", "/admin/ui/style.css"):
            response = client.get(path)
            assert response.headers.get("cache-control") == "no-cache", path
            # 回源时未变化的文件走 304，不会反复全量下载
            assert "etag" in response.headers, path

    @pytest.mark.unit
    def test_ADMIN_UI_ENABLED为false时不提供前端(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch, ADMIN_UI_ENABLED="false")

        assert client.get("/admin/ui/").status_code == 404

    @pytest.mark.unit
    def test_前端壳不会绕过管理接口的鉴权(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        assert client.get("/admin/audit/stats").status_code == 401
        assert client.get("/admin/audit/stats", headers=_headers()).status_code == 200

    @pytest.mark.unit
    def test_未配置密钥时管理接口仍然403(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        client = _client(tmp_path, monkeypatch)

        assert client.get("/admin/ui/").status_code == 200
        assert client.get("/admin/usage/summary").status_code == 403


class TestContract:
    """前端依赖的字段契约（改动接口时必须同步 web/API.md）"""

    @pytest.mark.unit
    def test_用量接口返回前端使用的字段(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        summary = client.get("/admin/usage/summary", headers=_headers()).json()

        assert {"group_by", "items", "count", "range"} <= set(summary)
        for key in ("bucket", "requests", "attempts", "success_rate", "failover_count",
                    "p95_duration_ms", "avg_ttft_ms", "avg_throughput", "output_tokens"):
            assert key in summary["items"][0]

    @pytest.mark.unit
    def test_时序接口返回按桶排序的点(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        series = client.get("/admin/usage/timeseries", params={"bucket": "day"}, headers=_headers()).json()

        assert {"bucket", "points", "range"} <= set(series)
        assert [point["bucket"] for point in series["points"]] == sorted(p["bucket"] for p in series["points"])
        assert "p95_duration_ms" in series["points"][0]

    @pytest.mark.unit
    def test_审计明细与链路接口返回尝试行(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        attempts = client.get("/admin/audit", headers=_headers()).json()
        assert {"total", "items"} <= set(attempts)
        for key in ("attempt_key", "gateway_request_id", "attempt_seq", "outcome",
                    "duration_ms", "error_category", "http_status", "finalized"):
            assert key in attempts["items"][0]

        chain = client.get("/admin/audit/chain/r2", headers=_headers()).json()
        assert {"gateway_request_id", "attempts"} <= set(chain)
        assert [item["attempt_seq"] for item in chain["attempts"]] == [1, 2]

        missing = client.get("/admin/audit/chain/ghost", headers=_headers()).json()
        assert missing["attempts"] == []

    @pytest.mark.unit
    def test_模型榜与审计健康接口可用(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        ranking = client.get("/admin/usage/models", params={"sort": "p95"}, headers=_headers()).json()
        assert ranking["sort"] == "p95" and ranking["items"]

        body = client.get("/admin/audit/stats", headers=_headers()).json()
        assert {"config", "collector", "store"} <= set(body)
        assert body["config"]["db_path"]
        assert body["store"]["rows"] == 3

    @pytest.mark.unit
    def test_配置接口返回脱敏视图与补丁能力(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EXAMPLE_PLATFORM_API_KEY", "super-secret-value")
        client = _client(tmp_path, monkeypatch)

        document = client.get("/admin/config/document", headers=_headers()).json()

        assert "super-secret-value" not in str(document)
        assert {"path", "version", "platforms", "aliases", "audit", "editable", "errors", "warnings"} <= set(document)
        assert document["editable"]["ops"]

        schema = client.get("/admin/config/schema", headers=_headers()).json()
        assert {"ops", "platform_fields", "model_fields", "audit_fields", "limits"} <= set(schema)

    @pytest.mark.unit
    def test_plan与apply返回前端要展示的字段(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        ops = [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 4}]

        plan = client.post("/admin/config/plan", headers=_headers(), json={"ops": ops}).json()
        assert {"changes", "diff", "errors", "warnings", "valid", "needs_restart"} <= set(plan)
        assert plan["valid"] is True

        applied = client.post("/admin/config/apply", headers=_headers(), json={"ops": ops, "note": "界面调权重"}).json()
        assert {"backup", "reloaded", "needs_restart", "version_after"} <= set(applied)

        backups = client.get("/admin/config/backups", headers=_headers()).json()
        assert {"name", "created_at", "bytes", "version", "note"} <= set(backups["items"][0])
        assert "界面调权重" in backups["items"][0]["note"]

    @pytest.mark.unit
    def test_版本冲突时返回409并带回当前版本(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        response = client.post("/admin/config/plan", headers=_headers(), json={
            "ops": [{"op": "set_platform_field", "platform": "example-platform", "field": "weight", "value": 4}],
            "version": "0" * 64,
        })

        assert response.status_code == 409
        assert response.json()["detail"]["actual"]

    @pytest.mark.unit
    def test_前端字段清单与文档一致(self):
        """web/API.md 是前后端的契约文件，接口新增了字段就必须同步文档"""
        path = os.path.join(_WEB, "API.md")
        if not os.path.exists(path):
            pytest.fail("前端契约文档缺失: web/API.md")

        doc = open(path, "r", encoding="utf-8").read()
        for endpoint in ("/admin/usage/summary", "/admin/usage/timeseries", "/admin/usage/models",
                         "/admin/audit", "/admin/audit/chain", "/admin/audit/stats",
                         "/admin/config/document", "/admin/config/plan", "/admin/config/apply",
                         "/admin/config/backups", "/admin/config/rollback", "/admin/config/reload",
                         "/admin/config/models"):
            assert endpoint in doc, f"契约文档缺少 {endpoint}"


class TestSourceHygiene:
    """前端源码静态自检：不需要浏览器，专门抓「只有运行期才炸」的那类笔误"""

    @staticmethod
    def _js_sources() -> Dict[str, str]:
        js_dir = os.path.join(_WEB, "js")
        if not os.path.isdir(js_dir):
            pytest.fail("前端脚本目录缺失: web/js")
        return {name: open(os.path.join(js_dir, name), encoding="utf-8").read()
                for name in sorted(os.listdir(js_dir)) if name.endswith(".js")}

    @pytest.mark.unit
    def test_dom接口只接受元素(self):
        """getComputedStyle 一类 DOM 接口只吃元素：曾经把 canvas 的 2d 上下文传进去"""
        offenders = []
        for name, text in self._js_sources().items():
            for argument in re.findall(r"getComputedStyle\(([^)]*)\)", text):
                if ".ctx" in argument or "box" in argument:
                    offenders.append(f"{name}: getComputedStyle({argument})")

        assert not offenders, f"DOM 接口收到了非元素参数: {'; '.join(offenders)}"

    @pytest.mark.unit
    def test_跨模块import的每个名字都有导出(self):
        """import 了一个不存在的导出时浏览器只在运行期报 ReferenceError"""
        sources = self._js_sources()
        exported = {
            name: {match.group(1) for match in re.finditer(
                r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z0-9_$]+)", text)}
            for name, text in sources.items()
        }

        missing = []
        for name, text in sources.items():
            for names, target in re.findall(r"import\s*\{([^}]*)\}\s*from\s*\"\./([^\"/]+\.js)\"", text):
                if target not in sources:
                    missing.append(f"{name} 引用了不存在的模块 ./{target}")
                    continue
                for symbol in names.split(","):
                    symbol = symbol.split(" as ")[0].strip()
                    if symbol and symbol not in exported[target]:
                        missing.append(f"{name} 引用了 ./{target} 里没有的导出: {symbol}")

        assert not missing, "; ".join(missing)

    @pytest.mark.unit
    def test_界面覆盖了全部补丁操作(self):
        """后端新增一种 op 时，界面也必须能用它（避免只剩 CLI 能改的情况）"""
        from openai_proxy.config_store.editor import OPS

        text = open(os.path.join(_WEB, "js", "config_fields.js"), encoding="utf-8").read()
        missing = [op for op in OPS if f'"{op}"' not in text]
        assert not missing, f"界面没有提供这些补丁操作: {', '.join(missing)}"

    @pytest.mark.unit
    def test_别名与新增入口都在界面上(self):
        """别名和新增模型/别名最容易「其实有但找不到」，锁住这些控件"""
        fields = open(os.path.join(_WEB, "js", "config_fields.js"), encoding="utf-8").read()
        view = open(os.path.join(_WEB, "js", "view_config.js"), encoding="utf-8").read()

        assert '"新增别名"' in fields, "界面上没有新增别名的入口"
        assert '"新增模型条目"' in fields, "界面上没有新增模型条目的入口"
        assert fields.count("collapsible(") >= 4, "平台/别名/审计/新增平台应当都是可折叠区块"
        assert '"全部展开"' in view and '"全部折叠"' in view, "配置视图缺少批量折叠控件"

    @pytest.mark.unit
    def test_视图模块之间没有互相调用不存在的函数(self):
        """视图里调用本模块函数前必须先定义（曾经调过已经不存在的 renderTail）"""
        for name, raw in self._js_sources().items():
            # 注释里的 read() / values() 不是调用：先剥掉注释再扫
            text = _strip_js_comments(raw)
            defined = {match.group(1) for match in re.finditer(r"function\s+([A-Za-z0-9_$]+)\s*\(", text)}
            defined |= {match.group(1) for match in re.finditer(r"const\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\(", text)}
            # 类里的方法定义（constructor / get / post 等）也算已定义
            defined |= {match.group(1) for match in re.finditer(
                r"^\s*(?:static\s+)?(?:async\s+)?([A-Za-z0-9_$]+)\([^()]*\)\s*\{", text, re.MULTILINE)}

            # 函数/方法/箭头函数的形参：回调类形参（stage、buildOp）也是本模块可用的名字
            params: set = set()
            signatures = [
                *re.findall(r"function\s+[A-Za-z0-9_$]+\(([^)]*)\)", text),
                *re.findall(r"^\s*(?:static\s+)?(?:async\s+)?[A-Za-z0-9_$]+\(([^()]*)\)\s*\{", text, re.MULTILINE),
                *re.findall(r"\(([^()]*)\)\s*=>", text),
            ]
            for signature in signatures:
                params |= {match.group(0) for match in re.finditer(r"[A-Za-z_$][A-Za-z0-9_$]*", signature)}

            # 对象解构出来的局部名（例如 options 里传进来的 buildOp）同样是本模块可用的名字
            # 非贪婪到「} =」为止：默认值里的 {} / [] 不能把匹配截断在半截
            # 每段只取等号左边的名字，默认值里出现的其它名字（undefined 之类）不算已定义
            for names in re.findall(r"(?:const|let|var)\s*\{([\s\S]*?)\}\s*=", text):
                params |= set(re.findall(r"(?:^|,)\s*([A-Za-z_$][A-Za-z0-9_$]*)", names))

            imported = set()
            for names in re.findall(r"import\s*\{([^}]*)\}\s*from", text):
                for symbol in names.split(","):
                    symbol = symbol.split(" as ")[-1].strip()
                    if symbol:
                        imported.add(symbol)

            called = {match.group(1) for match in re.finditer(r"(?<![.\w])([a-z][A-Za-z0-9_$]*)\(", text)}
            unknown = called - defined - imported - params - _JS_GLOBALS
            assert not unknown, f"{name} 调用了本模块里不存在的函数: {', '.join(sorted(unknown))}"

    @pytest.mark.unit
    def test_写进输入框的字面量不含非ASCII(self):
        """输入框里的文字可能被当成请求头发出去：非 ASCII 字符会让浏览器在 fetch 里直接抛类型错误"""
        offenders = []
        for name, text in self._js_sources().items():
            for match in re.finditer(r"\.value\s*=\s*([\"'`])(.*?)\1", text):
                literal = match.group(2)
                if any(ord(char) > 0x7E or ord(char) < 0x20 for char in literal):
                    offenders.append(f"{name}: .value = {literal!r}")

        assert not offenders, f"HTTP 请求头装不下这些字面量: {'; '.join(offenders)}"

    @pytest.mark.unit
    def test_密钥在进入请求头之前先校验(self):
        """密钥不合法时要在页面上说明原因，而不是让浏览器抛看不懂的 ISO-8859-1 错误"""
        sources = self._js_sources()
        api, app = sources["api.js"], sources["app.js"]

        assert re.search(r"export function normalizeKey", api), "api.js 缺少校验密钥的入口"
        assert "HEADER_UNSAFE" in api, "api.js 没有约束请求头能装下的字符范围"
        assert re.search(r"setKey\([^)]*\)\s*\{[^}]*normalizeKey", api), "Api.setKey 没有校验密钥"
        assert re.search(r"readStoredKey", api), "api.js 没有清理 sessionStorage 里残留的非法密钥"
        assert re.search(r"try\s*\{[^{}]*setKey\(", app), "app.js 没有拦住不合法的密钥"
        assert "storageProblem" in app, "残留的非法密钥没有在页面上告知用户"

    @pytest.mark.unit
    def test_配置页把字段字典与字段说明渲染出来(self):
        """「页面即文档」的前提：说明一律来自后端字典，界面不自己编文案"""
        fields = open(os.path.join(_WEB, "js", "config_fields.js"), encoding="utf-8").read()
        view = open(os.path.join(_WEB, "js", "view_config.js"), encoding="utf-8").read()
        css = open(os.path.join(_WEB, "style.css"), encoding="utf-8").read()

        assert "export function fieldDictionary" in fields, "config_fields.js 里没有字段字典区块"
        assert "fieldDictionary(" in view, "配置视图没有渲染字段字典"
        assert "docs: schema.docs" in view, "配置视图没有把后端的字段字典传给表单"
        assert "docNodes(" in fields, "编辑块里没有挂当前字段的说明"
        # 字典按作用域分块、卡片式排版：六列大表在窄屏会横向溢出，展开后长到看不清
        assert "docGrid(" in fields, "字段字典不是卡片网格，窄屏会挤成一坨"
        assert "collapsible(\"配置结构说明\"" in fields, "配置结构说明没有单独折叠成一个区块"
        # 一条说得清的说明要有：默认值、取值/单位、写法示例、生效方式
        for keyword in ("默认：", "写法 ", "生效"):
            assert keyword in fields, f"字段说明的组成部分没出现在界面上: {keyword}"
        for css_class in ("editor-block", "field-doc", "doc-note", ".doc-grid", ".doc-card", "guide-body"):
            assert css_class in css, f"style.css 里没有 {css_class} 的样式：字典会渲染成一堆没样式的文本"
        # 布局依赖这三条：键值对齐、控件不带 fieldset 边框、配置页表格可换行
        for layout in ("dl.kv", ".control", "table.wrap"):
            assert layout in css, f"style.css 里没有 {layout}：配置页的排版会散开"
        # 「选择模型」是小标签在上方的控件，同行按钮要按底边对齐才竖着对上
        assert "control-row" in fields and ".control-row" in css, "模型条目的选择行没有按底边对齐"

    @pytest.mark.unit
    def test_取值优先用选项而不是输入框(self):
        """输入框会把长取值截断看不全：能列候选的字段必须走下拉或标签"""
        sources = self._js_sources()
        fields, view = sources["config_fields.js"], sources["view_config.js"]
        controls = sources.get("option_controls.js")
        css = open(os.path.join(_WEB, "style.css"), encoding="utf-8").read()

        assert controls, "缺少 web/js/option_controls.js：取值控件没有统一的地方"
        assert "export function valueControl" in controls, "没有按字段类型挑控件的入口"
        assert "export function optionPicker" in controls, "没有「已选用标签、新增从下拉挑」的控件"
        assert 'type: "number"' in controls, "数值字段没有用带边界的 number 输入框"
        assert "min: limit" in controls and "max: limit" in controls, "数字输入框没有带上后端的取值范围"

        assert "valueControl(" in fields, "编辑行没有走统一的取值控件"
        assert "enums: editables.enums" in fields and "enums: schema.enums" in view, "后端的取值选项没传到表单"

        # 新增模型条目：只有模型名是输入框，覆盖走「字段下拉 + 取值控件 + 已选标签」
        assert "overridesPicker(" in fields, "新增模型条目没有用选项挑覆盖"
        assert "export function overridesPicker" in controls, "覆盖挑选器没有和取值控件放在一起"
        assert '"加一项覆盖"' in controls, "覆盖没有逐项加入的入口"
        assert "可选覆盖：" not in fields, "新增模型条目还留着自由文本的覆盖输入框"
        assert "parseOverrides" not in fields, "还在用 `weight=5, timeout=30` 那种要照抄文档的写法"
        # 页面上没有任何「手填补充」入口：连插件灌进来的模型也有可勾的候选
        assert "补充手填" not in fields and "extra.hidden" not in fields, "别名行还留着手填入口"
        assert "splitList" not in fields and "splitList" not in controls, "列表型字段还留着逗号手填入口"
        # 新增平台的模型条目：一次填一个（标签可删），不再让人写 `model-a, model-b` 这种容易写错的列表
        assert "chipsInput(" in fields, "新增平台的模型条目没有用逐项加入的控件"
        assert "model-a, model-b" not in fields, "新增平台还在让人一行写一串模型名"
        assert '"加一项"' in controls, "逐项加入的列表控件没有加入口"
        # 别名指向本来就枚举得出来：必须从「all / 平台 / 平台|模型」里挑
        assert "selectorOptions(" in fields, "别名指向没有可枚举的候选"
        assert fields.count("selectorEditor(") >= 2, "已有别名行或新增别名行没有用选项控件"
        assert "textInput((alias.selectors" not in fields, "已有别名行又退回成逗号字符串输入框"
        for label in ("整组：all", "整个平台：", "单个模型："):
            assert label in fields, f"别名指向的下拉里没有给出这一类写法: {label}"
        # 屏蔽模型的候选来自这个平台现有的条目，不是让人凭记忆写模型名
        assert "availableFor:" in fields and "modelOptions(" in fields, "屏蔽模型没有可勾选的候选"
        for css_class in (".value-slot", ".option-picker", ".option-chips", ".option-actions", ".option-chip", "input.num", ".model-add", ".overrides-picker", ".chips-input", ".form-grid", ".option-select", ".option-filter"):
            assert css_class in css, f"style.css 里没有 {css_class} 的样式：选项控件会散开"
        # 别名行在窄屏要整行铺开指向，否则标签与下拉会挤成一列、按钮掉到最下面
        assert "max-width: 720px" in css and ".alias-row > .option-picker" in css, "窄屏的别名行没有为选项控件让出整行"

    @pytest.mark.unit
    def test_候选很多时给筛选框而不是手填输入框(self):
        """爬虫平台的模型是插件灌进来的，动辄上百个：下拉要能筛选并封顶，不能退回成输入框"""
        sources = self._js_sources()
        fields, view, controls = sources["config_fields.js"], sources["view_config.js"], sources["option_controls.js"]
        css = open(os.path.join(_WEB, "style.css"), encoding="utf-8").read()
        api = open(os.path.join(_WEB, "API.md"), encoding="utf-8").read()

        # 候选超过阈值才给筛选框；已经选中的取值不会被筛选框筛掉
        assert "FILTER_AT" in controls and "MAX_OPTIONS" in controls, "候选没有数量阈值与下拉上限，下拉会长到看不见头"
        assert "option-filter" in controls and ".option-filter" in css, "候选很多时没有筛选框"
        assert "用筛选框缩小范围" in controls, "下拉被截断时没有告诉用户还剩多少候选"
        assert "!chosen.includes(item.value)" in controls, "候选列表没有排除已经选中的取值"
        # 筛选框留在外面、下拉重建：否则一边打字一边失去焦点
        assert "option-select" in controls and ".option-select" in css, "下拉重建会把筛选框一起挪走"

        # 运行时清单：爬虫平台的模型不在 models.yaml 里，候选只能问进程
        assert '"/admin/config/models"' in view, "界面没有拉运行时模型清单"
        assert '"/admin/config/models").catch(' in view, "运行时清单读不到时应该只少候选，而不是整个配置页失败"
        assert "runtimeNamesOf(" in fields and "runtimeSelectors(" in fields, "运行时模型没有进到候选里"
        assert "插件灌进来的模型：" in fields, "别名指向的下拉里没有运行时模型这一类候选"
        assert "LIST_NOTE" in fields and "只能勾这里给出的模型" in fields, "通配模式挑不出来这件事没有写在界面上"
        assert "/admin/config/models" in api, "web/API.md 没有写明运行时模型清单这个契约"

    @pytest.mark.unit
    def test_差异实时显示并按新增修改删除着色(self):
        """改动一进清单就自动预演：差异按新增绿 / 修改黄 / 删除红显示，每一行还要纵向对齐"""
        sources = self._js_sources()
        view, diff = sources["view_config.js"], sources.get("diff_view.js")
        css = open(os.path.join(_WEB, "style.css"), encoding="utf-8").read()
        api = open(os.path.join(_WEB, "API.md"), encoding="utf-8").read()

        assert diff, "缺少 web/js/diff_view.js：差异的排版没有统一的地方"

        # 实时：清单一变就排一次预演（只读、不写盘），不靠人点按钮
        assert "scheduleLivePlan()" in view and "LIVE_PLAN_DELAY_MS" in view, "改动之后没有自动预演"
        assert "实时差异" in view, "差异区没有说明这是实时的预演结果"
        assert "请先预演" not in view, "写盘前还要求人手动点一次预演才能继续"
        # 预演的必须正是将要写盘的那批改动：清单在预演之后又变过就要重算
        assert "planFor" in view and "opsSignature" in view, "写盘前没有核对预演是否对应当前清单"

        # 三类差异都要有类别（决定颜色），颜色只写在 CSS 里，不在 JS 里拼颜色
        assert "diff_rows" in diff, "没有用后端按类别拆开的行级差异 diff_rows"
        assert "align-row" in diff, "差异行没有带上决定颜色的类别类名"
        for kind in ("add", "modify", "remove"):
            assert f".align-row.{kind}" in css, f"style.css 里没有 .align-row.{kind} 的颜色"
        for variable in ("--add-bg", "--mod-bg", "--del-bg"):
            assert variable in css, f"style.css 缺少 {variable}：差异行没有底色"
        assert "diff-legend" in diff and ".diff-legend" in css, "颜色口径没有写在页面上"

        # 对齐：待提交清单与两张差异表共用同一套列宽，窄屏也不能把列挪位
        assert "align-grid" in diff and "align-grid" in css, "清单与差异表没有共用同一套栅格"
        assert ".align-row" in css and "grid-template-columns" in css, ".align-row 不是栅格，行不会纵向对齐"
        for cell in ("cell-kind", "cell-path", "cell-old", "cell-new"):
            assert f".{cell}" in css, f"style.css 里没有 .{cell} 的列定义"
        narrow = css.split("@media (max-width: 720px)")[-1]
        assert ".cell-old" in narrow and ".cell-new" in narrow, "窄屏没有显式指定原值/新值所在的列，行会歪"

        # 旧的整段 pre 差异（只按 +/- 上色、行不对齐、要人手动预演才看得到）已经换掉
        assert "diffBlock" not in sources["ui.js"], "ui.js 还留着旧的 diffBlock"
        assert "diff-line-add" not in css, "旧的 diff 行样式还留着"
        assert "diff_rows" in api, "web/API.md 没有写明 diff_rows 这个契约"

    @pytest.mark.unit
    def test_窄屏表格只在自己的区域里横向平移(self):
        """列多的表在窄屏不能把整页撑出横向滚动条：表格套 .table-scroll，并且给一句平移提示"""
        sources = self._js_sources()
        css = open(os.path.join(_WEB, "style.css"), encoding="utf-8").read()

        # 所有表格都由 table() 套上滚动容器（配置页自建的表也一样）
        assert "table-scroll" in sources["ui.js"], "table() 没有给表格套可横向平移的容器"
        assert "table-scroll" in sources["config_fields.js"], "模型条目表没有套滚动容器"
        assert ".table-scroll" in css and "overflow-x: auto" in css, "style.css 里没有 .table-scroll 的横向平移"

        # 提示默认不出现，只在窄屏出现（默认规则必须写在窄屏规则之前，否则会把窄屏规则覆盖掉）
        assert "table-hint" in sources["ui.js"], "宽表没有给「可以左右拖动」的提示"
        assert ".table-hint { display: none; }" in css, "宽屏也会显示平移提示"
        narrow = "\n".join(re.findall(r"@media \(max-width: 720px\) \{(.*?)\n\}", css, re.S))
        assert ".table-hint" in narrow, "窄屏没有显示平移提示"
        # 窄屏允许换行，但列不能被压成一个字符宽（中文列没有空格可断）
        assert "white-space: normal" in narrow and "min-width" in narrow, "窄屏的列会被压成一个字符宽"

        chip = re.search(r"\.chip \{[^}]*\}", css)
        assert chip and "white-space: nowrap" in chip.group(0), "状态标签会在窄屏被竖着拆成一个字一行"
