# -*- coding: utf-8 -*-
"""轻量级配置模块。

新架构遵循"极简"原则，配置拆成两份文件：

- ``config.yaml``    : 业务配置（模型分级、存储路径、开关等，非敏感，可提交仓库）
- ``providers.ini``  : 敏感配置（API Key / Base URL 等，禁止提交仓库）

相比旧架构 ``glyphkeeper/src/tools/config.py`` 的简化：

- 去掉 pydantic 数据模型，改用普通 dict + 点号路径访问，减少依赖；
- providers（敏感）与 yaml（业务）分离存放，互不覆盖；
- 保留懒加载单例 ``get_settings()`` 与 ``reload_config()``。
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .exceptions import OpenCOCKeeperError

# ── 项目根目录：src/core/config.py -> 项目根 ──
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent

CONFIG_FILE: Path = PROJECT_ROOT / "config.yaml"
PROVIDERS_FILE: Path = PROJECT_ROOT / "providers.ini"


class ConfigError(OpenCOCKeeperError):
    """配置缺失或解析失败。"""


def _warn(msg: str) -> None:
    """日志系统初始化前的早期告警，直接输出到 stderr。"""
    sys.stderr.write(f"[Config] {msg}\n")


def _load_config_yaml() -> Dict[str, Any]:
    """读取 config.yaml，解析失败时返回空 dict 并告警。"""
    if not CONFIG_FILE.exists():
        _warn(f"未找到 {CONFIG_FILE}，将使用默认/空配置")
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        _warn(f"无法读取 config.yaml: {e}")
        return {}


def _load_providers_ini() -> Dict[str, Dict[str, str]]:
    """读取 providers.ini，返回 {节名小写: {选项: 值}}。"""
    if not PROVIDERS_FILE.exists():
        _warn(f"未找到 {PROVIDERS_FILE}，请从 template/providers.ini.template 复制并填入真实 Key")
        return {}
    parser = configparser.ConfigParser()
    try:
        parser.read(PROVIDERS_FILE, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _warn(f"无法读取 providers.ini: {e}")
        return {}
    return {
        section.lower(): {k: v for k, v in parser.items(section)}
        for section in parser.sections()
    }


class Settings:
    """极简配置容器：以 dict 为底层，支持点号路径读取。

    用法::

        s = get_settings()
        s.get("project.debug")            # -> True
        s.get("storage.data_dir")         # -> "data"
        s.get("不存在的键", "默认值")      # -> "默认值"
        s.get_model_config("smart")       # -> {"provider": ..., "model_name": ...}
        s.get_provider("silkflow")        # -> {"base_url": ..., "api_key": ...}
        s.get_api_key("silkflow")         # -> "..."
    """

    def __init__(
        self,
        data: Optional[Dict[str, Any]] = None,
        providers: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        object.__setattr__(self, "_data", data or {})
        object.__setattr__(self, "_providers", providers or {})

    # ── 通用读取 ──
    def get(self, dotted_path: str, default: Any = None) -> Any:
        """按点号路径读取配置，如 ``settings.get("project.debug")``。"""
        node: Any = self._data
        for part in dotted_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def get_model_config(self, tier: str) -> Dict[str, Any]:
        """读取指定层级的模型配置。"""
        model = self.get(f"model_tiers.{tier}")
        if not isinstance(model, dict):
            available = list((self.get("model_tiers") or {}).keys())
            raise ConfigError(f"未知的模型层级 '{tier}'，可用层级: {available}")
        return dict(model)

    # ── providers.ini 相关 ──
    @property
    def providers(self) -> Dict[str, Dict[str, str]]:
        """全部提供方配置（节名已小写化）。"""
        return self._providers

    def get_provider(self, name: str) -> Optional[Dict[str, str]]:
        """按名称获取提供方配置，名称不区分大小写。"""
        return self._providers.get(name.lower())

    def get_api_key(self, provider: str) -> Optional[str]:
        """获取指定提供方的 API Key。"""
        cfg = self.get_provider(provider)
        return cfg.get("api_key") if cfg else None


# ── 全局单例（懒加载） ──
_instance: Optional[Settings] = None


def load_config() -> Settings:
    """重新读取配置文件并返回新的 Settings 实例。"""
    return Settings(data=_load_config_yaml(), providers=_load_providers_ini())


def get_settings() -> Settings:
    """获取全局配置实例（懒加载单例）。"""
    global _instance
    if _instance is None:
        _instance = load_config()
    return _instance


def reload_config() -> Settings:
    """强制重新加载配置（修改配置文件后调用）。"""
    global _instance
    _instance = load_config()
    return _instance
