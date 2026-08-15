# -*- coding: utf-8 -*-
"""LLM 客户端（OpenAI 兼容）—— llm_client

从 llm.py 拆出（M1，见 docs/REFACTOR_DESIGN.md §4）：HTTP 调用层。
配置默认唯一来源 setting/llm.ini（settings.llm_config，无代码内兜底、
无环境变量通道——README「架构」约定 2）；tool_choice="required" 的端点
支持探测缓存（4xx 自动回退 auto，首次探测后不再尝试）。

编排（call_llm：全局串行锁 + 通用 LLM 调用日志 + 空响应重试）在 llm
模块；本模块不依赖 llm / llm_prompt / llm_parse——客户端与组装/解析解耦。
"""

try:
    import requests
except ImportError:  # 允许在无 requests 环境下做离线自测
    requests = None

import settings


class ChatError(Exception):
    """LLM 调用链异常（配置缺失/网络失败/API 4xx/响应格式异常）。"""


def load_llm_config() -> dict:
    """LLM 配置的唯一生效位置：setting/llm.ini（settings 模块）。

    不提供任何代码内兜底：文件缺失、格式损坏、必填字段（base_url / api_key /
    model）缺失时直接抛出 ChatError，避免「写了却不生效」的歧义。
    temperature / max_tokens / reasoning / reasoning_effort 缺失时使用默认值。
    """
    try:
        return settings.llm_config()
    except settings.ConfigError as exc:
        raise ChatError(str(exc))


# tool_choice="required" 的端点支持探测缓存：
# None=未探测；False=端点不支持（已 4xx 回退 auto，不再尝试）。
_TOOL_CHOICE_REQUIRED_OK = None


class ChatClient:
    def __init__(self, llm_cfg: dict = None):
        llm_cfg = llm_cfg or load_llm_config()
        self.base_url = llm_cfg["base_url"].rstrip("/")
        self.api_key = llm_cfg["api_key"]
        self.model = llm_cfg["model"]
        self.temperature = float(llm_cfg.get("temperature", 0.95))
        self.max_tokens = int(llm_cfg.get("max_tokens", 1024))
        self.reasoning = bool(llm_cfg.get("reasoning", True))
        self.reasoning_effort = str(llm_cfg.get("reasoning_effort", "low"))

    @property
    def endpoint(self) -> str:
        base = self.base_url
        if not base.endswith("/v1"):
            base += "/v1"
        return base + "/chat/completions"

    def chat(self, messages: list, timeout: int = 180, tools: list = None,
             tool_choice: str = None) -> dict:
        """调用 OpenAI 兼容 API，返回完整响应 dict。

        tools 非空时携带原生 function calling 参数；tool_choice 覆盖默认
        tool_choice（None = 端点默认 auto）；模型可能同时输出 content 与
        tool_calls（say 台词 / update_state 状态 / rmgame 查询），
        由 llm_parse.parse_llm_response 解析。
        """
        global _TOOL_CHOICE_REQUIRED_OK
        if requests is None:
            raise ChatError("未安装 requests，无法调用 API：pip install requests")
        if not self.api_key:
            raise ChatError(
                "未配置 API Key。请在 setting/llm.ini 的 api_key 中填写。"
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # DeepSeek 思考模式开关：thinking.type 取 enabled/disabled
            # （顶层布尔 reasoning 不是 DeepSeek API 参数，会被静默忽略）
            "thinking": {"type": "enabled" if self.reasoning else "disabled"},
            "reasoning_effort": self.reasoning_effort,
            "stream": False,
        }
        if tools:
            if isinstance(tools, dict):
                tools = [tools]
            payload["tools"] = tools
            tc = tool_choice
            if tc == "required" and _TOOL_CHOICE_REQUIRED_OK is False:
                tc = "auto"          # 端点不支持 required（已探测），回退 auto
            payload["tool_choice"] = tc or "auto"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(2):
            try:
                resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=timeout)
            except requests.exceptions.RequestException as exc:
                last_err = ChatError(f"网络请求失败: {exc}")
                continue
            if resp.status_code == 200:
                return resp.json()
            if (resp.status_code == 400 and payload.get("tool_choice") == "required"
                    and _TOOL_CHOICE_REQUIRED_OK is None):
                # 端点可能不支持 required：标记并降级 auto 重发一次（同请求内）
                _TOOL_CHOICE_REQUIRED_OK = False
                payload["tool_choice"] = "auto"
                try:
                    resp = requests.post(self.endpoint, json=payload,
                                         headers=headers, timeout=timeout)
                except requests.exceptions.RequestException as exc:
                    raise ChatError(f"网络请求失败: {exc}")
                if resp.status_code == 200:
                    return resp.json()
            if 400 <= resp.status_code < 500:
                raise ChatError(f"API 返回 {resp.status_code}: {resp.text[:300]}")
            last_err = ChatError(f"API 返回 {resp.status_code}: {resp.text[:300]}")
        raise last_err or ChatError("未知错误")

    def chat_with_retry(self, messages: list, timeout: int = 180,
                        tools: list = None, tool_choice: str = None) -> dict:
        """带自动重试的调用：响应既无台词也无工具调用时，追加修复指令重试一次。"""
        resp = self.chat(messages, timeout=timeout, tools=tools,
                         tool_choice=tool_choice)
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ChatError(f"响应格式异常: {str(resp)[:300]}")
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
            retry_msgs = messages + [
                {"role": "user",
                 "content": "（你刚才没有给出任何回应。请调用 say 工具说出你的台词；"
                            "如需更新状态则调用 update_state 工具。）"}]
            resp = self.chat(retry_msgs, timeout=timeout, tools=tools,
                             tool_choice=tool_choice)
        return resp


