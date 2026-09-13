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

# 3.（可选）安装检索增强依赖（jieba 分词 / xlsx 角色卡 / pdf / docx 模组解析）
uv sync --extra retrieval

# 4. 放置模组原文
#    将 PDF / Markdown 模组文件放入 data/modules/

# 5.（可选）离线生成规则库 data/rules/*.md（用于 search_rule 规则检索）
uv run python scripts/build_rules_from_chm.py

# 6. 启动
uv run main.py
```

启动后浏览器打开 `http://127.0.0.1:12954`（端口可在 `config.yaml` 的 `webui` 段调整）。

> **Windows 提示**：`cp` 对应 `copy`（或 PowerShell 的 `Copy-Item`）；也可直接使用 `.venv\Scripts\python.exe` 代替 `uv run python`。

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
- [COC7thChm](https://github.com/COCchm/COC7thChm) - 整理了 COC7 版规则文件