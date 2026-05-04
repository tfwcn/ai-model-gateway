"""
工具调用能力缓存管理器

负责管理模型工具调用能力测试结果的JSON文件缓存，包括保存、加载、验证和错误日志记录。
使用原子操作确保数据完整性。
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class CapabilityCacheManager:
    """
    工具调用能力缓存管理器

    管理模型工具调用能力测试结果的JSON文件缓存，提供：
    - 原子写入（先写临时文件再重命名）
    - 数据验证和完整性检查
    - 永久缓存（无过期时间）
    - 增量更新支持

    缓存格式:
    {
        "version": 1,
        "updated_at": "2026-05-04T07:30:00Z",
        "models": {
            "deepseek-ai/DeepSeek-V4-Flash": {
                "supports_tools": true,
                "tested_at": "2026-05-04T07:30:00Z",
                "platform": "modelscope"
            }
        }
    }

    使用示例:
        cache = CapabilityCacheManager(cache_file="data/tool_capability.json")

        # 保存数据
        cache.save(models={...})

        # 加载数据
        data = cache.load()
        if data:
            capabilities = data['models']
    """

    CACHE_VERSION = 1

    def __init__(self, cache_file: str = "data/tool_capability.json"):
        """
        初始化缓存管理器

        Args:
            cache_file: 缓存文件路径（相对于项目根目录）
        """
        self.cache_file = Path(cache_file)

        # 确保目录存在
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

        logger.debug(f"能力缓存管理器初始化: {self.cache_file}")

    def save(
        self,
        models: Dict[str, Dict[str, Any]],
        platform: Optional[str] = None
    ) -> bool:
        """
        保存能力测试结果到缓存文件

        使用原子操作：先写入临时文件，成功后再重命名为目标文件。
        支持增量更新：合并新结果到现有缓存中。

        Args:
            models: 模型能力映射 {model_id: {"supports_tools": bool, "tested_at": str, ...}}
            platform: 平台名称（可选，用于日志）

        Returns:
            是否保存成功
        """
        try:
            # 加载现有缓存（如果存在）
            existing_data = self.load() or {"models": {}}

            # 合并新结果到现有数据
            for model_id, capability in models.items():
                existing_data["models"][model_id] = capability

            # 构建缓存数据结构
            cache_data = {
                "version": self.CACHE_VERSION,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "models": existing_data["models"]
            }

            # 原子写入：先写临时文件
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self.cache_file.parent,
                suffix=".tmp",
                prefix=".capability_"
            )

            try:
                with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)

                # 重命名临时文件为目标文件（原子操作）
                os.replace(temp_path, str(self.cache_file))

                platform_str = f" [{platform}]" if platform else ""
                logger.info(f"✓ 能力缓存已保存{platform_str}: {len(models)} 个模型")
                return True

            except Exception as e:
                # 清理临时文件
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise e

        except Exception as e:
            logger.error(f"✗ 保存能力缓存失败: {e}", exc_info=True)
            return False

    def load(self) -> Optional[Dict[str, Any]]:
        """
        从缓存文件加载数据

        Returns:
            缓存数据字典（包含version、updated_at、models），如果文件不存在或损坏则返回None
        """
        if not self.is_valid():
            return None

        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # 验证数据结构
            if not self._validate_cache_data(cache_data):
                logger.warning("能力缓存数据格式无效，删除损坏的文件")
                self.cache_file.unlink(missing_ok=True)
                return None

            # 检查版本兼容性
            if cache_data.get("version") != self.CACHE_VERSION:
                logger.warning(
                    f"能力缓存版本不匹配 (期望: {self.CACHE_VERSION}, 实际: {cache_data.get('version')}), "
                    f"清除旧缓存"
                )
                self.cache_file.unlink(missing_ok=True)
                return None

            model_count = len(cache_data.get('models', {}))
            logger.debug(f"✓ 能力缓存已加载: {model_count} 个模型")
            return cache_data

        except json.JSONDecodeError as e:
            logger.error(f"能力缓存文件JSON格式错误: {e}")
            # 删除损坏的文件
            self.cache_file.unlink(missing_ok=True)
            return None

        except Exception as e:
            logger.error(f"加载能力缓存失败: {e}", exc_info=True)
            return None

    def is_valid(self) -> bool:
        """
        检查缓存文件是否存在且有效

        Returns:
            True如果缓存文件存在且可读
        """
        if not self.cache_file.exists():
            logger.debug("能力缓存文件不存在")
            return False

        if not self.cache_file.is_file():
            logger.warning("能力缓存路径不是文件")
            return False

        # 尝试读取并验证JSON格式
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            is_valid = self._validate_cache_data(data)
            if not is_valid:
                # 删除无效文件
                logger.warning("检测到无效的能力缓存文件，正在删除")
                self.cache_file.unlink(missing_ok=True)
                return False
            return True

        except Exception as e:
            logger.warning(f"能力缓存文件验证失败: {e}")
            return False

    def get_model_capability(self, model_id: str) -> Optional[bool]:
        """
        获取单个模型的工具调用能力

        Args:
            model_id: 模型ID

        Returns:
            True表示支持工具调用，False表示不支持，None表示未测试
        """
        cache_data = self.load()
        if not cache_data:
            return None

        model_info = cache_data.get("models", {}).get(model_id)
        if not model_info:
            return None

        return model_info.get("supports_tools")

    def clear(self) -> bool:
        """
        清除缓存文件

        Returns:
            是否清除成功
        """
        try:
            if self.cache_file.exists():
                self.cache_file.unlink()
                logger.info("✓ 能力缓存已清除")
            return True
        except Exception as e:
            logger.error(f"✗ 清除能力缓存失败: {e}")
            return False

    def _validate_cache_data(self, data: Dict[str, Any]) -> bool:
        """
        验证缓存数据结构

        Args:
            data: 缓存数据

        Returns:
            True如果数据结构有效
        """
        if not isinstance(data, dict):
            return False

        # 检查必需字段
        required_fields = ["version", "updated_at", "models"]
        for field in required_fields:
            if field not in data:
                logger.warning(f"缓存数据缺少必需字段: {field}")
                return False

        # 验证models字段类型
        if not isinstance(data["models"], dict):
            logger.warning("缓存数据中models字段类型错误")
            return False

        return True