# ---------------------------------------------------------------------------
# 离线自测（无需 API Key；用临时 ini 与 fake requests，不依赖真实配置/网络）
# ---------------------------------------------------------------------------

def selftest() -> None:
    import tempfile
    from pathlib import Path

    # LLM 配置唯一生效位置：setting/llm.ini（无代码内兜底、无环境变量通道）
    import data as data_mod
    assert not hasattr(data_mod, "DEFAULT_LLM_CONFIG"), "data.py 不应再有 LLM 默认配置"
    assert not hasattr(data_mod, "resolve_api_key"), "环境变量通道应已移除"
    assert not hasattr(data_mod, "CONFIG"), "data.py 不应再有运行配置（已迁 setting/app.ini）"
    # 用临时 ini 验证配置加载；文件缺失 → 明确报错（不静默兜底）
    _td = tempfile.mkdtemp(prefix="llm_client_selftest_")
    _tmp_ini = Path(_td) / "llm.ini"
    _tmp_ini.write_text(
        "[llm]\nbase_url = https://example.com/v1\napi_key = k\nmodel = m\n"
        "temperature = 0.95\nmax_tokens = 1024\nreasoning = true\n"
        "reasoning_effort = low\n",
        encoding="utf-8")
    cfg = settings.llm_config(path=_tmp_ini)
    assert cfg["base_url"].startswith("http"), cfg
    assert cfg["model"], cfg
    assert 0 < float(cfg["temperature"]) <= 2, cfg
    try:
        settings.llm_config(path=Path(_td) / "llm_config_不存在.ini")
        raise AssertionError("缺失配置文件应抛出 ConfigError")
    except settings.ConfigError as exc:
        assert "LLM 配置文件" in str(exc), exc
    print("  LLM 配置唯一来源: setting/llm.ini ✓（无兜底/无环境变量）")

    # 推理参数：配置读取→客户端传递一致（值跟随 setting/llm.ini，不锁具体值）
    assert isinstance(cfg["reasoning"], bool), cfg
    assert isinstance(cfg["reasoning_effort"], str) and cfg["reasoning_effort"], cfg
    client = ChatClient(llm_cfg=cfg)
    assert client.reasoning == cfg["reasoning"], "reasoning 应原样传递到客户端"
    assert client.reasoning_effort == cfg["reasoning_effort"], \
        "reasoning_effort 应原样传递到客户端"
    # 缺省值：reasoning 缺省 true、reasoning_effort 缺省 low（不依赖用户配置）
    with tempfile.TemporaryDirectory() as _td2:
        _tmp_ini2 = Path(_td2) / "llm.ini"
        _tmp_ini2.write_text(
            "[llm]\nbase_url = https://example.com/v1\napi_key = k\nmodel = m\n",
            encoding="utf-8")
        _dflt = settings.llm_config(path=_tmp_ini2)
        assert _dflt["reasoning"] is True and _dflt["reasoning_effort"] == "low", _dflt

    # 推理内容与缓存统计提取（实现位于 llm_parse，此处验证调用链离线行为）
    from llm_parse import _cache_stats, extract_reasoning
    assert extract_reasoning({"choices": [{"message": {"content": "x"}}]}) == ""
    assert extract_reasoning({"choices": [{"message": {"reasoning_content": "  思考中  "}}]}) == "思考中"
    assert extract_reasoning({"choices": [{"message": {"reasoning": "r"}}]}) == "r"
    assert extract_reasoning({}) == "" and extract_reasoning("bad") == ""
    cs = _cache_stats({"usage": {"prompt_tokens": 100,
                                 "prompt_cache_hit_tokens": 30,
                                 "prompt_cache_miss_tokens": 70}})
    assert cs == "缓存命中: hit=30 miss=70 命中率=30.0% (总 100)", cs
    cs2 = _cache_stats({"usage": {"prompt_tokens": 100,
                                  "prompt_tokens_details": {"cached_tokens": 25}}})
    assert cs2 == "缓存命中: hit=25 miss=75 命中率=25.0% (总 100)", cs2
    assert _cache_stats({"usage": {"prompt_tokens": 50}}) is None, "无缓存字段不应记录"
    assert _cache_stats({}) is None and _cache_stats("bad") is None
    assert _cache_stats(None) is None

    # 请求 payload 键（DeepSeek 文档）：thinking.type 控制思考模式开关，
    # 顶层布尔 reasoning 不是 API 参数；用 fake requests 捕获实际发送的 payload
    captured = {}

    class _FakePost:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "x"}}]}

    class _FakeRequests:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _FakePost()

    saved_requests = globals().get("requests")
    globals()["requests"] = _FakeRequests
    try:
        # 用构造配置独立验证 payload 映射（不依赖用户 setting/llm.ini 的具体值）：
        # reasoning=true→thinking.enabled、false→disabled、effort 原样透传
        ChatClient(llm_cfg={**cfg, "reasoning": True,
                            "reasoning_effort": "medium"}).chat(
            [{"role": "user", "content": "hi"}])
        p = captured["payload"]
        assert "reasoning" not in p, "顶层布尔 reasoning 不是 DeepSeek 参数，应删除"
        assert p["thinking"] == {"type": "enabled"}, p
        assert p["reasoning_effort"] == "medium", p
        ChatClient(llm_cfg={**cfg, "reasoning": False}).chat(
            [{"role": "user", "content": "hi"}])
        assert captured["payload"]["thinking"] == {"type": "disabled"}, \
            captured["payload"]
        ChatClient(llm_cfg={**cfg, "reasoning_effort": "low"}).chat(
            [{"role": "user", "content": "hi"}])
        assert captured["payload"]["reasoning_effort"] == "low", captured["payload"]
        # tool_choice：required 透传；auto 默认
        client.chat([{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "say"}}],
                    tool_choice="required")
        assert captured["payload"]["tool_choice"] == "required", captured["payload"]
        client.chat([{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "say"}}],
                    tool_choice="auto")
        assert captured["payload"]["tool_choice"] == "auto", captured["payload"]
    finally:
        if saved_requests is None:
            globals().pop("requests", None)
        else:
            globals()["requests"] = saved_requests
    # ChatError：可捕获、可传递（call_llm 链路统一异常类型）
    try:
        raise ChatError("test")
    except ChatError as exc:
        assert str(exc) == "test"
    print("  推理参数: payload thinking=enabled/disabled（按 reasoning 映射，无顶层 reasoning）✓ | "
          "effort 透传 ✓ | extract_reasoning ✓ | "
          "tool_choice=required/auto 透传 ✓ | ChatError ✓")


if __name__ == "__main__":
    selftest()
