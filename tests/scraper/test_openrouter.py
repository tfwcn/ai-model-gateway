"""
OpenRouter 爬虫测试
"""

import asyncio
import logging
import pytest
from openai_proxy.scraper.openrouter import OpenRouterModelScraper

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

@pytest.mark.asyncio
async def test_openrouter_scraper():
    """测试 OpenRouter 爬虫"""

    # 创建爬虫实例
    scraper = OpenRouterModelScraper(
        scrape_url="https://openrouter.ai/models?fmt=cards&input_modalities=text%2Cimage&max_price=0&order=most-popular&output_modalities=text",
        max_models=10,
        timeout=60,
        headless=True
    )

    try:
        print("开始爬取 OpenRouter 免费模型...")
        models = await scraper.scrape()

        print(f"\n✓ 成功获取到 {len(models)} 个模型:\n")
        for i, model in enumerate(models, 1):
            print(f"{i}. {model.get('model_id')} - {model.get('model_name')}")

        assert len(models) > 0, "应该获取到至少一个模型"
        return models

    except Exception as e:
        print(f"✗ 爬取失败: {e}")
        raise
