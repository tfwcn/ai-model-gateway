# 🐳 部署指南

AI Model Gateway 的 Docker 和 Kubernetes 部署说明。

## 📋 目录

- [Docker 部署](#docker-部署)
- [Docker Compose 部署](#docker-compose-部署)
- [Kubernetes 部署](#kubernetes-部署)
- [环境变量配置](#环境变量配置)
- [生产环境建议](#生产环境建议)

---

## Docker 部署

### 构建镜像

```bash
docker build -t ai-model-gateway:latest .
```

### 运行容器

```bash
docker run -d \
  --name ai-model-gateway \
  -p 8000:8000 \
  -v $(pwd)/models.yaml:/app/models.yaml:ro \
  -v $(pwd)/.env:/app/.env:ro \
  ai-model-gateway:latest
```

### 使用预构建镜像

```bash
docker pull tfwcn/ai-model-gateway:latest
docker run -d \
  --name ai-model-gateway \
  -p 8000:8000 \
  -v $(pwd)/models.yaml:/app/models.yaml:ro \
  -v $(pwd)/.env:/app/.env:ro \
  tfwcn/ai-model-gateway:latest
```

---

## Docker Compose 部署

### docker-compose.yml

```yaml
version: "3.8"

services:
  ai-model-gateway:
    build: .
    container_name: ai-model-gateway
    ports:
      - "8000:8000"
    volumes:
      - ./models.yaml:/app/models.yaml:ro
      - ./models.example.yaml:/app/models.example.yaml:ro
      - ./.env:/app/.env:ro
      - playwright-cache:/app/.cache/ms-playwright
    restart: unless-stopped
    networks:
      - proxy-network
    environment:
      - LOG_LEVEL=INFO

networks:
  proxy-network:
    driver: bridge

volumes:
  playwright-cache:
```

### 启动服务

```bash
docker-compose up -d
```

### 查看日志

```bash
docker-compose logs -f ai-model-gateway
```

---

## Kubernetes 部署

### Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-model-gateway
spec:
  replicas: 2
  selector:
    matchLabels:
      app: ai-model-gateway
  template:
    metadata:
      labels:
        app: ai-model-gateway
    spec:
      containers:
        - name: ai-model-gateway
          image: tfwcn/ai-model-gateway:latest
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: ai-model-gateway-secrets
          volumeMounts:
            - name: config
              mountPath: /app/models.yaml
              subPath: models.yaml
      volumes:
        - name: config
          configMap:
            name: ai-model-gateway-config
```

### Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: ai-model-gateway
spec:
  selector:
    app: ai-model-gateway
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8000
  type: ClusterIP
```

### ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ai-model-gateway-config
data:
  models.yaml: |
    modelscope:
      baseUrl: "https://api-inference.modelscope.cn/v1"
      weight: 10
      enabled: true
```

### Secret

```bash
kubectl create secret generic ai-model-gateway-secrets \
  --from-literal=MODELSCOPE_API_KEY=your-key \
  --from-literal=OPENROUTER_API_KEY=your-key \
  --from-literal=NVIDIA_API_KEY=your-key
```

### Prometheus ServiceMonitor

项目提供 K8s ServiceMonitor 配置：

```bash
kubectl apply -f k8s/servicemonitor.yaml
```

---

## 环境变量配置

### 必需的环境变量

```env
MODELSCOPE_API_KEY=your-modelscope-api-key
OPENROUTER_API_KEY=your-openrouter-api-key
NVIDIA_API_KEY=your-nvidia-api-key
```

### 可选的环境变量

```env
# 日志级别
LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL

# Redis 配置（用于缓存和会话管理）
REDIS_URL=redis://localhost:6379
RESPONSES_SESSION_TTL=86400

# 监控配置
PROMETHEUS_ENABLED=true
METRICS_PATH=/metrics
```

---

## 生产环境建议

### 1. 资源限制

```yaml
resources:
  requests:
    memory: "512Mi"
    cpu: "500m"
  limits:
    memory: "1Gi"
    cpu: "1000m"
```

### 2. 健康检查

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health/detailed
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 5
```

### 3. 自动扩缩容

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ai-model-gateway-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ai-model-gateway
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

### 4. 安全加固

- ✅ 使用 Kubernetes Secrets 管理 API 密钥
- ✅ 启用 NetworkPolicy 限制访问
- ✅ 使用 TLS 加密通信
- ✅ 定期更新镜像版本
- ✅ 启用 Pod Security Policies

### 5. 备份策略

```bash
# 备份配置文件
kubectl get configmap ai-model-gateway-config -o yaml > config-backup.yaml

# 备份密钥
kubectl get secret ai-model-gateway-secrets -o yaml > secrets-backup.yaml
```

---

## 故障排查

### 容器无法启动

```bash
# 查看容器日志
docker logs ai-model-gateway

# 检查配置文件
docker exec -it ai-model-gateway cat /app/models.yaml
```

### 性能问题

```bash
# 查看资源使用情况
docker stats ai-model-gateway

# 检查 Prometheus 指标
curl http://localhost:8000/metrics
```

### 网络连接问题

```bash
# 测试平台连接
docker exec -it ai-model-gateway curl https://api-inference.modelscope.cn/v1/models
```

---

## 相关文档

- [配置指南](./CONFIGURATION_GUIDE.md)
- [监控与运维](./MONITORING.md)
- [负载均衡策略](./LOAD_BALANCING.md)
