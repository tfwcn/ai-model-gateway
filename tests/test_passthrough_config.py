"""透传配置与维度提取单元测试

覆盖：
- 前缀默认值与路径重写
- 维度表达式解析
- 非法配置只跳过自己
- 环境变量密钥解析
"""
import pytest

from openai_proxy.passthrough.config import parse_routes
from openai_proxy.passthrough.dimensions import DimensionExtractor


class TestRouteConfig:
    """路由配置解析"""

    @pytest.mark.unit
    def test_默认前缀与路径重写(self):
        route = parse_routes({
            "example-service": {
                "upstream": "https://api.example.com/v1",
                "apiKey": "${EXAMPLE_PLATFORM_API_KEY}",
            }
        }, env_resolver=lambda value: "resolved")[0]

        assert route.prefix == "/proxy/example-service"
        assert route.matches("/proxy/example-service") == ""
        assert route.matches("/proxy/example-service/assets/render") == "/assets/render"
        assert route.matches("/proxy/other") is None
        assert route.target_url("/assets/render", "size=1024") == (
            "https://api.example.com/v1/assets/render?size=1024"
        )
        assert route.api_key == "resolved"

    @pytest.mark.unit
    def test_维度表达式解析(self):
        route = parse_routes({
            "example-service": {
                "upstream": "https://api.example.com/v1",
                "dimensions": {
                    "model": "body.model",
                    "size": "body.data.size",
                    "tenant": "header.x-tenant",
                    "stage": "path.0",
                },
            }
        })[0]

        assert [spec.name for spec in route.dimensions] == ["model", "size", "tenant", "stage"]
        assert route.default_dimensions()["platform"] == "passthrough"
        assert route.describe()["dimensions"] == ["model", "size", "tenant", "stage"]

    @pytest.mark.unit
    def test_非法路由只跳过自己(self):
        routes = parse_routes({
            "good": {"upstream": "https://api.example.com/v1"},
            "bad-prefix": {"upstream": "https://api.example.com/v1", "prefix": "/api/assets"},
            "bad-upstream": {"upstream": "ftp://example.com"},
            "bad-dimension": {"upstream": "https://api.example.com/v1", "dimensions": {"size": "payload.size"}},
        })

        assert [route.name for route in routes] == ["good"]

    @pytest.mark.unit
    def test_前缀重复时后者被跳过(self):
        routes = parse_routes({
            "first": {"upstream": "https://api.example.com/v1", "prefix": "/proxy/same"},
            "second": {"upstream": "https://api.example.com/v1", "prefix": "/proxy/same"},
        })

        assert [route.name for route in routes] == ["first"]


class TestDimensionExtractor:
    """请求参数提取"""

    @pytest.mark.unit
    def test_从四类来源提取并使用配置的键名(self):
        route = parse_routes({
            "example-service": {
                "upstream": "https://api.example.com/v1",
                "dimensions": {
                    "model": "body.data.items[0].name",
                    "size": "query.size",
                    "tenant": "header.X-Tenant",
                    "stage": "path.0",
                    "missing": "body.not-there",
                },
            }
        })[0]
        extractor = DimensionExtractor(route.dimensions)

        values = extractor.extract(
            body=b'{"data":{"items":[{"name":"model-a"}]}}',
            query={"size": "1024x1024"},
            headers={"x-tenant": "team-a"},
            remainder_path="/assets/render",
        )

        assert values == {
            "model": "model-a",
            "size": "1024x1024",
            "tenant": "team-a",
            "stage": "assets",
            "missing": "",
        }

    @pytest.mark.unit
    def test_超长维度值被截断(self):
        route = parse_routes({
            "example-service": {
                "upstream": "https://api.example.com/v1",
                "dimensions": {"size": "query.size"},
            }
        })[0]
        values = DimensionExtractor(route.dimensions).extract(
            body=b"", query={"size": "x" * 500}, headers={}, remainder_path="/"
        )

        assert len(values["size"]) == 200

    @pytest.mark.unit
    def test_请求体不是JSON时维度为空(self):
        route = parse_routes({
            "example-service": {
                "upstream": "https://api.example.com/v1",
                "dimensions": {"model": "body.model"},
            }
        })[0]
        values = DimensionExtractor(route.dimensions).extract(
            body=b"not-json", query={}, headers={}, remainder_path="/"
        )

        assert values["model"] == ""
