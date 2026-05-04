"""
通用网页爬虫基类

提供基于Playwright的浏览器自动化能力，支持动态页面加载、数据提取和错误处理。
所有具体的爬虫实现都应继承此类并重写extract_data()方法。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)


class WebScraper(ABC):
    """
    通用网页爬虫抽象基类

    封装了浏览器管理、页面加载、错误处理和重试机制的通用逻辑。
    子类需要实现extract_data()方法来定义具体的数据提取逻辑。

    使用示例:
        class MyScraper(WebScraper):
            async def extract_data(self, page: Page) -> Any:
                # 实现具体的数据提取逻辑
                return await page.evaluate("...")

        scraper = MyScraper(url="https://example.com", timeout=30)
        data = await scraper.scrape()
    """

    def __init__(
        self,
        url: str,
        timeout: int = 60,
        max_retries: int = 3,
        retry_delay: int = 5,
        headless: bool = True,
        user_agent: Optional[str] = None,
        **kwargs
    ):
        """
        初始化爬虫

        Args:
            url: 目标页面URL
            timeout: 页面加载超时时间（秒），默认60秒
            max_retries: 最大重试次数，默认3次
            retry_delay: 重试间隔时间（秒），默认5秒
            headless: 是否使用无头模式，默认True
            user_agent: 自定义User-Agent字符串，可选
            **kwargs: 其他浏览器配置参数
        """
        self.url = url
        self.timeout = timeout * 1000  # 转换为毫秒
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.headless = headless
        self.user_agent = user_agent or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.browser_config = kwargs

        # 运行时状态
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口，确保资源清理"""
        await self.close()

    async def scrape(self) -> Any:
        """
        执行爬虫任务的主入口方法

        包含完整的重试逻辑和错误处理。

        Returns:
            提取的数据（由子类的extract_data()方法返回）

        Raises:
            Exception: 当所有重试都失败时抛出最终异常
        """
        last_exception = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"开始第 {attempt}/{self.max_retries} 次尝试抓取: {self.url}")

                # 加载页面
                await self.load_page()

                # 提取数据（由子类实现）
                data = await self.extract_data(self._page)

                logger.info(f"✓ 成功抓取数据")
                return data

            except PlaywrightTimeoutError as e:
                last_exception = e
                logger.warning(f"⚠ 第 {attempt} 次尝试超时: {e}")

            except Exception as e:
                last_exception = e
                logger.error(f"✗ 第 {attempt} 次尝试失败: {type(e).__name__}: {e}")

            finally:
                # 清理当前尝试的资源
                await self._cleanup()

            # 如果不是最后一次尝试，等待后重试
            if attempt < self.max_retries:
                logger.info(f"等待 {self.retry_delay} 秒后重试...")
                await asyncio.sleep(self.retry_delay)

        # 所有重试都失败
        error_msg = f"爬虫任务失败，已重试 {self.max_retries} 次"
        logger.error(error_msg)
        raise Exception(error_msg) from last_exception

    async def load_page(self) -> None:
        """
        加载目标页面并等待JavaScript渲染完成

        启动Playwright浏览器，导航到目标URL，并等待页面完全加载。

        Raises:
            PlaywrightTimeoutError: 页面加载超时
            Exception: 其他浏览器错误
        """
        logger.debug(f"启动浏览器 (headless={self.headless})...")

        # 启动Playwright
        playwright = await async_playwright().start()

        try:
            # 启动浏览器
            self._browser = await playwright.chromium.launch(
                headless=self.headless,
                **self.browser_config
            )

            # 创建浏览器上下文（配置User-Agent等）
            self._context = await self._browser.new_context(
                user_agent=self.user_agent,
                viewport={"width": 1920, "height": 1080}
            )

            # 创建页面
            self._page = await self._context.new_page()

            # 设置超时
            self._page.set_default_timeout(self.timeout)

            # 导航到目标URL，等待DOM加载完成
            logger.debug(f"导航到: {self.url}")
            await self._page.goto(
                self.url,
                wait_until="domcontentloaded",  # 等待DOM加载完成，不等待所有网络请求
                timeout=self.timeout
            )

            # 额外等待以确保动态内容加载
            await asyncio.sleep(2)

            logger.debug("页面加载完成")

        except Exception as e:
            logger.error(f"页面加载失败: {e}")
            await self._cleanup()
            raise

    @abstractmethod
    async def extract_data(self, page: Page) -> Any:
        """
        提取页面数据（抽象方法，子类必须实现）

        在此方法中实现具体的数据提取逻辑，可以使用：
        - CSS选择器: await page.query_selector()
        - JavaScript评估: await page.evaluate()
        - 网络请求拦截: await page.route()

        Args:
            page: Playwright页面对象

        Returns:
            提取的数据（可以是字典、列表或其他结构）
        """
        pass

    async def _cleanup(self) -> None:
        """清理浏览器资源"""
        try:
            if self._page:
                await self._page.close()
                self._page = None
            if self._context:
                await self._context.close()
                self._context = None
            if self._browser:
                await self._browser.close()
                self._browser = None
        except Exception as e:
            logger.warning(f"清理资源时出错: {e}")

    async def close(self) -> None:
        """
        关闭浏览器并释放资源

        应该在爬虫任务完成后调用此方法，或在上下文管理器退出时自动调用。
        """
        await self._cleanup()
        logger.debug("浏览器资源已释放")

    def get_page_info(self) -> Dict[str, Any]:
        """
        获取当前页面信息（用于调试）

        Returns:
            包含页面信息的字典
        """
        if not self._page:
            return {"status": "page_not_loaded"}

        return {
            "url": self._page.url,
            "title": self._page.title() if self._page else None,
            "status": "loaded"
        }
