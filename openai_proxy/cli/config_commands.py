"""
配置类命令 - 与 /admin/config 接口共用同一套补丁

命令只负责把参数翻译成补丁操作，校验、备份、写盘、重载全部交给
ConfigCommandService，因此界面上能做的改动命令行也一定能做，反之亦然。
"""
from typing import Any, Dict, List, Optional

from openai_proxy.cli.codes import EXIT_CONFLICT, EXIT_ERROR, EXIT_OK
from openai_proxy.cli.output import Console
from openai_proxy.cli.parser import parse_field_values, parse_overrides
from openai_proxy.cli.usage_commands import AuditAccess
from openai_proxy.config_store.service import ConfigConflict, ConfigInvalid
from openai_proxy.config_store.writer import ConfigWriteError

# 新增平台时可以从命令行传进来的字段：命令行参数名 -> 配置文件字段名
_PLATFORM_ADD_FIELDS = {
    "weight": "weight",
    "timeout": "timeout",
    "retry_count": "retry_count",
    "connect_timeout": "connect_timeout",
    "first_chunk_timeout": "first_chunk_timeout",
    "stream_probe_timeout": "stream_probe_timeout",
}


# 字段字典的分组：与 /admin/config/schema 的 docs 作用域一一对应
_DOC_SCOPES = (
    ("platform", "平台级字段"),
    ("model", "模型级可覆盖字段（写在模型条目里，覆盖平台级取值）"),
    ("audit", "audit 段字段（这一段每个都要重启）"),
)


