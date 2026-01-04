"""
Valkey/Redis 存储管理器
支持 Aiven Valkey 和标准 Redis 实例
"""

import os
import json
import asyncio
from typing import Any, Dict, List, Optional

import redis.asyncio as redis

from log import log


class ValkeyManager:
    """Valkey/Redis 存储管理器"""

    STATE_FIELDS = {
        "error_codes",
        "disabled",
        "last_success",
        "user_email",
        "model_cooldowns",
    }

    # Redis key prefixes
    KEY_CREDENTIALS = "gcli:creds:"
    KEY_ANTIGRAVITY = "gcli:ag_creds:"
    KEY_CONFIG = "gcli:config"
    KEY_CREDS_INDEX = "gcli:creds_index"
    KEY_AG_INDEX = "gcli:ag_creds_index"

    def __init__(self):
        self._client: Optional[redis.Redis] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._config_cache: Dict[str, Any] = {}
        self._config_loaded = False

    async def initialize(self) -> None:
        """初始化 Valkey/Redis 连接"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                valkey_url = os.getenv("VALKEY_URL") or os.getenv("REDIS_URL")
                if not valkey_url:
                    raise ValueError("VALKEY_URL or REDIS_URL environment variable not set")

                self._client = redis.from_url(
                    valkey_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_timeout=10,
                    socket_connect_timeout=10,
                )

                # Test connection
                await self._client.ping()

                self._initialized = True
                log.info("Valkey/Redis storage initialized successfully")

            except Exception as e:
                log.error(f"Failed to initialize Valkey/Redis: {e}")
                raise

    async def close(self) -> None:
        """关闭连接"""
        if self._client:
            await self._client.close()
            self._client = None
            self._initialized = False

    # ======================== 凭证操作 ========================

    async def get_all_credentials(self, is_antigravity: bool = False) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证"""
        if not self._initialized:
            await self.initialize()

        try:
            index_key = self.KEY_AG_INDEX if is_antigravity else self.KEY_CREDS_INDEX
            prefix = self.KEY_ANTIGRAVITY if is_antigravity else self.KEY_CREDENTIALS

            # Get all credential keys from index (convert to list for stable ordering)
            cred_keys = list(await self._client.smembers(index_key))
            if not cred_keys:
                return {}

            result = {}
            pipe = self._client.pipeline()

            for filename in cred_keys:
                pipe.hgetall(f"{prefix}{filename}")

            values = await pipe.execute()

            for filename, data in zip(cred_keys, values):
                if data:
                    # Parse all JSON fields
                    parsed = {}
                    for k, v in data.items():
                        try:
                            parsed[k] = json.loads(v)
                        except (json.JSONDecodeError, TypeError):
                            parsed[k] = v
                    result[filename] = parsed

            return result

        except Exception as e:
            log.error(f"Error getting all credentials: {e}")
            return {}

    async def store_credential(
        self, filename: str, creds_data: Dict[str, Any], is_antigravity: bool = False
    ) -> bool:
        """存储单个凭证（自动按 refresh_token 去重）"""
        if not self._initialized:
            await self.initialize()

        try:
            index_key = self.KEY_AG_INDEX if is_antigravity else self.KEY_CREDS_INDEX
            prefix = self.KEY_ANTIGRAVITY if is_antigravity else self.KEY_CREDENTIALS
            key = f"{prefix}{filename}"
            refresh_token = creds_data.get("refresh_token", "")

            # 按 refresh_token 去重
            if refresh_token:
                existing_files = list(await self._client.smembers(index_key))
                for existing_file in existing_files:
                    if existing_file == filename:
                        continue
                    existing_data = await self._client.hgetall(f"{prefix}{existing_file}")
                    if existing_data:
                        existing_token = existing_data.get("refresh_token", "")
                        if existing_token:
                            try:
                                existing_token = json.loads(existing_token)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        if existing_token == refresh_token:
                            log.debug(f"Skipped duplicate: {filename} (same token as {existing_file})")
                            return False

            # Serialize all types to JSON for consistent deserialization
            serialized = {}
            for k, v in creds_data.items():
                serialized[k] = json.dumps(v)

            # Store credential and add to index
            pipe = self._client.pipeline()
            pipe.hset(key, mapping=serialized)
            pipe.sadd(index_key, filename)
            await pipe.execute()

            log.debug(f"Stored credential: {filename} (antigravity={is_antigravity})")
            return True

        except Exception as e:
            log.error(f"Error storing credential {filename}: {e}")
            return False

    async def get_credential(self, filename: str, is_antigravity: bool = False) -> Optional[Dict[str, Any]]:
        """获取单个凭证"""
        if not self._initialized:
            await self.initialize()

        try:
            prefix = self.KEY_ANTIGRAVITY if is_antigravity else self.KEY_CREDENTIALS
            key = f"{prefix}{filename}"

            data = await self._client.hgetall(key)
            if not data:
                return None

            # Parse all JSON fields
            parsed = {}
            for k, v in data.items():
                try:
                    parsed[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    parsed[k] = v

            return parsed

        except Exception as e:
            log.error(f"Error getting credential {filename}: {e}")
            return None

    async def delete_credential(self, filename: str, is_antigravity: bool = False) -> bool:
        """删除凭证"""
        if not self._initialized:
            await self.initialize()

        try:
            index_key = self.KEY_AG_INDEX if is_antigravity else self.KEY_CREDS_INDEX
            prefix = self.KEY_ANTIGRAVITY if is_antigravity else self.KEY_CREDENTIALS
            key = f"{prefix}{filename}"

            pipe = self._client.pipeline()
            pipe.delete(key)
            pipe.srem(index_key, filename)
            await pipe.execute()

            log.debug(f"Deleted credential: {filename}")
            return True

        except Exception as e:
            log.error(f"Error deleting credential {filename}: {e}")
            return False

    async def update_credential_state(
        self, filename: str, state_data: Dict[str, Any], is_antigravity: bool = False
    ) -> bool:
        """更新凭证状态"""
        if not self._initialized:
            await self.initialize()

        try:
            prefix = self.KEY_ANTIGRAVITY if is_antigravity else self.KEY_CREDENTIALS
            key = f"{prefix}{filename}"

            # Only update state fields (use JSON for all values)
            updates = {}
            for field in self.STATE_FIELDS:
                if field in state_data:
                    updates[field] = json.dumps(state_data[field])

            if updates:
                await self._client.hset(key, mapping=updates)

            return True

        except Exception as e:
            log.error(f"Error updating credential state {filename}: {e}")
            return False

    async def batch_store_credentials(
        self, credentials_dict: Dict[str, Dict[str, Any]], is_antigravity: bool = False
    ) -> int:
        """批量存储凭证"""
        if not self._initialized:
            await self.initialize()

        success_count = 0
        for filename, creds_data in credentials_dict.items():
            if await self.store_credential(filename, creds_data, is_antigravity):
                success_count += 1

        return success_count

    # ======================== 配置操作 ========================

    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        if not self._initialized:
            await self.initialize()

        try:
            value = await self._client.hget(self.KEY_CONFIG, key)
            if value is None:
                return default

            # Try to parse as JSON
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value

        except Exception as e:
            log.error(f"Error getting config {key}: {e}")
            return default

    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置值"""
        if not self._initialized:
            await self.initialize()

        try:
            # Serialize value
            if isinstance(value, (dict, list)):
                serialized = json.dumps(value)
            elif isinstance(value, bool):
                serialized = json.dumps(value)
            else:
                serialized = str(value) if value is not None else ""

            await self._client.hset(self.KEY_CONFIG, key, serialized)

            # Update cache
            self._config_cache[key] = value

            return True

        except Exception as e:
            log.error(f"Error setting config {key}: {e}")
            return False

    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        if not self._initialized:
            await self.initialize()

        try:
            data = await self._client.hgetall(self.KEY_CONFIG)
            if not data:
                return {}

            result = {}
            for k, v in data.items():
                try:
                    result[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    result[k] = v

            self._config_cache = result.copy()
            self._config_loaded = True

            return result

        except Exception as e:
            log.error(f"Error getting all config: {e}")
            return {}

    async def reload_config_cache(self) -> None:
        """重新加载配置缓存"""
        await self.get_all_config()

    # ======================== 统计操作 ========================

    async def get_credentials_count(self, is_antigravity: bool = False) -> int:
        """获取凭证数量"""
        if not self._initialized:
            await self.initialize()

        try:
            index_key = self.KEY_AG_INDEX if is_antigravity else self.KEY_CREDS_INDEX
            return await self._client.scard(index_key)
        except Exception as e:
            log.error(f"Error getting credentials count: {e}")
            return 0

    async def get_active_credentials_count(self, is_antigravity: bool = False) -> int:
        """获取活跃（未禁用）凭证数量"""
        if not self._initialized:
            await self.initialize()

        try:
            all_creds = await self.get_all_credentials(is_antigravity)
            return sum(1 for c in all_creds.values() if not c.get("disabled", False))
        except Exception as e:
            log.error(f"Error getting active credentials count: {e}")
            return 0

    async def list_credentials(self, is_antigravity: bool = False) -> List[str]:
        """列出所有凭证文件名"""
        if not self._initialized:
            await self.initialize()

        try:
            index_key = self.KEY_AG_INDEX if is_antigravity else self.KEY_CREDS_INDEX
            return list(await self._client.smembers(index_key))
        except Exception as e:
            log.error(f"Error listing credentials: {e}")
            return []

    async def get_credential_state(self, filename: str, is_antigravity: bool = False) -> Dict[str, Any]:
        """获取凭证状态"""
        if not self._initialized:
            await self.initialize()

        try:
            cred = await self.get_credential(filename, is_antigravity)
            if not cred:
                return {}

            return {field: cred.get(field) for field in self.STATE_FIELDS if field in cred}
        except Exception as e:
            log.error(f"Error getting credential state {filename}: {e}")
            return {}

    async def get_all_credential_states(self, is_antigravity: bool = False) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        if not self._initialized:
            await self.initialize()

        try:
            all_creds = await self.get_all_credentials(is_antigravity)
            result = {}
            for filename, cred in all_creds.items():
                result[filename] = {
                    field: cred.get(field) for field in self.STATE_FIELDS if field in cred
                }
            return result
        except Exception as e:
            log.error(f"Error getting all credential states: {e}")
            return {}

    async def delete_config(self, key: str) -> bool:
        """删除配置项"""
        if not self._initialized:
            await self.initialize()

        try:
            await self._client.hdel(self.KEY_CONFIG, key)
            self._config_cache.pop(key, None)
            return True
        except Exception as e:
            log.error(f"Error deleting config {key}: {e}")
            return False
