# -*- coding: utf-8 -*-
"""rmgame —— RPG Maker 游戏文本点评工具（角色侧扩展）

职责分层（M4 依赖收敛后只向下，见 docs/REFACTOR_DESIGN.md §7）：
- 门面层  cli（命令行入口）/ facade（pet 门面）/ bridge（角色工具执行体）
- 特性层  arbitrate（wiki 仲裁）
- 中坚层  rewriter（LLM 重写管线）→ wiki（目录/索引/引用管理）
- 支撑层  summarizer（事件摘要）/ llmfmt（raw → LLM 友好格式）
- 运行时层 monitor（守护：CDP + OCR）→ cdp / ocr（通道提供者）
- 纯数据层 discovery（游戏库注册）/ extract（raw 提取）/ matcher（匹配）

设计文档：docs/RPG_MAKER_TOOL.md（内部文档，不入库）。
版本号唯一事实来源为 settings.VERSION（README「版本与发布」附录）；
本包不另设版本号。
"""
