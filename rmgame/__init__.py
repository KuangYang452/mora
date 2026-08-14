# -*- coding: utf-8 -*-
"""rmgame —— RPG Maker 游戏文本点评工具（角色侧扩展）

M0 范围：游戏发现（discovery）+ 文本提取（extract）。
设计文档：docs/RPG_MAKER_TOOL.md（内部文档，不入库）。

职责分层：
- discovery.py  扫描硬盘 → 游戏库注册（runtime/games.json）
- extract.py    Data/*.json → raw/（默认对话，可选全量）
- cli.py        命令行入口（scan/extract/status/selftest）
- rewriter.py   （M1 起）LLM 重写管线
- wiki.py       （M1 起）wiki 目录/索引/引用管理
- monitor.py    （M3 起）守护进程：CDP + OCR
- bridge.py     （M5 起）角色工具执行体
"""

VERSION = "0.1.0"
