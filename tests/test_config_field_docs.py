"""字段字典与配置结构说明的单元测试

锁住三件事：
1. 凡是管理台/CLI 能改的字段，字典里必须有对应说明（界面不会出现「能改但没解释」）
2. 每条说明要说得清：名字、语义、默认值、取值、写法示例、生效方式
3. 写法示例只用通用占位符（example-platform / api.example.com / EXAMPLE_*_API_KEY / model-a）
"""
import re
from typing import Dict, List

import pytest

from openai_proxy.config_store import config_guide, field_docs, schema


def _all_docs() -> Dict[str, field_docs.FieldDoc]:
    """两份字典合起来看。audit 段的 enabled 与平台级的重名，加前缀保住各自的那份"""
    merged: Dict[str, field_docs.FieldDoc] = dict(field_docs.FIELD_DOCS)
    for field, doc in field_docs.AUDIT_DOCS.items():
        merged[field if field not in merged else f"audit.{field}"] = doc
    for field, doc in field_docs.PASSTHROUGH_DOCS.items():
        merged[field if field not in merged else f"passthrough.{field}"] = doc
    return merged


def _editable_fields() -> List[str]:
    return sorted(
        set(schema.PLATFORM_FIELDS) | set(schema.MODEL_FIELDS)
        | set(schema.AUDIT_FIELDS) | set(schema.PASSTHROUGH_FIELDS)
    )


class TestCoverage:
    """字典必须覆盖所有能改的字段"""

    @pytest.mark.unit
    def test_每个可编辑字段都有说明(self):
        missing = field_docs.missing_documentation(_editable_fields())
        assert not missing, f"这些字段能改但字典里没有说明: {', '.join(missing)}"

    @pytest.mark.unit
    def test_字典里不留已经不能改的字段(self):
        """字段从白名单里删掉时，说明也要跟着删，免得界面显示过期的解释"""
        stale = set(field_docs.documented_fields()) - set(_editable_fields())
        assert not stale, f"这些说明对应的字段已经不能改了: {', '.join(sorted(stale))}"

    @pytest.mark.unit
    def test_audit与平台的同名字段各有各的说明(self):
        platform = field_docs.doc_for("enabled", "platform")
        audit = field_docs.doc_for("enabled", "audit")
        assert platform and audit
        assert platform["summary"] != audit["summary"]

    @pytest.mark.unit
    def test_按作用域取说明(self):
        assert field_docs.doc_for("retention_days", "audit"), "audit 段字段要有说明"
        assert not field_docs.doc_for("retention_days", "platform"), "audit 字段不该出现在平台级"
        assert field_docs.doc_for("timeout", "model"), "模型级覆盖共用平台级语义"


