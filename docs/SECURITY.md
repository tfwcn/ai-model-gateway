# 🚨 安全注意事项

AI Model Gateway 的安全最佳实践。

## 📋 目录

- [API 密钥管理](#api-密钥管理)
- [访问控制](#访问控制)
- [网络安全](#网络安全)
- [数据安全](#数据安全)
- [审计与监控](#审计与监控)

---

## API 密钥管理

### ✅ 正确做法

**使用环境变量：**

```bash
# .env 文件（不要提交到 Git）
MODELSCOPE_API_KEY=your-key-here
OPENROUTER_API_KEY=your-key-here
```

```yaml
# models.yaml
modelscope:
  apiKey: "${MODELSCOPE_API_KEY}"  # 从环境变量读取
```

**使用 Kubernetes Secrets：**

```bash
kubectl create secret generic ai-model-gateway-secrets \
  --from-literal=MODELSCOPE_API_KEY=your-key
```

**使用 Docker Secrets：**

```bash
echo "your-api-key" | docker secret create modelscope_key -
```

### ❌ 错误做法

**不要硬编码密钥：**

```yaml
# ❌ 绝对不要这样做！
modelscope:
  apiKey: "sk-1234567890abcdef"  # 泄露风险！
```

**不要提交到版本控制：**

```bash
# 确保 .env 在 .gitignore 中
echo ".env" >> .gitignore
```

---

## 访问控制

### 网络隔离

**Docker 网络：**

```yaml
services:
  ai-model-gateway:
    networks:
      - internal-network  # 仅内部访问

networks:
  internal-network:
    driver: bridge
    internal: true  # 禁止外部访问
```

**Kubernetes NetworkPolicy：**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ai-model-gateway-policy
spec:
  podSelector:
    matchLabels:
      app: ai-model-gateway
  ingress:
    - from:
        - podSelector:
            matchLabels:
              role: frontend
      ports:
        - protocol: TCP
          port: 8000
```

### API 认证

未来版本将支持 API 密钥认证：

```yaml
security:
  api_key_auth:
    enabled: true
    header: "X-API-Key"
```

---

## 网络安全

### TLS/SSL 加密

**生产环境必须启用 HTTPS：**

```nginx
# Nginx 反向代理配置
server {
    listen 443 ssl;
    server_name gateway.example.com;
    
    ssl_certificate /etc/ssl/certs/gateway.crt;
    ssl_certificate_key /etc/ssl/private/gateway.key;
    
    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

**Let's Encrypt 免费证书：**

```bash
certbot --nginx -d gateway.example.com
```

### 速率限制

防止滥用和 DDoS 攻击：

```yaml
rate_limiting:
  enabled: true
  requests_per_minute: 60
  burst_size: 10
```

或使用 Nginx：

```nginx
limit_req_zone $binary_remote_addr zone=api:10m rate=1r/s;

location / {
    limit_req zone=api burst=10 nodelay;
    proxy_pass http://localhost:8000;
}
```

---

## 数据安全

### 日志脱敏

确保日志中不包含敏感信息：

```python
# 自定义日志过滤器
import logging

class SensitiveDataFilter(logging.Filter):
    def filter(self, record):
        # 移除 API 密钥
        record.msg = record.msg.replace(os.getenv('MODELSCOPE_API_KEY'), '***')
        return True

logger.addFilter(SensitiveDataFilter())
```

### 缓存安全

**Redis 加密连接：**

```yaml
cache:
  type: "redis"
  redis_url: "rediss://localhost:6379"  # rediss:// 表示 SSL
  ssl_cert_reqs: "required"
```

**缓存数据加密（未来功能）：**

```yaml
cache:
  encryption:
    enabled: true
    algorithm: "AES-256-GCM"
```

---

## 审计与监控

### 访问日志

记录所有 API 请求：

```yaml
logging:
  access_log:
    enabled: true
    format: "%(asctime)s %(remote_addr)s %(method)s %(path)s %(status)s"
    file: "/var/log/ai-model-gateway/access.log"
```

### 异常检测

监控异常行为：

```bash
# 检测高频请求
curl http://localhost:8000/metrics | grep http_requests_total | sort -rn | head -10

# 检测频繁故障转移
curl http://localhost:8000/metrics | grep model_failover_total
```

### 告警规则

Prometheus AlertManager 配置：

```yaml
groups:
  - name: security-alerts
    rules:
      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "高错误率检测到"
          
      - alert: SuspiciousActivity
        expr: rate(http_requests_total[1m]) > 100
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "可疑的高频访问"
```

---

## 安全检查清单

部署前请确认：

- [ ] API 密钥通过环境变量或 Secrets 管理
- [ ] `.env` 文件已添加到 `.gitignore`
- [ ] 生产环境启用了 HTTPS
- [ ] 配置了速率限制
- [ ] 启用了访问日志
- [ ] 配置了监控和告警
- [ ] 定期更新依赖包
- [ ] 进行了安全扫描

### 安全扫描工具

```bash
# Python 依赖安全扫描
pip install safety
safety check

# 代码安全扫描
pip install bandit
bandit -r openai_proxy/

# Docker 镜像扫描
docker scan tfwcn/ai-model-gateway:latest
```

---

## 应急响应

### 密钥泄露处理

如果 API 密钥泄露：

1. **立即撤销密钥**
   - 登录对应平台控制台
   - 撤销泄露的 API 密钥
   - 生成新密钥

2. **更新配置**
   ```bash
   # 更新 .env 文件
   nano .env
   
   # 重启服务
   docker-compose restart ai-model-gateway
   ```

3. **审计日志**
   ```bash
   # 检查是否有未授权访问
   grep "unauthorized" /var/log/ai-model-gateway/access.log
   ```

4. **通知相关人员**
   - 团队内部通知
   - 如有必要，通知平台方

---

## 相关文档

- [配置指南](./CONFIGURATION_GUIDE.md)
- [部署指南](./DEPLOYMENT.md)
- [监控与运维](./MONITORING.md)
