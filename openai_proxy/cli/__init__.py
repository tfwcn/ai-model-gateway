"""
命令行工具 - 与 /admin/config、/admin/usage 接口共用同一套服务

模块划分：
    parser     参数树与取值解析
    output     输出（表格 / JSON）
    config_commands  配置类命令
    usage_commands   用量与审计命令
    main       入口与派发
"""
from openai_proxy.cli.main import CliApp, main

__all__ = ["CliApp", "main"]
