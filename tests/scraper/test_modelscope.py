"""ModelScope 爬虫单元测试"""

import pytest
import os
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai_proxy.scraper.modelscope import ModelScopeModelScraper


class TestModelScopeModelScraperInit:
    """测试 ModelScope 爬虫初始化"""

    def test_init_with_default_params(self):
        """测试使用默认参数初始化"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models?filter=inference_type&page=1&sort=default&tabKey=task"
        )
        assert scraper.scrape_url == "https://www.modelscope.cn/models?filter=inference_type&page=1&sort=default&tabKey=task"
        assert scraper.max_models == 50
        assert scraper.timeout == 60000  # 转换为毫秒
        assert scraper.headless is True

    def test_init_with_custom_params(self):
        """测试使用自定义参数初始化"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models",
            max_models=20,
            timeout=30,
            headless=False
        )
        assert scraper.scrape_url == "https://www.modelscope.cn/models"
        assert scraper.max_models == 20
        assert scraper.timeout == 30000  # 转换为毫秒
        assert scraper.headless is False


class TestModelScopeModelScraperProcessAndFilter:
    """测试 ModelScope 爬虫处理和过滤模型"""

    def test_process_and_filter_models_basic(self):
        """测试基本模型处理"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models",
            max_models=10
        )

        models = [
            {"model_id": "model1", "model_name": "Model 1", "rank": 1},
            {"model_id": "model2", "model_name": "Model 2", "rank": 2},
            {"model_id": "model3", "model_name": "Model 3", "rank": 3}
        ]

        result = scraper._process_and_filter_models(models)
        assert len(result) == 3
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"
        assert result[2]["model_id"] == "model3"

    def test_process_and_filter_models_duplicate_ids(self):
        """测试去除重复ID"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models",
            max_models=10
        )

        models = [
            {"model_id": "model1", "model_name": "Model 1", "rank": 1},
            {"model_id": "model1", "model_name": "Model 1 Duplicate", "rank": 2},
            {"model_id": "model2", "model_name": "Model 2", "rank": 3}
        ]

        result = scraper._process_and_filter_models(models)
        assert len(result) == 2
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"

    def test_process_and_filter_models_invalid_ids(self):
        """测试过滤无效ID"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models",
            max_models=10
        )

        models = [
            {"model_id": "", "model_name": "Empty ID", "rank": 1},
            {"model_id": None, "model_name": "None ID", "rank": 2},
            {"model_id": "model1", "model_name": "Model 1", "rank": 3}
        ]

        result = scraper._process_and_filter_models(models)
        assert len(result) == 1
        assert result[0]["model_id"] == "model1"

    def test_process_and_filter_models_limit(self):
        """测试限制模型数量"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models",
            max_models=2
        )

        models = [
            {"model_id": "model1", "model_name": "Model 1", "rank": 1},
            {"model_id": "model2", "model_name": "Model 2", "rank": 2},
            {"model_id": "model3", "model_name": "Model 3", "rank": 3}
        ]

        result = scraper._process_and_filter_models(models)
        assert len(result) == 2
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"

    def test_process_and_filter_models_sort_by_rank(self):
        """测试按排名排序"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models",
            max_models=10
        )

        models = [
            {"model_id": "model3", "model_name": "Model 3", "rank": 3},
            {"model_id": "model1", "model_name": "Model 1", "rank": 1},
            {"model_id": "model2", "model_name": "Model 2", "rank": 2}
        ]

        result = scraper._process_and_filter_models(models)
        assert len(result) == 3
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"
        assert result[2]["model_id"] == "model3"


class TestModelScopeModelScraperParseJsonStructure:
    """测试 ModelScope 爬虫解析JSON结构"""

    def test_parse_json_structure_with_models_key(self):
        """测试解析包含models键的JSON"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models"
        )

        json_data = {
            "models": [
                {"id": "model1", "name": "Model 1"},
                {"id": "model2", "name": "Model 2"}
            ]
        }

        result = scraper._parse_json_structure(json_data)
        assert len(result) == 2
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"

    def test_parse_json_structure_with_data_key(self):
        """测试解析包含data键的JSON"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models"
        )

        json_data = {
            "data": [
                {"id": "model1", "name": "Model 1"},
                {"id": "model2", "name": "Model 2"}
            ]
        }

        result = scraper._parse_json_structure(json_data)
        assert len(result) == 2
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"

    def test_parse_json_structure_with_slug(self):
        """测试解析包含slug的JSON"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models"
        )

        json_data = {
            "items": [
                {"slug": "model1", "name": "Model 1"},
                {"slug": "model2", "name": "Model 2"}
            ]
        }

        result = scraper._parse_json_structure(json_data)
        assert len(result) == 2
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"

    def test_parse_json_structure_nested(self):
        """测试解析嵌套JSON结构"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models"
        )

        json_data = {
            "props": {
                "pageProps": {
                    "models": [
                        {"id": "model1", "name": "Model 1"},
                        {"id": "model2", "name": "Model 2"}
                    ]
                }
            }
        }

        result = scraper._parse_json_structure(json_data)
        assert len(result) == 2
        assert result[0]["model_id"] == "model1"
        assert result[1]["model_id"] == "model2"

    def test_parse_json_structure_no_models(self):
        """测试解析不包含模型的JSON"""
        scraper = ModelScopeModelScraper(
            scrape_url="https://www.modelscope.cn/models"
        )

        json_data = {
            "other_data": "some value"
        }

        result = scraper._parse_json_structure(json_data)
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