class TestContent:
    """说明本身要能当文档读"""

    @pytest.mark.unit
    def test_每条说明都写全了必填项(self):
        for field, doc in _all_docs().items():
            assert doc.label, f"{field} 缺少中文名"
            assert len(doc.summary) >= 15, f"{field} 的语义说明太短"
            assert doc.unit, f"{field} 缺少取值单位"
            assert doc.default, f"{field} 缺少默认值说明"
            assert doc.example, f"{field} 缺少写法示例"
            assert doc.effect in (field_docs.RELOAD, field_docs.RESTART), f"{field} 的生效方式不合法"

    @pytest.mark.unit
    def test_写法示例只用通用占位符(self):
        hosts = set()
        for field, doc in _all_docs().items():
            hosts |= set(re.findall(r"https?://([^/\\s\"']+)", doc.example))
        assert hosts <= {"api.example.com"}, f"写法示例里出现了不该出现的地址: {sorted(hosts)}"

    @pytest.mark.unit
    def test_需要重启的字段都明确标注(self):
        # audit 段只在启动时应用一次；插件实例按模块名复用
        for field in schema.AUDIT_FIELDS:
            assert field_docs.doc_for(field, "audit")["effect"] == field_docs.RESTART, field
        assert field_docs.doc_for("plugin")["effect"] == field_docs.RESTART

    @pytest.mark.unit
    def test_容易踩的坑写在说明里(self):
        """这几条是真实的误用点：说明里必须提到，否则用户只有踩了才知道"""
        notes = {field: doc.note for field, doc in _all_docs().items()}
        assert "0" in notes["retry_count"] and "1" in notes["retry_count"]
        assert "第一个" in notes["weight"]
        assert "v1/models" in notes["enabled"]
        assert "环境变量" in notes["metrics_label_model"]
        assert "blacklist" in notes["models"] or "blacklist" in notes["blacklist"]

    @pytest.mark.unit
    def test_插件写法示例与代码读取的位置一致(self):
        """cache_timeout 只在 plugin 顶层被读取：示例挂进 args 会把人带错（代码里就是 plugin_config.get("cache_timeout")）"""
        from ruamel.yaml import YAML

        example = field_docs.doc_for("plugin")["example"]
        plugin = YAML(typ="safe").load(example)["plugin"]

        assert "cache_timeout" in plugin, "示例里的 cache_timeout 必须与 code 同级"
        assert "cache_timeout" not in (plugin.get("args") or {}), "cache_timeout 写在 args 里不会被读到"
        assert "顶层" in field_docs.doc_for("plugin")["note"]


class TestLimits:
    """边界与说明要一致"""

    @pytest.mark.unit
    def test_retry_count不允许0(self):
        issues = schema.check_value("nonneg_int", "retry_count", 0, "platforms.example-platform.retry_count")
        assert issues, "retry_count 0 应当被拒绝"
        assert "只试一次请写 1" in issues[0].message

    @pytest.mark.unit
    def test_retry_count的边界与报错理由(self):
        limits = {field: {"min": low, "max": high} for field, (low, high) in schema._LIMITS.items()}
        assert limits["retry_count"] == {"min": 1, "max": 20}
        assert "只试一次请写 1" in schema._limit_note("retry_count")

    @pytest.mark.unit
    def test_写法示例里的数值不能越界(self):
        """字典里的示例必须是界面接受得了的写法，否则用户照着抄反而被拒"""
        for field, doc in _all_docs().items():
            match = re.search(r":\s*([0-9]+(?:\.[0-9]+)?)\s*$", doc.example)
            if not match or field not in schema._LIMITS:
                continue
            low, high = schema._LIMITS[field]
            value = float(match.group(1))
            assert low <= value <= high, f"{field} 的示例 {value} 落在边界 {low}~{high} 之外"


