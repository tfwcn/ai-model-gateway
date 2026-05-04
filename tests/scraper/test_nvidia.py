"""
NVIDIA模型爬虫单元测试
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from openai_proxy.scraper.nvidia import NVIDIAModelScraper


class TestNVIDIAModelScraper:
    """NVIDIAModelScraper测试类"""

    def test_init_default(self):
        """测试默认初始化"""
        scraper = NVIDIAModelScraper()

        assert scraper.free_model_count == 10
        assert scraper.use_web_scraping == False
        assert scraper.timeout == 60000  # 转换为毫秒
        assert scraper.max_retries == 3

    def test_init_custom_params(self):
        """测试自定义参数初始化"""
        scraper = NVIDIAModelScraper(
            free_model_count=20,
            timeout=120,
            max_retries=5,
            headless=False
        )

        assert scraper.free_model_count == 20
        assert scraper.timeout == 120000
        assert scraper.max_retries == 5
        assert scraper.headless == False

    def test_free_model_patterns(self):
        """测试免费模型识别模式"""
        scraper = NVIDIAModelScraper()

        # 应该匹配的免费模型
        free_models = [
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "microsoft/phi-3-mini-4k-instruct",
            "google/gemma-2-9b-it",
            "meta/llama-3.1-8b-instruct",
            "deepseek-ai/deepseek-coder-6.7b-instruct",
            "qwen/qwen-2.5-72b-instruct",
            "z-ai/glm-4-9b-chat",
            "minimaxai/minimax-01",
            "moonshotai/kimi-k2-instruct",
        ]

        # 不应该匹配的付费模型
        paid_models = [
            "somevendor/expensive-model",
            "unknown/model-x",
        ]

        import re
        for model_id in free_models:
            is_free = any(
                re.match(pattern, model_id)
                for pattern in scraper.free_model_patterns
            )
            assert is_free, f"应该识别为免费模型: {model_id}"

        for model_id in paid_models:
            is_free = any(
                re.match(pattern, model_id)
                for pattern in scraper.free_model_patterns
            )
            assert not is_free, f"不应该识别为免费模型: {model_id}"

    def test_filter_free_models(self):
        """测试免费模型过滤功能"""
        scraper = NVIDIAModelScraper(free_model_count=5)

        all_models = [
            {"id": "nvidia/test-model", "name": "NVIDIA Test"},
            {"id": "microsoft/phi-3", "name": "Phi-3"},
            {"id": "paid/expensive", "name": "Expensive Model"},
            {"id": "google/gemma-2", "name": "Gemma-2"},
            {"id": "another/paid", "name": "Another Paid"},
            {"id": "deepseek-ai/coder", "name": "DeepSeek Coder"},
        ]

        free_models = scraper._filter_free_models(all_models)

        # 应该只返回免费模型
        assert len(free_models) == 4
        assert all(m["model_id"] in ["nvidia/test-model", "microsoft/phi-3",
                                     "google/gemma-2", "deepseek-ai/coder"]
                   for m in free_models)

        # 验证排名
        for i, model in enumerate(free_models):
            assert model["rank"] == i + 1

    def test_filter_respects_count_limit(self):
        """测试过滤后的数量限制"""
        scraper = NVIDIAModelScraper(free_model_count=2)

        all_models = [
            {"id": "nvidia/model-1"},
            {"id": "microsoft/phi-3-mini"},  # 匹配 microsoft/phi-
            {"id": "google/gemma-2b"},       # 匹配 google/gemma-
            {"id": "deepseek-ai/model-4"},
        ]

        # _filter_free_models 不截取，由调用方截取
        free_models = scraper._filter_free_models(all_models)
        assert len(free_models) == 4  # 所有都匹配免费模式

        # 但process_and_filter会截取
        processed = scraper._process_and_filter_models([
            {"model_id": m["id"], "model_name": m["id"],
             "owner": m["id"].split("/")[0], "rank": i+1}
            for i, m in enumerate(all_models)
        ])
        assert len(processed) == 2  # 限制为2个

    def test_process_and_filter_removes_duplicates(self):
        """测试去重功能"""
        scraper = NVIDIAModelScraper()

        models_with_dups = [
            {"model_id": "test/model-1", "model_name": "Model 1", "rank": 1},
            {"model_id": "test/model-1", "model_name": "Model 1 Dup", "rank": 2},
            {"model_id": "test/model-2", "model_name": "Model 2", "rank": 3},
        ]

        result = scraper._process_and_filter_models(models_with_dups)

        assert len(result) == 2
        model_ids = [m["model_id"] for m in result]
        assert "test/model-1" in model_ids
        assert "test/model-2" in model_ids

    def test_process_and_filter_removes_invalid(self):
        """测试移除无效模型ID"""
        scraper = NVIDIAModelScraper()

        models_with_invalid = [
            {"model_id": "valid/model-1", "model_name": "Valid 1", "rank": 1},
            {"model_id": "", "model_name": "Empty ID", "rank": 2},
            {"model_id": "no-slash", "model_name": "No Slash", "rank": 3},
            {"model_id": None, "model_name": "None ID", "rank": 4},
            {"model_id": "valid/model-2", "model_name": "Valid 2", "rank": 5},
        ]

        result = scraper._process_and_filter_models(models_with_invalid)

        assert len(result) == 2
        assert all("/" in m["model_id"] for m in result)

    def test_process_and_filter_sorts_by_rank(self):
        """测试按排名排序"""
        scraper = NVIDIAModelScraper()

        unsorted_models = [
            {"model_id": "test/model-3", "model_name": "Model 3", "rank": 3},
            {"model_id": "test/model-1", "model_name": "Model 1", "rank": 1},
            {"model_id": "test/model-2", "model_name": "Model 2", "rank": 2},
        ]

        result = scraper._process_and_filter_models(unsorted_models)

        assert result[0]["rank"] == 1
        assert result[1]["rank"] == 2
        assert result[2]["rank"] == 3

    @pytest.mark.asyncio
    async def test_extract_from_api_success(self):
        """测试API提取成功场景"""
        scraper = NVIDIAModelScraper(free_model_count=3)
        scraper.api_key = "test-key"

        mock_response_data = {
            "data": [
                {"id": "nvidia/test-model", "name": "Test Model"},
                {"id": "microsoft/phi-3", "name": "Phi-3"},
                {"id": "paid/expensive", "name": "Expensive"},
            ]
        }

        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value.__aenter__.return_value = mock_session

            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value=mock_response_data)

            mock_get = AsyncMock(return_value=mock_response)
            mock_session.get.return_value.__aenter__.return_value = mock_get

            result = await scraper._extract_from_api()

            # 应该只返回免费模型，且限制数量
            assert len(result) <= 3
            assert all("/" in m["model_id"] for m in result)

    @pytest.mark.asyncio
    async def test_extract_from_api_failure(self):
        """测试API提取失败场景"""
        scraper = NVIDIAModelScraper()
        scraper.api_key = "test-key"

        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value.__aenter__.return_value = mock_session

            mock_response = AsyncMock()
            mock_response.status = 500

            mock_get = AsyncMock(return_value=mock_response)
            mock_session.get.return_value.__aenter__.return_value = mock_get

            result = await scraper._extract_from_api()

            assert result == []

    @pytest.mark.asyncio
    async def test_extract_from_api_exception(self):
        """测试API提取异常场景"""
        scraper = NVIDIAModelScraper()
        scraper.api_key = "test-key"

        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session_class.side_effect = Exception("Connection error")

            result = await scraper._extract_from_api()

            assert result == []

    def test_parse_json_structure(self):
        """测试JSON结构解析"""
        scraper = NVIDIAModelScraper()

        # 模拟Next.js嵌入数据结构
        json_data = {
            "props": {
                "pageProps": {
                    "models": [
                        {"id": "test/model-1", "name": "Model 1"},
                        {"id": "test/model-2", "name": "Model 2"},
                    ]
                }
            }
        }

        result = scraper._parse_json_structure(json_data)

        assert result is not None
        assert len(result) == 2
        assert result[0]["model_id"] == "test/model-1"

    def test_parse_json_structure_nested(self):
        """测试嵌套JSON结构解析"""
        scraper = NVIDIAModelScraper()

        json_data = {
            "initialState": {
                "data": {
                    "items": [
                        {"id": "nested/model-1"},
                        {"id": "nested/model-2"},
                    ]
                }
            }
        }

        result = scraper._parse_json_structure(json_data)

        assert result is not None
        assert len(result) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
