#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Model Gateway - 支持多平台模型自动切换重试
面向对象重构版本 - 多文件架构
"""

from openai_proxy.service import OpenAIProxyService

# 创建服务实例
service = OpenAIProxyService()

# 创建FastAPI应用
app = service.create_app()
