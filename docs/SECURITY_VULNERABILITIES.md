# GCLI2API 原始安全漏洞报告

本文档记录了原仓库中存在的安全漏洞，这些漏洞可能导致凭证泄露。

---

## CRITICAL - 可直接获取凭证的漏洞

### 1. 硬编码 OAuth Client Secret

**位置:** `src/utils.py:20, 29`

```python
CLIENT_SECRET = "GOCSPX-..."  # 硬编码在源码中
ANTIGRAVITY_CLIENT_SECRET = "GOCSPX-..."  # 硬编码在源码中
```

**风险:** 任何能访问源码的人都可以获取 OAuth Client Secret，用于伪造认证请求。

---

### 2. API 端点直接返回完整凭证

**位置:**
- `GET /api/auth/creds/detail/{filename}` → `src/web_routes.py:909`
- `GET /api/auth/creds/download/{filename}` → `src/web_routes.py:1106`
- `GET /api/auth/creds/download-all` → `src/web_routes.py:1141`

**攻击方式:**
```bash
# 获取单个凭证详情（包含 refresh_token）
curl -H "Authorization: Bearer <panel_password>" \
     http://target/api/auth/creds/detail/credential-123.json

# 下载单个凭证文件
curl -H "Authorization: Bearer <panel_password>" \
     http://target/api/auth/creds/download/credential-123.json

# 批量下载所有凭证
curl -H "Authorization: Bearer <panel_password>" \
     http://target/api/auth/creds/download-all
```

**返回内容包含:**
- `access_token` - 访问令牌
- `refresh_token` - 刷新令牌（可无限续期）
- `client_id` / `client_secret` - OAuth 客户端凭证

---

### 3. OAuth 回调端点返回完整凭证

**位置:**
- `POST /api/auth/callback` → `src/web_routes.py:280`
- `POST /api/auth/callback-url` → `src/web_routes.py:331`

**风险:** OAuth 完成后，完整的凭证信息（包括 refresh_token 和 client_secret）直接返回给前端。

---

### 4. 配置 API 返回明文密码

**位置:** `GET /api/auth/config` → `src/web_routes.py:1198-1200`

```python
current_config["api_password"] = await config.get_api_password()
current_config["panel_password"] = await config.get_panel_password()
current_config["password"] = await config.get_server_password()
```

**攻击方式:**
```bash
curl -H "Authorization: Bearer <any_valid_token>" \
     http://target/api/auth/config
```

**风险:** 获取面板密码后可访问所有凭证下载接口。

---

## HIGH - 间接泄露凭证的漏洞

### 5. 密码写入日志文件

**位置:** `src/web_routes.py:1229, 1304, 1313-1315`

```python
log.debug(f"收到的password值: {new_config.get('password', 'NOT_FOUND')}")
log.debug(f"设置{key}字段为: {value}")  # 包含密码
log.debug(f"保存后立即读取的API密码: {test_api_password}")
```

**攻击方式:** 获取日志文件访问权限即可读取密码。

---

### 6. 请求头和认证信息写入日志

**位置:**
- `src/utils.py:404, 462, 468` - 认证失败时记录完整请求头
- `src/gemini_router.py:73-77, 193-197` - 记录请求头和 API Key
- `src/auth.py:454, 667` - 记录 access_token 前缀

**日志示例:**
```
DEBUG: 请求头: {'Authorization': 'Bearer sk-xxx...', ...}
DEBUG: API Key: AIza...
DEBUG: Token前缀: ya29.a0ARrdaM...
```

---

### 7. 面板认证 Token 即密码本身

**位置:**
- `src/web_routes.py:218` - 登录返回 `token: request.password`
- `src/utils.py:494` - 验证时直接比较 token 与密码

**风险:**
- Token 存储在 `localStorage`，易受 XSS 攻击
- Token 即密码，获取 token 等于获取密码

---

### 8. Access Token 放入 URL 查询参数

**位置:** `src/google_oauth_api.py:395`

```python
f".../tokeninfo?access_token={access_token}"
```

**风险:** Token 可能泄露到：
- 浏览器历史记录
- 服务器访问日志
- 代理服务器日志
- HTTP Referer 头

---

## 攻击链示例

### 场景 1: 从日志获取凭证

1. 获取服务器日志文件访问权限
2. 搜索 `password值:` 或 `API密码:` 获取面板密码
3. 使用密码调用 `/api/auth/creds/download-all` 下载所有凭证

### 场景 2: 从配置 API 获取凭证

1. 通过任意方式获取一个有效的面板 token
2. 调用 `GET /api/auth/config` 获取所有密码
3. 使用 panel_password 下载凭证

### 场景 3: XSS 攻击获取凭证

1. 在面板页面注入 XSS
2. 读取 `localStorage` 获取 token（即 panel_password）
3. 使用 token 调用凭证下载 API

---

## 建议修复措施

1. **移除硬编码密钥** - 使用环境变量
2. **凭证 API 脱敏** - 不返回 refresh_token 和 client_secret
3. **日志脱敏** - 移除所有密码和 token 的日志记录
4. **Token 与密码分离** - 使用 JWT 或随机 token，不要直接使用密码
5. **Token 安全存储** - 使用 HttpOnly Cookie 替代 localStorage
6. **URL 参数安全** - 使用 POST body 传递敏感信息

---

*报告生成时间: 2025-01*
*审计工具: Codex CLI*