class ConfigCommands:
    """config / platform / model / alias / audit 各动作"""

    def __init__(self, service, console: Console):
        self.service = service
        self.console = console

    # ---------- 读 ----------

    def config_document(self, args) -> int:
        view = self.service.view()
        platforms = [item for item in view["platforms"] if not args.platform or item["name"] == args.platform]

        if not platforms:
            self.console.line(f"配置里没有平台 {args.platform}", to_err=True)
            return EXIT_ERROR

        if self.console.as_json:
            self.console.emit({"version": view["version"], "platforms": platforms, "aliases": view["aliases"], "audit": view["audit"]})
            return EXIT_OK

        self.console.line(f"配置文件: {view['path']}  版本: {view['version'][:12]}  大小: {view['bytes']} 字节")
        self._platform_table(platforms)
        self._alias_table(view["aliases"])
        self._audit_line(view["audit"])
        self._env_line(view["platforms"])
        return EXIT_OK

    def config_show(self, args) -> int:
        view = self.service.view()
        platform = next((item for item in view["platforms"] if item["name"] == args.platform), None)

        if platform is None:
            self.console.line(f"配置里没有平台 {args.platform}", to_err=True)
            return EXIT_ERROR

        if self.console.as_json:
            self.console.emit(platform)
            return EXIT_OK

        simple = {
            "baseUrl": platform["baseUrl"],
            "apiKey": _api_key_line(platform["apiKey"]),
            "weight": platform["weight"],
            "timeout": platform["timeout"],
            "retry_count": platform["retry_count"],
            "connect_timeout": platform["connect_timeout"],
            "first_chunk_timeout": platform["first_chunk_timeout"],
            "stream_probe_timeout": platform["stream_probe_timeout"],
            "enable_tool_call_conversion": platform["enable_tool_call_conversion"],
            "enabled": platform["enabled"],
            "blacklist": platform["blacklist"],
        }
        self.console.block(f"平台 {args.platform}", [f"{key}: {_brief(value)}" for key, value in simple.items()])

        self.console.line("模型条目:")
        for model in platform["models"]:
            suffix = f"  覆盖: {_brief(model['overrides'])}" if model["overrides"] else ""
            self.console.line(f"  {model['selector']}{suffix}")

        if platform["plugin"]:
            plugin = platform["plugin"]
            self.console.line(f"插件: {plugin['code']}（爬虫模式: {'是' if plugin['scraper'] else '否'}）")
            self.console.line(f"  参数名: {', '.join(plugin['args']) or '（无）'}")
            self.console.line("  注意：爬虫平台的模型由插件灌入，只能用 blacklist 屏蔽")

        return EXIT_OK

    def config_fields(self, args) -> int:
        """字段字典与结构说明：命令行看到的与管理台「模型配置」页完全同一份"""
        from openai_proxy.config_store.routes import schema_payload

        payload = schema_payload()
        if self.console.as_json:
            self.console.json({
                "docs": payload["docs"], "sections": payload["sections"], "limits": payload["limits"],
                # 取值选项也一起给：命令行与「模型配置」页必须看到同一份元数据
                "enums": payload["enums"],
            })
            return EXIT_OK

        scopes = [item for item in _DOC_SCOPES if not args.scope or args.scope == item[0]]
        for scope, title in scopes:
            docs = payload["docs"][scope]
            self.console.line(f"{title}（{len(docs)} 个）")
            for field, doc in docs.items():
                self._field_doc(field, doc, payload["limits"].get(field))
            self.console.line("")

        self.console.line("结构说明")
        for section in payload["sections"]:
            self.console.line(f"  {section['title']}")
            for line in section["body"].splitlines():
                self.console.line(f"    {line}")
            self.console.line("")

        self.console.line("说明来源：openai_proxy/config_store/field_docs.py 与 config_guide.py")
        return EXIT_OK

    def _field_doc(self, field: str, doc: Dict[str, Any], limit: Optional[Dict[str, Any]]) -> None:
        """一个字段说得清的说明：语义、默认值、取值、写法示例、注意点、改完怎么生效"""
        bounds = f" ｜ 边界：{limit['min']} ~ {limit['max']}" if limit else ""
        self.console.line(f"  {field} — {doc['label']}（改完 {doc['effect']}生效）")
        self.console.line(f"    {doc['summary']}")
        self.console.line(f"    默认：{doc['default']} ｜ 取值：{doc['unit']}{bounds}")
        # 有限取值：把界面下拉里能选的那几个写法照抄出来，不用猜
        values = list(doc.get("values") or ())
        if values:
            none_note = "（可以不配置，删掉这一行回落到上层）" if doc.get("allow_none") else ""
            self.console.line(f"    可选项：{' / '.join(values)}{none_note}")
        for line in str(doc["example"]).splitlines():
            self.console.line(f"    写法 {line.strip()}")
        if doc["note"]:
            self.console.line(f"    注意：{doc['note']}")

    def config_validate(self, args) -> int:
        result = self.service.validate()

        if self.console.as_json:
            self.console.emit(result)
        elif result["valid"]:
            self.console.line("配置文件校验通过")
        else:
            self.console.line("配置文件有问题：")
        self.console.issues(result["errors"], result["warnings"])
        return EXIT_OK if result["valid"] else EXIT_ERROR

    def config_backups(self, args) -> int:
        items = self.service.backups()["items"]

        if self.console.as_json:
            self.console.emit({"items": items})
            return EXIT_OK
        if not items:
            self.console.line("还没有备份：每次写盘前都会自动备份当前文件")
            return EXIT_OK

        self.console.table(items, [
            ("name", "备份文件"),
            ("created_at", "时间"),
            ("bytes", "字节"),
            ("version", "版本"),
            ("note", "备注"),
        ])
        self.console.line("回滚: manage.py config rollback <备份文件>")
        return EXIT_OK

    def config_rollback(self, args) -> int:
        """回滚到指定备份（当前内容也会先备份，所以回滚本身也能再回滚）"""
        try:
            result = self.service.rollback(args.name, reload=not args.no_reload)
        except ConfigWriteError as exc:
            self.console.line(f"回滚失败: {exc.message}", to_err=True)
            return EXIT_ERROR

        if self.console.as_json:
            self.console.emit(result)
            return EXIT_OK

        self.console.line(f"已回滚到备份: {result['rolled_back_to']}")
        self.console.line(f"回滚后的版本: {result['version_after'][:12]}")
        self.console.line(f"回滚前的内容也备份在: {result['backup']}（想撤销这次回滚就回滚到它）")
        return EXIT_OK

    # ---------- 写 ----------

    def platform_set(self, args) -> int:
        return self._write(args, [{
            "op": "set_platform_field",
            "platform": args.platform,
            "field": args.field,
            "value": parse_field_values(args),
        }])

    def platform_unset(self, args) -> int:
        return self._write(args, [{
            "op": "unset_platform_field",
            "platform": args.platform,
            "field": args.field,
        }])

    def platform_add(self, args) -> int:
        config: Dict[str, Any] = {
            "baseUrl": args.base_url,
            "models": list(args.model),
        }
        if args.api_key:
            config["apiKey"] = args.api_key
        for argument, field in _PLATFORM_ADD_FIELDS.items():
            value = getattr(args, argument)
            if value is not None:
                config[field] = value
        if args.blacklist:
            config["blacklist"] = list(args.blacklist)
        if args.disabled:
            config["enabled"] = False

        return self._write(args, [{"op": "add_platform", "platform": args.platform, "config": config}])

    def platform_remove(self, args) -> int:
        return self._write(args, [{"op": "remove_platform", "platform": args.platform}])

    def model_add(self, args) -> int:
        return self._write(args, [{
            "op": "add_model",
            "platform": args.platform,
            "model": args.model,
            "overrides": parse_overrides(args.field, args.value),
        }])

    def model_remove(self, args) -> int:
        return self._write(args, [{"op": "remove_model", "platform": args.platform, "model": args.model}])

    def model_set(self, args) -> int:
        return self._write(args, [{
            "op": "set_model_field",
            "platform": args.platform,
            "model": args.model,
            "field": args.field,
            "value": parse_field_values(args),
        }])

    def model_unset(self, args) -> int:
        return self._write(args, [{
            "op": "unset_model_field",
            "platform": args.platform,
            "model": args.model,
            "field": args.field,
        }])

    def alias_list(self, args) -> int:
        aliases = self.service.view()["aliases"]

        if self.console.as_json:
            self.console.emit({"aliases": aliases})
            return EXIT_OK
        if not aliases:
            self.console.line("配置里没有 all_aliases 段")
            return EXIT_OK

        self.console.table(
            [{"name": item["name"], "selectors": ", ".join(item["selectors"])} for item in aliases],
            [("name", "别名"), ("selectors", "指向")],
        )
        return EXIT_OK

    def alias_set(self, args) -> int:
        return self._write(args, [{"op": "set_alias_selectors", "alias": args.alias, "selectors": list(args.selector)}])

    def audit_set(self, args) -> int:
        return self._write(args, [{"op": "set_audit_field", "field": args.field, "value": parse_field_values(args)}])

    def audit_show(self, args) -> int:
        access = AuditAccess(self.service.config_file)
        resolved = access.audit_config
        stats = access.stats()
        access.close()

        if self.console.as_json:
            self.console.emit({"config": resolved.to_dict(), "store": stats})
            return EXIT_OK

        self.console.block("审计配置（环境变量与 audit: 段合并后的结果）", [
            f"{key}: {_brief(value)}" for key, value in resolved.to_dict().items()
        ])
        self.console.block("审计库状态", [f"{key}: {_brief(value)}" for key, value in stats.items()])
        return EXIT_OK

    # ---------- 内部 ----------

    def _write(self, args, ops: List[Dict[str, Any]]) -> int:
        """预演或写盘：两个动作共用同一份补丁，避免「预演通过、写盘却不同」"""
        version = self.service.raw_text()["version"]

        if args.dry_run:
            summary = self.service.plan(ops, version)
            summary["dry_run"] = True
            self.console.summary(summary)
            return EXIT_OK if summary["valid"] else EXIT_ERROR

        try:
            summary = self.service.write(ops, version=version, note=args.note, reload=not args.no_reload)
        except ConfigConflict as exc:
            if self.console.as_json:
                self.console.emit({"valid": False, "conflict": True, "actual_version": exc.actual, "ops": ops})
            else:
                self.console.line("配置在你操作期间被别人改过，为避免覆盖别人的改动已停止。", to_err=True)
                self.console.line(f"请重新执行命令（当前版本 {exc.actual[:12]}）", to_err=True)
            return EXIT_CONFLICT
        except ConfigInvalid as exc:
            if self.console.as_json:
                self.console.emit({"valid": False, "errors": exc.errors, "written": False})
            else:
                self.console.issues(exc.errors, [])
                self.console.line("校验未通过，未写盘。", to_err=True)
            return EXIT_ERROR
        except ConfigWriteError as exc:
            self.console.line(f"写盘被拒绝: {exc.message}", to_err=True)
            return EXIT_ERROR

        self.console.summary(summary)
        return EXIT_OK

    def _platform_table(self, platforms: List[Dict[str, Any]]) -> None:
        rows = [
            {
                "name": item["name"],
                "baseUrl": item["baseUrl"],
                "weight": item["weight"],
                "timeout": item["timeout"],
                "connect_timeout": item["connect_timeout"],
                "first_chunk_timeout": item["first_chunk_timeout"],
                "models": len(item["models"]),
                "scraper": bool(item["plugin"] and item["plugin"]["scraper"]),
                "enabled": item["enabled"],
            }
            for item in platforms
        ]
        self.console.table(rows, [
            ("name", "平台"),
            ("baseUrl", "baseUrl"),
            ("weight", "权重"),
            ("timeout", "超时"),
            ("connect_timeout", "连接超时"),
            ("first_chunk_timeout", "首块预算"),
            ("models", "模型数"),
            ("scraper", "爬虫"),
            ("enabled", "启用"),
        ])

    def _alias_table(self, aliases: List[Dict[str, Any]]) -> None:
        if not aliases:
            return
        self.console.line("")
        self.console.table(
            [{"name": item["name"], "selectors": ", ".join(item["selectors"])} for item in aliases],
            [("name", "别名"), ("selectors", "指向")],
        )

    def _audit_line(self, audit: Dict[str, Any]) -> None:
        self.console.line("")
        self.console.line(f"审计段: {_brief(audit) if audit else '（未配置，使用环境变量默认值）'}")

    def _env_line(self, platforms: List[Dict[str, Any]]) -> None:
        missing = []
        for platform in platforms:
            key = platform["apiKey"]
            if key["kind"] == "env_placeholder" and not key["env_set"]:
                missing.append(f"{platform['name']} -> {key['env']}")
        if not missing:
            return
        self.console.line("")
        self.console.line("警告：以下平台引用的环境变量当前未设置，启动时会被跳过：", to_err=True)
        for item in missing:
            self.console.line(f"  {item}", to_err=True)


# ---------- 工具 ----------


def _api_key_line(key: Dict[str, Any]) -> str:
    if key["kind"] == "env_placeholder":
        state = "已设置" if key["env_set"] else "未设置"
        return f"环境变量 {key['env']}（{state}）"
    return "字面量密钥（已隐藏）"


def _brief(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "-"
    return str(value)
