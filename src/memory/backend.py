# -*- coding: utf-8 -*-
"""
@File     :   backend.py
@Desc     :   记忆 RAG 后端适配：MemoryBackend 协议 + 真实 Mem0 封装（user_id=world_id 隔离）
@Note     :   协议方法：add_events 写入原子事件、search_topk 召回、close 释放资源；
             Mem0Memory 惰性加载 mem0ai（首次 add/search 才 import 与构建），
             配置经 memory.rag 段 + model_tiers + providers.ini 拼装，缺项即抛 ConfigError
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from ..core.config import PROJECT_ROOT, ConfigError, Settings, get_settings
from ..core.exceptions import MemoryOperationError
from ..core.log import get_logger
from .interface import MemoryHit

logger = get_logger(__name__)


# ============================================
# 后端协议
# ============================================


class MemoryBackend(Protocol):
    """记忆后端统一契约，供 Memory 门面依赖注入。

    实现者需提供：
      - add_events:  把一批原子事件写入向量库（user_id 强制绑定 world_id）
      - search_topk: 带 world_id 过滤的 Top-K 语义召回
      - close:       释放底层连接（可选，幂等）
    """

    def add_events(
        self,
        events: List[Dict[str, Any]],
        *,
        world_id: str,
        batch_turn_nums: List[int],
        location: Optional[str] = None,
    ) -> None: ...

    def search_topk(
        self,
        query: str,
        *,
        world_id: str,
        top_k: int,
        since_turn: Optional[int] = None,
    ) -> List[MemoryHit]: ...

    def delete_since(
        self,
        world_id: str,
        turn_num: int,
    ) -> int: ...

    async def close(self) -> None: ...


# ============================================
# 真实 Mem0 后端
# ============================================

# qdrant 建集合需要向量维度，按 embedder 模型决定；bge-m3 为 1024 维
_DEFAULT_EMBED_DIMS = 1024


class Mem0Memory:
    """Mem0 RAG 后端：把原子事件存为 Mem0 记忆，user_id 映射 world_id 强制隔离。

    每个事件一条记忆，metadata 绑定轮次（turn_num）与地点（location），
    供后续按轮次召回 / 撤销同步精确剔除。
    若未安装 mem0ai，首次调用时抛清晰 ImportError 提示安装命令。
    """

    def __init__(
        self,
        memory: Optional[Any] = None,
        *,
        embed_dims: Optional[int] = None,
    ) -> None:
        self._memory = memory  # 懒加载：None 时按配置构建
        self._embed_dims = int(embed_dims or _DEFAULT_EMBED_DIMS)
        self._closed = False

    # ---- 构造 ----

    @classmethod
    def from_config(
        cls, settings: Optional[Settings] = None
    ) -> "Mem0Memory":
        """按配置构建后端；memory.rag 缺关键项时抛 ConfigError 给出明确修法。

        关键配置来源：memory.rag.embedder_model（嵌入模型）、
        memory.rag.embedder_provider（提供方，须在 providers.ini 有 base_url/api_key）、
        memory.rag.llm_tier（提炼/召回用的文本模型档位）。
        """
        settings = settings or get_settings()
        rag = settings.get("memory.rag") or {}

        embedder_model = rag.get("embedder_model")
        embedder_provider = rag.get("embedder_provider")
        if not embedder_model or not embedder_provider:
            raise ConfigError(
                "Mem0 后端缺少嵌入配置：请在 config.yaml 的 memory.rag 段设置 "
                "embedder_model 与 embedder_provider（提供方须支持 /v1/embeddings）"
            )
        provider_cfg = settings.get_provider(str(embedder_provider))
        if not provider_cfg or not provider_cfg.get("api_key"):
            raise ConfigError(
                f"嵌入提供方 '{embedder_provider}' 未在 providers.ini 配置 api_key"
            )

        # 构建实例并挂载配置，真正的 mem0.Memory 留到首次 add/search 时惰性构建
        instance = cls(embed_dims=int(rag.get("embedding_model_dims", _DEFAULT_EMBED_DIMS)))
        instance._rag_config = dict(rag)
        instance._embedder_model = str(embedder_model)
        instance._embedder_cfg = dict(provider_cfg)
        instance._llm_tier = str(rag.get("llm_tier", "standard"))
        return instance

    # ---- 懒加载真实 Mem0 ----

    def _mem0(self) -> Any:
        """首次调用时 import mem0ai 并按配置构建 mem0.Memory，之后复用。"""
        if self._memory is not None:
            return self._memory
        if self._closed:
            raise RuntimeError("Mem0Memory 已关闭，禁止继续写入或检索")
        try:
            from mem0 import Memory  # 状态：惰性导入，未安装时抛出
        except ImportError as e:  # noqa: BLE001
            raise ImportError(
                "使用真实 Mem0 后端需先安装: uv add --optional rag mem0ai"
            ) from e
        try:
            self._memory = Memory.from_config(self._build_mem0_config())
        except Exception as e:  # noqa: BLE001
            raise ConfigError(
                f"构建 Mem0 失败（请核对 memory.rag 配置与 mem0ai 版本）: {e}"
            ) from e
        return self._memory

    def _build_mem0_config(self) -> dict:
        """拼装 mem0.Memory.from_config 所需的组件配置。

        复用项目 model_tiers + providers.ini：LLM 走 OpenAI 兼容的文本模型档位，
        嵌入走同提供方的 /v1/embeddings 端点；mem0ai v2 中 base_url 字段名为
        openai_base_url，vector_store 用 qdrant 本地模式（path 指向 data 目录）。
        """
        settings = get_settings()
        model_cfg = settings.get_model_config(self._llm_tier)
        llm_provider = settings.get_provider(str(model_cfg.get("provider", "")))
        if not llm_provider:
            raise ConfigError(f"LLM 档位 '{self._llm_tier}' 的提供方未配置")

        # qdrant 本地模式数据目录：data/mem0/qdrant，避开默认 /tmp 在 Windows 的路径问题
        data_dir = PROJECT_ROOT / str(settings.get("storage.data_dir", "data"))
        local_path = str(data_dir / "mem0" / "qdrant")

        # embedder 配置：默认不传 embedding_dims。
        # 背景：mem0ai 只要配置了 embedding_dims 就会在 /v1/embeddings 请求里带上
        # dimensions 参数，而 SiliconFlow 等非 matryoshka 端点会以 400/20015 拒绝。
        # 需要按维度截断的端点（如 OpenAI text-embedding-3-small）可在
        # config.yaml memory.rag.pass_embedding_dims 开启后恢复。
        # 注意：qdrant 集合维度由下方 vector_store.embedding_model_dims 决定，与这里无关。
        embedder_config = {
            "model": self._embedder_model,
            "api_key": self._embedder_cfg.get("api_key"),
            "openai_base_url": self._embedder_cfg.get("base_url"),
        }
        if self._rag_config.get("pass_embedding_dims", False):
            embedder_config["embedding_dims"] = self._embed_dims

        return {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": model_cfg.get("model_name"),
                    "api_key": llm_provider.get("api_key"),
                    "openai_base_url": llm_provider.get("base_url"),
                    "temperature": model_cfg.get("temperature", 0.2),
                    "max_tokens": model_cfg.get("max_tokens", 2000),
                },
            },
            "embedder": {
                "provider": "openai",
                "config": embedder_config,
            },
            "vector_store": {
                "provider": self._rag_config.get("vector_store", "qdrant"),
                "config": {
                    "collection_name": self._rag_config.get(
                        "collection_name", "open_coc_keeper_mem"
                    ),
                    "embedding_model_dims": self._embed_dims,
                    "path": local_path,
                },
            },
        }

    # ---- 协议实现：写入 ----

    def add_events(
        self,
        events: List[Dict[str, Any]],
        *,
        world_id: str,
        batch_turn_nums: List[int],
        location: Optional[str] = None,
    ) -> None:
        """把一批原子事件写入 Mem0；每条 user_id=world_id，metadata 绑定轮次/地点。

        事件未自带 turn 时绑定本批最大轮次（撤销到该批内任一轮次即可整批剔除），
        宁可多删不残留，保证与 SQLite 回档语义一致。
        """
        default_turn = max(batch_turn_nums) if batch_turn_nums else None
        memory = self._mem0()
        for event in events:
            text = event.get("text")
            if not text:
                continue
            turn = event.get("turn") if event.get("turn") is not None else default_turn
            metadata: Dict[str, Any] = {"turn_num": turn}
            if location:
                metadata["location"] = location
            try:
                # 状态：写入单条记忆，user_id 承载世界隔离
                # infer=False：事件已是固化接口提炼过的原子文段，跳过 mem0 的 LLM 二次提取
                # （infer=True 对单条事实消息常返回空结果导致写入失败，且白耗 token）
                memory.add(
                    [{"role": "user", "content": str(text)}],
                    user_id=world_id,
                    metadata=metadata,
                    infer=False,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("写入 Mem0 失败(world=%s turn=%s): %s", world_id, turn, e)
                raise

    # ---- 协议实现：召回 ----

    def search_topk(
        self,
        query: str,
        *,
        world_id: str,
        top_k: int,
        since_turn: Optional[int] = None,
    ) -> List[MemoryHit]:
        """带 world_id 过滤的 Top-K 召回；since_turn 非空时按轮次下限过滤。"""
        memory = self._mem0()
        filters = {"user_id": world_id}
        if since_turn is not None:
            filters["AND"] = [{"turn_num": {"$gte": since_turn}}]
        try:
            result = memory.search(
                query, filters=filters, top_k=top_k
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Mem0 检索失败(world=%s): %s", world_id, e)
            raise
        hits: List[MemoryHit] = []
        for entry in result.get("results", []):
            meta = entry.get("metadata") or {}
            hits.append(
                MemoryHit(
                    text=str(entry.get("memory", "")),
                    turn_num=meta.get("turn_num"),
                    location=meta.get("location"),
                    score=float(entry.get("score", 0.0) or 0.0),
                    memory_id=entry.get("id"),
                )
            )
        return hits

    # ---- 协议实现：撤销 ----

    def delete_since(self, world_id: str, turn_num: int) -> int:
        """物理删除该世界 turn_num >= N 的记忆，返回预估删除条数。

        复用 mem0 内部已建立的 qdrant client（vector_store.client），
        绝不二次打开本地 path——同进程双重打开会触发文件锁冲突；
        count 仅用于日志/断言预估，与 delete 之间的并发写入会使两者略偏离。
        """
        store = self._mem0().vector_store  # 状态：复用既有连接，杜绝锁冲突
        from qdrant_client import models  # 惰性导入，避免模块级强依赖

        selector = models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id", match=models.MatchValue(value=world_id)
                    ),
                    models.FieldCondition(
                        key="turn_num", range=models.Range(gte=int(turn_num))
                    ),
                ]
            )
        )
        try:
            counted = store.client.count(
                collection_name=store.collection_name,
                count_filter=selector.filter,
            ).count
            store.client.delete(
                collection_name=store.collection_name,
                points_selector=selector,
            )
            logger.info(
                "Qdrant 回档删除 world=%s turn>=%s count≈%s",
                world_id, turn_num, counted,
            )
            return int(counted)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Qdrant 回档删除失败(world=%s turn>=%s): %s",
                world_id, turn_num, e,
            )
            raise MemoryOperationError(
                f"Qdrant 回档删除失败 world={world_id} turn>={turn_num}: {e}"
            ) from e

    # ---- 协议实现：释放 ----

    def ensure_ready(self) -> None:
        """触发真实后端组件构建（连接向量库、加载嵌入器），供前置自检使用。

        只做本地组件初始化，不发起网络请求；构建失败抛 ConfigError。
        """
        self._mem0()

    async def close(self) -> None:
        """置关闭标记并释放后端（真实连接断开由 mem0ai 自身管理，尽力而为）。"""
        self._closed = True
        self._memory = None
