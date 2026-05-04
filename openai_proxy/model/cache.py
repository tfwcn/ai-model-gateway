"""
模型缓存管理器

负责管理爬虫结果的JSON文件缓存，包括保存、加载、验证和错误日志记录。
使用原子操作确保数据完整性。
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ModelCacheManager:
    """
    模型缓存管理器

    管理免费模型列表的JSON文件缓存，提供：
    - 原子写入（先写临时文件再重命名）
    - 数据验证和完整性检查
    - 错误日志管理（限制大小）
    - 元数据跟踪

    使用示例:
        cache = ModelCacheManager(cache_file="data/nvidia_free_models.json")

        # 保存数据
        cache.save(models=[...], metadata={...})

        # 加载数据
        data = cache.load()
        if data:
            models = data['models']
    """

    def __init__(self, cache_file: str, max_error_log_size: int = 100):
        """
        初始化缓存管理器

        Args:
            cache_file: 缓存文件路径
            max_error_log_size: 错误日志最大条目数，默认100
        """
        self.cache_file = Path(cache_file)
        self.max_error_log_size = max_error_log_size

        # 确保目录存在
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

        logger.debug(f"缓存管理器初始化: {self.cache_file}")

    def save(
        self,
        models: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
        success: bool = True
    ) -> bool:
        """
        保存模型数据到缓存文件

        使用原子操作：先写入临时文件，成功后再重命名为目标文件。

        Args:
            models: 模型列表
            metadata: 元数据（fetch_time, total_count等）
            success: 是否为成功执行（用于更新last_success时间）

        Returns:
            是否保存成功
        """
        try:
            # 构建缓存数据结构
            cache_data = {
                "models": models,
                "metadata": metadata or {},
                "last_success": datetime.now(timezone.utc).isoformat() if success else self._get_last_success(),
                "error_log": self._load_error_log()
            }

            # 如果失败，添加错误记录
            if not success and metadata and "error" in metadata:
                error_entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": metadata["error"]
                }
                cache_data["error_log"].append(error_entry)

                # 限制日志大小
                keep_count = min(self.max_error_log_size, 50)
                if len(cache_data["error_log"]) > keep_count:
                    cache_data["error_log"] = cache_data["error_log"][-keep_count:]

            # 原子写入：先写临时文件
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self.cache_file.parent,
                suffix=".tmp",
                prefix=".cache_"
            )

            try:
                with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)

                # 重命名临时文件为目标文件（原子操作）
                os.replace(temp_path, str(self.cache_file))

                logger.info(f"✓ 缓存已保存: {len(models)} 个模型")
                return True

            except Exception as e:
                # 清理临时文件
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise e

        except Exception as e:
            logger.error(f"✗ 保存缓存失败: {e}", exc_info=True)
            return False

    def load(self) -> Optional[Dict[str, Any]]:
        """
        从缓存文件加载数据

        Returns:
            缓存数据字典（包含models、metadata等），如果文件不存在或损坏则返回None
        """
        if not self.is_valid():
            return None

        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # 验证数据结构
            if not self._validate_cache_data(cache_data):
                logger.warning("缓存数据格式无效，删除损坏的文件")
                self.cache_file.unlink(missing_ok=True)
                return None

            logger.debug(f"✓ 缓存已加载: {len(cache_data.get('models', []))} 个模型")
            return cache_data

        except json.JSONDecodeError as e:
            logger.error(f"缓存文件JSON格式错误: {e}")
            # 删除损坏的文件
            self.cache_file.unlink(missing_ok=True)
            return None

        except Exception as e:
            logger.error(f"加载缓存失败: {e}", exc_info=True)
            return None

    def is_valid(self) -> bool:
        """
        检查缓存文件是否存在且有效

        Returns:
            True如果缓存文件存在且可读
        """
        if not self.cache_file.exists():
            logger.debug("缓存文件不存在")
            return False

        if not self.cache_file.is_file():
            logger.warning("缓存路径不是文件")
            return False

        # 尝试读取并验证JSON格式
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            is_valid = self._validate_cache_data(data)
            if not is_valid:
                # 删除无效文件
                logger.warning("检测到无效的缓存文件，正在删除")
                self.cache_file.unlink(missing_ok=True)
            return is_valid
        except json.JSONDecodeError:
            # JSON格式错误，删除文件
            logger.warning("检测到损坏的JSON文件，正在删除")
            self.cache_file.unlink(missing_ok=True)
            return False
        except Exception as e:
            logger.debug(f"缓存验证失败: {e}")
            return False

    def clear(self) -> bool:
        """
        清除缓存文件

        Returns:
            是否成功清除
        """
        try:
            if self.cache_file.exists():
                self.cache_file.unlink()
                logger.info("缓存已清除")
            return True
        except Exception as e:
            logger.error(f"清除缓存失败: {e}")
            return False

    def get_cache_info(self) -> Dict[str, Any]:
        """
        获取缓存信息

        Returns:
            包含缓存状态的字典
        """
        if not self.is_valid():
            return {"status": "invalid", "exists": self.cache_file.exists()}

        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            return {
                "status": "valid",
                "model_count": len(data.get("models", [])),
                "last_success": data.get("last_success"),
                "error_count": len(data.get("error_log", [])),
                "file_size": self.cache_file.stat().st_size,
                "file_path": str(self.cache_file)
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _validate_cache_data(self, data: Any) -> bool:
        """
        验证缓存数据的结构和完整性

        Args:
            data: 要验证的数据

        Returns:
            True如果数据有效
        """
        if not isinstance(data, dict):
            return False

        # 必须包含models字段
        if "models" not in data:
            return False

        # models必须是列表
        if not isinstance(data["models"], list):
            return False

        # 验证每个模型的基本结构
        for model in data["models"]:
            if not isinstance(model, dict):
                return False
            if "model_id" not in model:
                return False

        return True

    def _load_error_log(self) -> List[Dict[str, str]]:
        """
        加载错误日志

        Returns:
            错误日志列表
        """
        if not self.is_valid():
            return []

        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get("error_log", [])
        except Exception:
            return []

    def _add_error_log(self, error_message: str) -> None:
        """
        添加错误日志记录

        Args:
            error_message: 错误消息
        """
        error_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": error_message
        }

        # 加载现有日志
        error_log = self._load_error_log()
        error_log.append(error_entry)

        # 限制日志大小：保留最近的max_error_log_size条，但只保留50条以防过大
        keep_count = min(self.max_error_log_size, 50)
        if len(error_log) > keep_count:
            error_log = error_log[-keep_count:]

        # 保存到文件
        try:
            if self.is_valid():
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                data["error_log"] = error_log

                # 原子写入
                temp_fd, temp_path = tempfile.mkstemp(
                    dir=self.cache_file.parent,
                    suffix=".tmp",
                    prefix=".cache_"
                )
                try:
                    with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(temp_path, str(self.cache_file))
                except Exception:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                    raise
        except Exception as e:
            logger.error(f"保存错误日志失败: {e}")

    def _get_last_success(self) -> Optional[str]:
        """
        获取上次成功时间

        Returns:
            ISO格式的时间戳，如果不存在则返回None
        """
        if self.is_valid():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get("last_success")
            except Exception:
                pass
        return None
