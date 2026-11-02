#!/usr/bin/env python3
"""
AI Model Gateway 管理命令行

用法示例（示例平台/示例模型，替换成你自己的配置）：
    python manage.py config document
    python manage.py config show example-platform
    python manage.py platform set example-platform --field weight --value 5
    python manage.py model set example-platform model-a --field first_chunk_timeout --value 45
    python manage.py model add example-platform model-new --dry-run
    python manage.py alias set alias-a --selector all --selector "example-platform|model-a"
    python manage.py audit set --field retention_days --value 45
    python manage.py config backups
    python manage.py config rollback models.yaml.20260101T000000Z.bak
    python manage.py usage summary --group-by model --since 24h
    python manage.py usage models --sort p95
    python manage.py usage attempts --outcome failure
    python manage.py usage chain <gateway_request_id>

所有会写盘的动作都支持 --dry-run 先看差异；写盘前一定会先备份当前文件。
"""
import sys

from openai_proxy.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
