"""
PostgreSQL 存储管理器
"""

import os
import time
import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from log import log


def _parse_jsonb(value: Any, default: Any = None) -> Any:
    """安全解析 JSONB 字段，处理字符串或已解析的值"""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return value


class PostgresManager:
    """PostgreSQL 数据库管理器"""

    STATE_FIELDS = {
        "error_codes",
        "disabled",
        "last_success",
        "user_email",
        "model_cooldowns",
    }

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._config_cache: Dict[str, Any] = {}
        self._config_loaded = False

    async def initialize(self) -> None:
        """初始化 PostgreSQL 连接池"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                postgres_dsn = os.getenv("POSTGRES_DSN")
                if not postgres_dsn:
                    raise ValueError("POSTGRES_DSN environment variable not set")

                self._pool = await asyncpg.create_pool(
                    postgres_dsn,
                    min_size=2,
                    max_size=10,
                    command_timeout=60,
                    ssl="require" if "sslmode=require" in postgres_dsn else None
                )

                await self._create_tables()
                await self._load_config_cache()

                self._initialized = True
                log.info("PostgreSQL storage initialized")

            except Exception as e:
                log.error(f"Error initializing PostgreSQL: {e}")
                raise

    async def _create_tables(self):
        """创建数据库表"""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS credentials (
                    id SERIAL PRIMARY KEY,
                    filename TEXT UNIQUE NOT NULL,
                    credential_data JSONB NOT NULL,
                    disabled BOOLEAN DEFAULT FALSE,
                    error_codes JSONB DEFAULT '[]'::jsonb,
                    last_success DOUBLE PRECISION,
                    user_email TEXT,
                    model_cooldowns JSONB DEFAULT '{}'::jsonb,
                    rotation_order INTEGER DEFAULT 0,
                    call_count INTEGER DEFAULT 0,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS antigravity_credentials (
                    id SERIAL PRIMARY KEY,
                    filename TEXT UNIQUE NOT NULL,
                    credential_data JSONB NOT NULL,
                    disabled BOOLEAN DEFAULT FALSE,
                    error_codes JSONB DEFAULT '[]'::jsonb,
                    last_success DOUBLE PRECISION,
                    user_email TEXT,
                    model_cooldowns JSONB DEFAULT '{}'::jsonb,
                    rotation_order INTEGER DEFAULT 0,
                    call_count INTEGER DEFAULT 0,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                )
            """)

            await conn.execute("CREATE INDEX IF NOT EXISTS idx_cred_disabled ON credentials(disabled)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_cred_rotation ON credentials(rotation_order)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ag_disabled ON antigravity_credentials(disabled)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ag_rotation ON antigravity_credentials(rotation_order)")

            log.debug("PostgreSQL tables created")

    async def _load_config_cache(self):
        """加载配置到内存缓存"""
        if self._config_loaded:
            return

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("SELECT key, value FROM config")
                for row in rows:
                    self._config_cache[row["key"]] = row["value"]

            self._config_loaded = True
            log.debug(f"Loaded {len(self._config_cache)} config items into cache")

        except Exception as e:
            log.error(f"Error loading config cache: {e}")
            self._config_cache = {}

    async def close(self) -> None:
        """关闭连接池"""
        if self._pool:
            await self._pool.close()
            self._pool = None
        self._initialized = False
        log.debug("PostgreSQL storage closed")

    def _ensure_initialized(self):
        """确保已初始化"""
        if not self._initialized:
            raise RuntimeError("PostgreSQL manager not initialized")

    def _get_table_name(self, is_antigravity: bool) -> str:
        return "antigravity_credentials" if is_antigravity else "credentials"

    async def get_next_available_credential(
        self, is_antigravity: bool = False, model_key: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """随机获取一个可用凭证"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            current_time = time.time()

            async with self._pool.acquire() as conn:
                if model_key:
                    # 获取所有启用的凭证，随机排序后找第一个不在冷却中的
                    rows = await conn.fetch(f"""
                        SELECT filename, credential_data, model_cooldowns
                        FROM {table}
                        WHERE disabled = FALSE
                        ORDER BY RANDOM()
                    """)

                    for r in rows:
                        cooldowns = _parse_jsonb(r["model_cooldowns"], {})
                        cooldown = cooldowns.get(model_key)
                        if cooldown is None or current_time >= cooldown:
                            return r["filename"], _parse_jsonb(r["credential_data"], {})
                    return None
                else:
                    row = await conn.fetchrow(f"""
                        SELECT filename, credential_data
                        FROM {table}
                        WHERE disabled = FALSE
                        ORDER BY RANDOM()
                        LIMIT 1
                    """)

                    if row:
                        return row["filename"], _parse_jsonb(row["credential_data"], {})

                return None

        except Exception as e:
            log.error(f"Error getting next available credential: {e}")
            return None

    async def get_available_credentials_list(self, is_antigravity: bool = False) -> List[str]:
        """获取所有可用凭证列表"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(f"""
                    SELECT filename FROM {table}
                    WHERE disabled = FALSE
                    ORDER BY rotation_order
                """)
                return [row["filename"] for row in rows]

        except Exception as e:
            log.error(f"Error getting available credentials list: {e}")
            return []

    async def store_credential(self, filename: str, credential_data: Dict[str, Any], is_antigravity: bool = False) -> bool:
        """存储或更新凭证（自动按 refresh_token 去重）"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            current_ts = time.time()
            refresh_token = credential_data.get("refresh_token", "")

            async with self._pool.acquire() as conn:
                # 按 refresh_token 去重：检查是否已存在相同 token 的凭证
                if refresh_token:
                    dup = await conn.fetchrow(f"""
                        SELECT id, filename FROM {table}
                        WHERE credential_data->>'refresh_token' = $1
                    """, refresh_token)
                    if dup and dup["filename"] != filename:
                        log.debug(f"Skipped duplicate: {filename} (same token as {dup['filename']})")
                        return False

                existing = await conn.fetchrow(f"SELECT id FROM {table} WHERE filename = $1", filename)

                if existing:
                    await conn.execute(f"""
                        UPDATE {table}
                        SET credential_data = $1, updated_at = $2
                        WHERE filename = $3
                    """, json.dumps(credential_data), current_ts, filename)
                else:
                    max_order = await conn.fetchval(f"SELECT COALESCE(MAX(rotation_order), -1) + 1 FROM {table}")
                    await conn.execute(f"""
                        INSERT INTO {table} (filename, credential_data, rotation_order, last_success, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $5)
                    """, filename, json.dumps(credential_data), max_order, current_ts, current_ts)

                log.debug(f"Stored credential: {filename}")
                return True

        except Exception as e:
            log.error(f"Error storing credential {filename}: {e}")
            return False

    async def get_credential(self, filename: str, is_antigravity: bool = False) -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(f"SELECT credential_data FROM {table} WHERE filename = $1", filename)
                if row:
                    return _parse_jsonb(row["credential_data"], {})

                row = await conn.fetchrow(f"SELECT credential_data FROM {table} WHERE filename LIKE '%' || $1", filename)
                if row:
                    return _parse_jsonb(row["credential_data"], {})

                return None

        except Exception as e:
            log.error(f"Error getting credential {filename}: {e}")
            return None

    async def list_credentials(self, is_antigravity: bool = False) -> List[str]:
        """列出所有凭证文件名"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(f"SELECT filename FROM {table} ORDER BY rotation_order")
                return [row["filename"] for row in rows]

        except Exception as e:
            log.error(f"Error listing credentials: {e}")
            return []

    async def delete_credential(self, filename: str, is_antigravity: bool = False) -> bool:
        """删除凭证"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            async with self._pool.acquire() as conn:
                result = await conn.execute(f"DELETE FROM {table} WHERE filename = $1", filename)
                deleted = result.split()[-1]

                if deleted == "0":
                    result = await conn.execute(f"DELETE FROM {table} WHERE filename LIKE '%' || $1", filename)
                    deleted = result.split()[-1]

                if int(deleted) > 0:
                    log.debug(f"Deleted credential: {filename}")
                    return True
                return False

        except Exception as e:
            log.error(f"Error deleting credential {filename}: {e}")
            return False

    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any], is_antigravity: bool = False) -> bool:
        """更新凭证状态"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            valid_updates = {k: v for k, v in state_updates.items() if k in self.STATE_FIELDS}

            if not valid_updates:
                return True

            set_clauses = []
            values = []
            idx = 1

            for key, value in valid_updates.items():
                if key in ("error_codes", "model_cooldowns"):
                    set_clauses.append(f"{key} = ${idx}::jsonb")
                    values.append(json.dumps(value))
                else:
                    set_clauses.append(f"{key} = ${idx}")
                    values.append(value)
                idx += 1

            set_clauses.append(f"updated_at = ${idx}")
            values.append(time.time())
            idx += 1
            values.append(filename)

            async with self._pool.acquire() as conn:
                result = await conn.execute(f"""
                    UPDATE {table}
                    SET {', '.join(set_clauses)}
                    WHERE filename = ${idx}
                """, *values)

                updated = result.split()[-1]
                return int(updated) > 0

        except Exception as e:
            log.error(f"Error updating credential state {filename}: {e}")
            return False

    async def get_credential_state(self, filename: str, is_antigravity: bool = False) -> Dict[str, Any]:
        """获取凭证状态"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(f"""
                    SELECT disabled, error_codes, last_success, user_email, model_cooldowns
                    FROM {table} WHERE filename = $1
                """, filename)

                if row:
                    return {
                        "disabled": row["disabled"] or False,
                        "error_codes": _parse_jsonb(row["error_codes"], []),
                        "last_success": row["last_success"] or time.time(),
                        "user_email": row["user_email"],
                        "model_cooldowns": _parse_jsonb(row["model_cooldowns"], {}),
                    }

                return {
                    "disabled": False,
                    "error_codes": [],
                    "last_success": time.time(),
                    "user_email": None,
                    "model_cooldowns": {},
                }

        except Exception as e:
            log.error(f"Error getting credential state {filename}: {e}")
            return {}

    async def get_all_credential_states(self, is_antigravity: bool = False) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(f"""
                    SELECT filename, disabled, error_codes, last_success, user_email, model_cooldowns
                    FROM {table}
                """)

                states = {}
                current_time = time.time()

                for row in rows:
                    filename = row["filename"]
                    model_cooldowns = _parse_jsonb(row["model_cooldowns"], {})

                    if model_cooldowns:
                        model_cooldowns = {k: v for k, v in model_cooldowns.items() if v > current_time}

                    states[filename] = {
                        "disabled": row["disabled"] or False,
                        "error_codes": _parse_jsonb(row["error_codes"], []),
                        "last_success": row["last_success"] or time.time(),
                        "user_email": row["user_email"],
                        "model_cooldowns": model_cooldowns,
                    }

                return states

        except Exception as e:
            log.error(f"Error getting all credential states: {e}")
            return {}

    async def get_credentials_summary(
        self,
        offset: int = 0,
        limit: Optional[int] = None,
        status_filter: str = "all",
        is_antigravity: bool = False,
        error_code_filter: Optional[str] = None,
        cooldown_filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """获取凭证摘要"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)

            async with self._pool.acquire() as conn:
                stats_rows = await conn.fetch(f"SELECT disabled, COUNT(*) as cnt FROM {table} GROUP BY disabled")
                global_stats = {"total": 0, "normal": 0, "disabled": 0}
                for row in stats_rows:
                    global_stats["total"] += row["cnt"]
                    if row["disabled"]:
                        global_stats["disabled"] = row["cnt"]
                    else:
                        global_stats["normal"] = row["cnt"]

                where_clauses = []
                if status_filter == "enabled":
                    where_clauses.append("disabled = FALSE")
                elif status_filter == "disabled":
                    where_clauses.append("disabled = TRUE")

                where_clause = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

                rows = await conn.fetch(f"""
                    SELECT filename, disabled, error_codes, last_success, user_email, rotation_order, model_cooldowns
                    FROM {table}
                    {where_clause}
                    ORDER BY rotation_order
                """)

                all_summaries = []
                current_time = time.time()

                for row in rows:
                    error_codes = _parse_jsonb(row["error_codes"], [])

                    if error_code_filter and str(error_code_filter).strip().lower() != "all":
                        filter_value = str(error_code_filter).strip()
                        try:
                            filter_int = int(filter_value)
                        except ValueError:
                            filter_int = None

                        match = False
                        for code in error_codes:
                            if code == filter_value or code == filter_int:
                                match = True
                                break
                        if not match:
                            continue

                    model_cooldowns = _parse_jsonb(row["model_cooldowns"], {})
                    active_cooldowns = {k: v for k, v in model_cooldowns.items() if v > current_time} if model_cooldowns else {}

                    summary = {
                        "filename": row["filename"],
                        "disabled": row["disabled"] or False,
                        "error_codes": error_codes,
                        "last_success": row["last_success"] or current_time,
                        "user_email": row["user_email"],
                        "rotation_order": row["rotation_order"],
                        "model_cooldowns": active_cooldowns,
                    }

                    if cooldown_filter == "in_cooldown":
                        if active_cooldowns:
                            all_summaries.append(summary)
                    elif cooldown_filter == "no_cooldown":
                        if not active_cooldowns:
                            all_summaries.append(summary)
                    else:
                        all_summaries.append(summary)

                total_count = len(all_summaries)
                if limit is not None:
                    summaries = all_summaries[offset:offset + limit]
                else:
                    summaries = all_summaries[offset:]

                return {
                    "items": summaries,
                    "total": total_count,
                    "offset": offset,
                    "limit": limit,
                    "stats": global_stats,
                }

        except Exception as e:
            log.error(f"Error getting credentials summary: {e}")
            return {
                "items": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "stats": {"total": 0, "normal": 0, "disabled": 0},
            }

    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置"""
        self._ensure_initialized()

        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO config (key, value, updated_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = $3
                """, key, json.dumps(value), time.time())

            self._config_cache[key] = value
            return True

        except Exception as e:
            log.error(f"Error setting config {key}: {e}")
            return False

    async def reload_config_cache(self):
        """重新加载配置缓存"""
        self._ensure_initialized()
        self._config_loaded = False
        await self._load_config_cache()
        log.info("Config cache reloaded from database")

    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置"""
        self._ensure_initialized()
        return self._config_cache.get(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        self._ensure_initialized()
        return self._config_cache.copy()

    async def delete_config(self, key: str) -> bool:
        """删除配置"""
        self._ensure_initialized()

        try:
            async with self._pool.acquire() as conn:
                await conn.execute("DELETE FROM config WHERE key = $1", key)

            self._config_cache.pop(key, None)
            return True

        except Exception as e:
            log.error(f"Error deleting config {key}: {e}")
            return False

    async def set_model_cooldown(
        self,
        filename: str,
        model_key: str,
        cooldown_until: Optional[float],
        is_antigravity: bool = False
    ) -> bool:
        """设置模型冷却时间"""
        self._ensure_initialized()

        try:
            table = self._get_table_name(is_antigravity)
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(f"SELECT model_cooldowns FROM {table} WHERE filename = $1", filename)

                if not row:
                    log.warning(f"Credential {filename} not found")
                    return False

                model_cooldowns = _parse_jsonb(row["model_cooldowns"], {})

                if cooldown_until is None:
                    model_cooldowns.pop(model_key, None)
                else:
                    model_cooldowns[model_key] = cooldown_until

                await conn.execute(f"""
                    UPDATE {table}
                    SET model_cooldowns = $1::jsonb, updated_at = $2
                    WHERE filename = $3
                """, json.dumps(model_cooldowns), time.time(), filename)

                log.debug(f"Set model cooldown: {filename}, model_key={model_key}")
                return True

        except Exception as e:
            log.error(f"Error setting model cooldown for {filename}: {e}")
            return False