class Test取值选项:
    """有限取值的字段：界面给下拉，选项必须与运行时真正认识的写法一致"""

    @pytest.mark.unit
    def test_选项非空且不重复(self):
        enums = field_docs.enum_fields()
        assert enums, "字典里一个取值选项都没有，界面只能让人手打"
        for field, entry in enums.items():
            assert entry["values"], f"{field} 标了选项却是空的"
            assert len(set(entry["values"])) == len(entry["values"]), f"{field} 的选项有重复"

    @pytest.mark.unit
    def test_选项都写在说明的取值范围里(self):
        """下拉里的每个取值都要能在同一条说明里读到，否则选项与文档各说各话"""
        for field, entry in field_docs.enum_fields().items():
            scope = "audit" if field in field_docs.AUDIT_DOCS else "platform"
            doc = field_docs.doc_for(field, scope)
            for value in entry["values"]:
                assert value in doc["unit"], f"{field} 的选项 {value} 没有出现在说明的取值范围里"

    @pytest.mark.unit
    def test_写法示例必须是从下拉里选得到的那一个(self):
        enums = field_docs.enum_fields()
        for field, entry in enums.items():
            scope = "audit" if field in field_docs.AUDIT_DOCS else "platform"
            written = field_docs.doc_for(field, scope)["example"].split(":")[-1].strip()
            assert written in entry["values"], f"{field} 的示例写法 {written} 不在选项里"

    @pytest.mark.unit
    def test_quota_period的选项就是运行时会认的周期(self):
        """说明与下拉都写四种周期：运行时刻意只认这四种写法"""
        from datetime import datetime, timedelta

        from openai_proxy.model.state import ModelStateManager

        state = ModelStateManager()
        values = field_docs.enum_fields()["quota_period"]["values"]
        assert set(values) == {"hourly", "daily", "weekly", "monthly"}

        now = datetime.now()
        for period in values:
            expiry = state._get_period_expiry(period)
            assert expiry > now, f"{period} 的到期时间不在将来"
            assert expiry.second == 0 and expiry.microsecond == 0, f"{period} 的到期时间没对齐到整分"

        hourly = state._get_period_expiry("hourly")
        assert hourly.minute == 0 and hourly - now <= timedelta(hours=1), "hourly 不是下个整点"
        daily = state._get_period_expiry("daily")
        assert daily.hour == 0 and daily - now <= timedelta(days=1), "daily 不是次日 0 点"
        weekly = state._get_period_expiry("weekly")
        assert weekly.hour == 0 and weekly.weekday() == 0, "weekly 不是下周一 0 点"
        monthly = state._get_period_expiry("monthly")
        assert monthly.day == 1 and monthly.hour == 0, "monthly 不是下月 1 号 0 点"
        # 说明里写了「其它写法按 daily 处理」：运行时的兜底确实是「次日 0 点」
        fallback = state._get_period_expiry("yearly")
        assert fallback.hour == 0 and timedelta(0) < (fallback - now) <= timedelta(days=1), "未知周期没有按每日兜底"

    @pytest.mark.unit
    def test_标了可以不配置的字段真的接受空值(self):
        """界面上的「不配置」选项要么走删除操作，要么写 null：后端必须收得下"""
        kinds = {**schema.PLATFORM_FIELDS, **schema.MODEL_FIELDS, **schema.AUDIT_FIELDS, **schema.PASSTHROUGH_FIELDS}
        for field, entry in field_docs.enum_fields().items():
            if not entry["allow_none"]:
                continue
            issues = schema.check_value(kinds[field], field, None, f"platforms.example-platform.{field}")
            assert not issues, f"{field} 标了可以不配置，但后端拒绝空值"

    @pytest.mark.unit
    def test_说明里的取值选项对界面与命令行是同一份(self):
        """dict 出去的取值必须是 JSON 原生类型：tuple 与 list 会让两边对不上"""
        for field in field_docs.documented_fields():
            scope = "audit" if field in field_docs.AUDIT_DOCS else (
                "passthrough" if field in field_docs.PASSTHROUGH_DOCS and field not in field_docs.FIELD_DOCS
                else "platform"
            )
            entry = field_docs.doc_for(field, scope)
            assert isinstance(entry["values"], list), f"{field} 的取值选项不是 list，JSON 两边会不一致"


class TestGuide:
    """结构说明（页面即文档的那几段）"""

    @pytest.mark.unit
    def test_段落不重复且都有正文(self):
        sections = config_guide.guide_sections()
        titles = [item["title"] for item in sections]
        assert titles and len(titles) == len(set(titles))
        assert all(item["body"].strip() for item in sections)

    @pytest.mark.unit
    def test_说清了哪些改动必须重启(self):
        body = "\n".join(item["body"] for item in config_guide.guide_sections())
        for keyword in ("audit", "plugin", "环境变量", "CONFIG_WATCH_ENABLED"):
            assert keyword in body, f"结构说明里没提到: {keyword}"

    @pytest.mark.unit
    def test_说清了model的几种写法(self):
        body = "\n".join(item["body"] for item in config_guide.guide_sections())
        for keyword in ("all", "平台名|模型名", "别名"):
            assert keyword in body, f"结构说明里没提到 model 写法: {keyword}"
