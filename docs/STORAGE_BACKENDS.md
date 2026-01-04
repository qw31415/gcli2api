# 存储后端配置指南

GCLI2API 支持多种存储后端，按优先级自动选择：

```
Valkey/Redis > PostgreSQL > MongoDB > SQLite（默认）
```

---

## 快速配置

### Valkey / Redis（推荐用于高并发）

```bash
# Aiven Valkey
export VALKEY_URL="rediss://default:password@host:port"

# 或标准 Redis
export REDIS_URL="redis://localhost:6379"
```

**优势：**
- 内存存储，读写极快
- 适合高频配置读取
- 减少 PostgreSQL 连接压力

---

### PostgreSQL（推荐用于持久化）

```bash
# Aiven PostgreSQL
export POSTGRES_DSN="postgres://user:pass@host:port/db?sslmode=require"

# 本地 PostgreSQL
export POSTGRES_DSN="postgres://user:pass@localhost:5432/gcli"
```

**优势：**
- 可靠的持久化存储
- 支持复杂查询
- 适合生产环境

---

### MongoDB

```bash
export MONGODB_URI="mongodb://user:pass@host:port/dbname"
```

---

### SQLite（默认）

无需配置，自动使用本地文件：
- 凭证目录：`./creds/`
- 数据库文件：`./creds/credentials.db`

---

## 环境变量优先级

| 变量 | 存储后端 | 优先级 |
|------|---------|--------|
| `VALKEY_URL` 或 `REDIS_URL` | Valkey/Redis | 1 (最高) |
| `POSTGRES_DSN` | PostgreSQL | 2 |
| `MONGODB_URI` | MongoDB | 3 |
| （无）| SQLite | 4 (默认) |

---

## 混合架构建议

对于生产环境，推荐同时使用：

```bash
# 主存储：PostgreSQL（持久化凭证）
export POSTGRES_DSN="postgres://..."

# 缓存层：Valkey（高频配置读取）
export VALKEY_URL="rediss://..."
```

**注意：** 当前版本只使用一个后端。未来版本可能支持分层存储。

---

## 连接池配置

### PostgreSQL
- 最小连接：2
- 最大连接：10
- 建议 Aiven 免费版限制 20 连接

### Valkey/Redis
- 单连接复用
- 自动重连
- 超时：10秒

---

## 故障转移

如果首选后端初始化失败，会自动尝试下一个：

```
Valkey 失败 → 尝试 PostgreSQL → 尝试 MongoDB → 使用 SQLite
```

日志示例：
```
ERROR: Failed to initialize Valkey/Redis backend: Connection refused
INFO: Falling back to other backends...
INFO: Using PostgreSQL storage backend
```

---

## 数据迁移

### 从 SQLite 迁移到 PostgreSQL

```python
# 使用 Python 脚本迁移
import asyncio
from src.storage.sqlite_manager import SQLiteManager
from src.storage.postgres_manager import PostgresManager

async def migrate():
    sqlite = SQLiteManager()
    await sqlite.initialize()

    postgres = PostgresManager()
    await postgres.initialize()

    # 迁移凭证
    creds = await sqlite.get_all_credentials()
    await postgres.batch_store_credentials(creds)

    # 迁移配置
    config = await sqlite.get_all_config()
    for k, v in config.items():
        await postgres.set_config(k, v)

asyncio.run(migrate())
```

---

## 常见问题

### Q: Aiven PostgreSQL 连接失败？
确保 IP 在白名单中，使用 `sslmode=require`。

### Q: Valkey 连接超时？
检查 `rediss://`（带 SSL）vs `redis://`（无 SSL）。

### Q: 如何查看当前使用的后端？
访问 `/api/auth/storage-info` 或查看启动日志。
