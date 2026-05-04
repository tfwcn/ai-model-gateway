"""
工具调用能力测试集成测试

测试完整的工具调用能力测试流程：
1. 缓存管理器功能
2. 能力测试器功能
3. 插件集成功能
"""

import asyncio
import sys
import os
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from openai_proxy.model.capability.cache import CapabilityCacheManager
from openai_proxy.model.capability.tester import ToolCapabilityTester


async def test_capability_cache():
    """测试能力缓存管理器"""
    print("\n=== 测试 1: 能力缓存管理器 ===")
    
    cache = CapabilityCacheManager(cache_file="data/test_capability.json")
    
    # 测试保存
    test_data = {
        "test-model-1": {
            "supports_tools": True,
            "tested_at": "2026-05-04T08:00:00Z",
            "platform": "test"
        },
        "test-model-2": {
            "supports_tools": False,
            "tested_at": "2026-05-04T08:00:01Z",
            "platform": "test"
        }
    }
    
    result = cache.save(test_data, platform="test")
    print(f"✓ 保存结果: {result}")
    
    # 测试加载
    loaded = cache.load()
    if loaded:
        print(f"✓ 加载成功，模型数量: {len(loaded.get('models', {}))}")
        print(f"✓ 版本: {loaded.get('version')}")
    else:
        print("✗ 加载失败")
    
    # 测试获取单个模型能力
    capability = cache.get_model_capability("test-model-1")
    print(f"✓ test-model-1 能力: {capability}")
    
    # 清理测试文件
    cache.clear()
    print("✓ 测试文件已清理")
    

async def test_capability_tester():
    """测试能力测试器（使用 ModelScope API）"""
    print("\n=== 测试 2: 能力测试器 ===")
    
    api_key = os.getenv("MODELSCOPE_API_KEY")
    if not api_key:
        print("⚠ MODELSCOPE_API_KEY 未设置，跳过实际API测试")
        return
    
    tester = ToolCapabilityTester(
        base_url="https://api-inference.modelscope.cn/v1",
        api_key=api_key,
        timeout=5
    )
    
    # 测试单个模型（DeepSeek-V4-Flash，已知支持工具调用）
    print("\n测试已知支持工具调用的模型: deepseek-ai/DeepSeek-V4-Flash")
    result = await tester.test_single_model("deepseek-ai/DeepSeek-V4-Flash", "modelscope")
    print(f"✓ 测试结果: {result} (期望: True)")
    
    # 测试单个模型（Qwen3.5，已知不支持工具调用）
    print("\n测试已知不支持工具调用的模型: Qwen/Qwen3.5-397B-A17B")
    result = await tester.test_single_model("Qwen/Qwen3.5-397B-A17B", "modelscope")
    print(f"✓ 测试结果: {result} (期望: False)")
    
    # 测试并行测试
    print("\n测试并行测试框架")
    models = [
        "deepseek-ai/DeepSeek-V4-Flash",
        "Qwen/Qwen3.5-397B-A17B"
    ]
    results = await tester.test_models_concurrently(models, max_concurrent=2, platform="modelscope")
    print(f"✓ 并行测试结果:")
    for model_id, supports in results.items():
        print(f"  - {model_id}: {supports}")


async def test_plugin_integration():
    """测试插件集成（ModelScope）"""
    print("\n=== 测试 3: 插件集成 ===")
    
    try:
        from plugin.modelscope import ModelScopePlugin
        
        api_key = os.getenv("MODELSCOPE_API_KEY")
        if not api_key:
            print("⚠ MODELSCOPE_API_KEY 未设置，跳过插件测试")
            return
        
        # 创建插件实例
        plugin = ModelScopePlugin(
            api_key=api_key,
            plugin_config={
                'args': {
                    'enable_tool_capability_test': True,
                    'max_concurrent_tests': 5,
                    'test_timeout_seconds': 5
                }
            }
        )
        
        print(f"✓ 插件初始化成功")
        print(f"✓ 能力测试启用: {plugin.enable_tool_capability_test}")
        print(f"✓ 最大并发数: {plugin.max_concurrent_tests}")
        print(f"✓ 测试超时: {plugin.test_timeout_seconds}秒")
        
        # 检查能力测试器是否初始化
        if plugin.capability_tester:
            print("✓ 能力测试器已初始化")
        else:
            print("✗ 能力测试器未初始化")
        
        # 检查能力缓存是否初始化
        if plugin.capability_cache:
            print("✓ 能力缓存管理器已初始化")
        else:
            print("✗ 能力缓存管理器未初始化")
            
    except Exception as e:
        print(f"✗ 插件集成测试失败: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """运行所有测试"""
    print("=" * 60)
    print("工具调用能力测试 - 集成测试")
    print("=" * 60)
    
    try:
        # 测试 1: 缓存管理器
        await test_capability_cache()
        
        # 测试 2: 能力测试器
        await test_capability_tester()
        
        # 测试 3: 插件集成
        await test_plugin_integration()
        
        print("\n" + "=" * 60)
        print("✅ 所有测试完成！")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ 测试过程中出现错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
