# OpenCOCKeeper

> 大语言模型驱动的《克苏鲁的呼唤》7版跑团守秘人（Keeper）AI

---

## 免责声明

> **Call of Cthulhu (克苏鲁的呼唤)** is a Trademark of Chaosium Inc.
>
> This project is a **Fan Work** created under Chaosium's [Fan Use Policy](https://www.chaosium.com/fan-use-and-licensing/). It is not an official product and is not endorsed by Chaosium Inc.
>
> 本项目遵循 Chaosium 的爱好者使用政策。OpenCOCKeeper 仅提供**跑团辅助系统的代码逻辑**，不自带任何《克苏鲁的呼唤》规则书原文或官方模组数据。使用者需自行导入合法的规则数据。

---

## 特性

- **AI 守秘人自主裁决**：主 Agent 通过 Function Calling 工具闭环，自主决定何时查阅模组、检索记忆、执行 d100 检定。
- **决策与演播分离**：Director（导演）只做规则判定与剧情走向，输出《叙事决策大纲》；Narrator（润色 Agent）不参与任何规则推演，专注克苏鲁氛围的文学演播。
- **物理真相 + 语义记忆双轨**：HP/SAN/背包等硬数值由 SQLite 严格掌控，基于 `state_diff` 增量秒级回档；长程剧情由 RAG 向量记忆维护，支持按轮次精确撤销。
- **多世界绝对隔离**：以 `world_id` 为唯一隔离维度，可并行运行多个模组世界；支持角色卡（xlsx）导入、开场演播、结团归档与全量存档/恢复。
- **WebUI 调试控制台**：浏览器交互跑团 + 实时 trace 可视化 + 数据编辑器 + 提示词在线热编辑。

---

## 快速开始

**前置**：Python 3.11+，[uv](https://docs.astral.sh/uv/) 包管理器。

```bash
# 1. 安装依赖
uv sync

# 2. 生成配置文件
#    config.yaml（业务配置，可提交）；providers.ini（敏感 API Key，已被 gitignore）
cp template/config.yaml.template config.yaml
cp template/providers.ini.template providers.ini
#    编辑 providers.ini，填入模型提供方的 base_url 与 api_key

# 3.（可选）启用真实 RAG 记忆后端（默认使用 FakeMemory 免向量库速跑）
uv add --optional rag mem0ai

# 4.（可选）安装检索增强依赖（jieba 分词 / xlsx 角色卡 / pdf / docx 模组解析）
uv sync --extra retrieval

# 5. 放置模组原文
#    将 PDF / Markdown 模组文件放入 data/modules/

# 6.（可选）离线生成规则库 data/rules/*.md（用于 search_rule 规则检索）
uv run python scripts/build_rules_from_chm.py

# 7. 启动
uv run main.py
```

启动后浏览器打开 `http://127.0.0.1:12954`（端口可在 `config.yaml` 的 `webui` 段调整）。

> **Windows 提示**：`cp` 对应 `copy`（或 PowerShell 的 `Copy-Item`）；也可直接使用 `.venv\Scripts\python.exe` 代替 `uv run python`。

---

## 核心设计

| 概念 | 说明 |
|---|---|
| **决策与演播分离** | 主 Agent（Director）只做逻辑裁决与剧情走向，输出《叙事决策大纲》；润色 Agent（Narrator）只把大纲渲染成沉浸式文本，两者使用不同模型档位（裁决 low temperature / 演播较高 temperature）。 |
| **物理真相与语义记忆双轨** | 硬数值（HP/SAN/背包/技能）由 SQLite 掌控，每轮记录 `state_diff` 增量，取反即秒级回档；剧情事实由 Mem0 RAG 维护，按 `turn_num` 物理删除实现双侧同步撤销。 |
| **世界绝对隔离** | 消息来源仅在适配器层映射为 `world_id`，下游存储 / Agent / 工具严格基于 world_id 隔离，多世界并行互不干扰。 |
| **原子工具闭环** | 主 Agent 经 Function Calling 自主调用原子工具（查模组 / 查记忆 / 查规则 / 检定改状态 / 管理 Tag），信息不足即检索、信息充足即收敛交卷，永不凭空脑补。 |

---

## 技术栈

- **语言 / 环境**：Python 3.11+，uv 依赖管理
- **LLM**：OpenAI 兼容异步客户端（`call_llm` / 流式 / Function Calling），多模型档位可配
- **存储**：SQLite（单库多世界）+ mem0ai / Qdrant 本地向量库（语义记忆）
- **检索**：纯 Python BM25+ 全文检索（模组原文 + 规则库，jieba 可选分词）
- **规则内核**：纯函数 d100 检定 / 奖惩骰 / 理智与耐久（CoC 7th 规则）
- **接入与 UI**：aiohttp + WebSocket（适配器层）；纯前端 HTML / CSS / JS WebUI

---

## License

[Apache License 2.0](LICENSE)

---

## 致谢

- [Chaosium Inc.](https://www.chaosium.com/) - 创造了精彩的 Call of Cthulhu 游戏
- [Google DeepMind Concordia](https://github.com/google-deepmind/concordia) - 多智能体架构灵感来源
- [ChatRPG v2](https://arxiv.org/abs/2210.03620) - 理论基础参考
- [Google Gemini](https://gemini.google.com/) - 提供代码编写协助与结对编程支持
- [COC7thChm](https://github.com/COCchm/COC7thChm) - 整理了 COC7 版规则文件