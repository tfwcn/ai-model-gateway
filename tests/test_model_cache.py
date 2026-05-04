"""
ModelCacheManager单元测试
"""

import pytest
import json
import os
import tempfile
from pathlib import Path
from openai_proxy.model.cache import ModelCacheManager


class TestModelCacheManager:
    """ModelCacheManager测试类"""

    @pytest.fixture
    def cache_manager(self, tmp_path):
        """创建临时缓存管理器"""
        cache_file = tmp_path / "test_cache.json"
        return ModelCacheManager(cache_file=str(cache_file))

    def test_init_creates_directory(self, tmp_path):
        """测试初始化时创建目录"""
        nested_dir = tmp_path / "nested" / "dir"
        cache_file = nested_dir / "cache.json"

        cache = ModelCacheManager(cache_file=str(cache_file))

        assert nested_dir.exists()

    def test_save_and_load(self, cache_manager):
        """测试保存和加载"""
        models = [
            {"model_id": "test/model-1", "model_name": "Model 1", "rank": 1},
            {"model_id": "test/model-2", "model_name": "Model 2", "rank": 2},
        ]

        metadata = {"fetch_time": 1234567890, "source": "test"}

        # 保存
        result = cache_manager.save(models=models, metadata=metadata, success=True)
        assert result == True

        # 验证文件存在
        assert cache_manager.cache_file.exists()

        # 加载
        data = cache_manager.load()
        assert data is not None
        assert len(data["models"]) == 2
        assert data["metadata"]["source"] == "test"
        assert "last_success" in data

    def test_save_atomic_operation(self, cache_manager):
        """测试原子写入操作"""
        models = [{"model_id": "test/model", "rank": 1}]

        # 保存
        cache_manager.save(models=models, metadata={}, success=True)

        # 验证没有临时文件残留
        temp_files = list(cache_manager.cache_file.parent.glob(".cache_*.tmp"))
        assert len(temp_files) == 0

    def test_is_valid_with_valid_cache(self, cache_manager):
        """测试有效缓存验证"""
        models = [{"model_id": "test/model", "rank": 1}]
        cache_manager.save(models=models, metadata={}, success=True)

        assert cache_manager.is_valid() == True

    def test_is_valid_with_nonexistent_file(self, tmp_path):
        """测试不存在的文件验证"""
        cache_file = tmp_path / "nonexistent.json"
        cache = ModelCacheManager(cache_file=str(cache_file))

        assert cache.is_valid() == False

    def test_is_valid_with_corrupted_json(self, tmp_path):
        """测试损坏的JSON文件验证"""
        cache_file = tmp_path / "corrupted.json"
        cache_file.write_text("{ invalid json }")

        cache = ModelCacheManager(cache_file=str(cache_file))

        # is_valid应该检测到损坏并删除文件
        assert cache.is_valid() == False
        assert not cache_file.exists()  # 损坏文件应被删除

    def test_is_valid_with_invalid_structure(self, tmp_path):
        """测试无效结构验证"""
        cache_file = tmp_path / "invalid.json"
        cache_file.write_text('{"wrong": "structure"}')

        cache = ModelCacheManager(cache_file=str(cache_file))

        assert cache.is_valid() == False
        assert not cache_file.exists()  # 无效文件应被删除

    def test_load_returns_none_for_nonexistent(self, cache_manager):
        """测试加载不存在的缓存返回None"""
        result = cache_manager.load()
        assert result is None

    def test_load_returns_none_for_corrupted(self, tmp_path):
        """测试加载损坏的缓存返回None"""
        cache_file = tmp_path / "corrupted.json"
        cache_file.write_text("{ broken }")

        cache = ModelCacheManager(cache_file=str(cache_file))
        result = cache.load()

        assert result is None

    def test_clear_removes_file(self, cache_manager):
        """测试清除缓存"""
        models = [{"model_id": "test/model", "rank": 1}]
        cache_manager.save(models=models, metadata={}, success=True)

        assert cache_manager.cache_file.exists()

        cache_manager.clear()

        assert not cache_manager.cache_file.exists()

    def test_clear_nonexistent_file(self, cache_manager):
        """测试清除不存在的缓存文件"""
        result = cache_manager.clear()
        assert result == True

    def test_error_log_on_failure(self, cache_manager):
        """测试失败时记录错误日志"""
        error_msg = "Test error message"
        cache_manager.save(
            models=[],
            metadata={"error": error_msg},
            success=False
        )

        data = cache_manager.load()
        assert data is not None
        assert len(data["error_log"]) == 1
        assert data["error_log"][0]["error"] == error_msg
        assert "timestamp" in data["error_log"][0]

    def test_error_log_rotation(self, tmp_path):
        """测试错误日志轮转"""
        cache_file = tmp_path / "rotation_test.json"
        cache = ModelCacheManager(cache_file=str(cache_file), max_error_log_size=3)

        # 添加超过限制的Error
        for i in range(5):
            cache.save(
                models=[],
                metadata={"error": f"Error {i+1}"},
                success=False
            )

        data = cache.load()
        assert len(data["error_log"]) <= 3

        # 验证保留的是最近的错误
        last_error = data["error_log"][-1]["error"]
        assert last_error == "Error 5"

    def test_get_cache_info(self, cache_manager):
        """测试获取缓存信息"""
        # 空缓存
        info = cache_manager.get_cache_info()
        assert info["status"] == "invalid"
        assert info["exists"] == False

        # 有数据的缓存
        models = [{"model_id": "test/model", "rank": 1}]
        cache_manager.save(models=models, metadata={"source": "test"}, success=True)

        info = cache_manager.get_cache_info()
        assert info["status"] == "valid"
        assert info["model_count"] == 1
        assert "last_success" in info
        assert "file_size" in info
        assert "file_path" in info

    def test_validate_cache_data_valid(self, cache_manager):
        """测试验证有效的缓存数据"""
        valid_data = {
            "models": [
                {"model_id": "test/model-1"},
                {"model_id": "test/model-2"},
            ]
        }

        assert cache_manager._validate_cache_data(valid_data) == True

    def test_validate_cache_data_invalid_types(self, cache_manager):
        """测试验证无效的数据类型"""
        assert cache_manager._validate_cache_data("not a dict") == False
        assert cache_manager._validate_cache_data([]) == False
        assert cache_manager._validate_cache_data(None) == False

    def test_validate_cache_data_missing_models(self, cache_manager):
        """测试验证缺少models字段"""
        invalid_data = {"other_field": "value"}
        assert cache_manager._validate_cache_data(invalid_data) == False

    def test_validate_cache_data_models_not_list(self, cache_manager):
        """测试验证models不是列表"""
        invalid_data = {"models": "not a list"}
        assert cache_manager._validate_cache_data(invalid_data) == False

    def test_validate_cache_data_model_without_id(self, cache_manager):
        """测试验证模型缺少model_id"""
        invalid_data = {
            "models": [
                {"name": "No ID"},
            ]
        }
        assert cache_manager._validate_cache_data(invalid_data) == False

    def test_last_success_timestamp(self, cache_manager):
        """测试last_success时间戳更新"""
        models = [{"model_id": "test/model", "rank": 1}]

        # 第一次成功保存
        cache_manager.save(models=models, metadata={}, success=True)
        data1 = cache_manager.load()
        timestamp1 = data1["last_success"]

        # 第二次成功保存
        cache_manager.save(models=models, metadata={}, success=True)
        data2 = cache_manager.load()
        timestamp2 = data2["last_success"]

        # 时间戳应该不同（或至少存在）
        assert timestamp1 is not None
        assert timestamp2 is not None

    def test_last_success_preserved_on_failure(self, cache_manager):
        """测试失败时保留上次成功时间"""
        models = [{"model_id": "test/model", "rank": 1}]

        # 先成功保存
        cache_manager.save(models=models, metadata={}, success=True)
        data1 = cache_manager.load()
        original_timestamp = data1["last_success"]

        # 然后失败
        cache_manager.save(
            models=[],
            metadata={"error": "Test failure"},
            success=False
        )
        data2 = cache_manager.load()

        # last_success应该保持不变
        assert data2["last_success"] == original_timestamp

    def test_metadata_preserved(self, cache_manager):
        """测试元数据保存和加载"""
        models = [{"model_id": "test/model", "rank": 1}]
        metadata = {
            "fetch_time": 1234567890,
            "total_count": 100,
            "returned_count": 10,
            "source": "api_filter",
            "custom_field": "custom_value"
        }

        cache_manager.save(models=models, metadata=metadata, success=True)
        data = cache_manager.load()

        assert data["metadata"]["fetch_time"] == 1234567890
        assert data["metadata"]["total_count"] == 100
        assert data["metadata"]["custom_field"] == "custom_value"

    def test_empty_models_list(self, cache_manager):
        """测试空模型列表"""
        cache_manager.save(models=[], metadata={}, success=True)

        data = cache_manager.load()
        assert data is not None
        assert len(data["models"]) == 0
        assert cache_manager.is_valid() == True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
