"""创作模型路由测试（task 路由 → 按模型名路由改造）。

背景（2026-09-13）：MaiBot 1.2.5 修复 #2031（model_name 被吞入任务名解析）后，
插件回退路线从「复用内部 task（``model=<任务名>`` 旧式写法）」改为「按模型名路由」
（``task_name="utils"`` + ``model_name=<已注册模型名>``）。后者可调用**只注册、
未分配任务**的模型（``get_model_info_by_name`` 查全局模型列表，不校验任务分配），
用户可在 WebUI 自行注册"无思考版"模型（``extra_params.thinking.disabled``）按名绑定。

直连路线（[creator_model]）保持不变：独立供应商/独立额度场景仍需它。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_creator_route.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_creator_route.py
"""

from __future__ import annotations

import asyncio
import logging as _stdlib_logging
import sys
from types import SimpleNamespace

import _synth_loader

_CREATOR = _synth_loader.load("services.creator")

CreatorClient = _CREATOR.CreatorClient


class _ListHandler(_stdlib_logging.Handler):
    """捕获日志消息，供断言告警内容。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def emit(self, record: _stdlib_logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _FakeLLM:
    """记录 generate 载荷的假 llm 能力。"""

    def __init__(self, *, available=None, raise_on_generate=None) -> None:
        self.calls: list = []
        self.availability_queries = 0
        self._available = list(available or [])
        self._raise = raise_on_generate

    async def generate(self, prompt, **kwargs):
        self.calls.append(dict(kwargs))
        if self._raise is not None:
            raise self._raise
        return {"response": "生成结果"}

    async def get_available_models(self):
        self.availability_queries += 1
        return list(self._available)


class _FakeTelemetry:
    """记录 record_llm_tokens 调用的假 telemetry（成本采样锁定用）。"""

    def __init__(self) -> None:
        self.token_calls: list = []

    def record_llm_tokens(self, tokens: float, task: str = "creation") -> None:
        self.token_calls.append((tokens, task))


def _make_client(
    *,
    creator_enabled: bool = False,
    base_url: str = "",
    creation_model: str = "",
    available=None,
    raise_on_generate=None,
) -> tuple:
    plugin = SimpleNamespace(
        config=SimpleNamespace(
            creator_model=SimpleNamespace(
                enabled=creator_enabled,
                base_url=base_url,
                api_key="",
                model_id="direct-model",
                max_tokens=384,
                timeout_seconds=5,
            ),
            llm=SimpleNamespace(
                creation_task="learner",  # 旧字段：保留用于验证新代码不再使用
                creation_model=creation_model,
                temperature=0.9,
            ),
        ),
        ctx=SimpleNamespace(
            llm=_FakeLLM(available=available, raise_on_generate=raise_on_generate),
            logger=_stdlib_logging.getLogger("creator-route-test"),
        ),
    )
    telemetry = _FakeTelemetry()
    plugin._telemetry = telemetry
    client = CreatorClient(plugin)
    handler = _ListHandler()
    plugin.ctx.logger.addHandler(handler)
    return client, plugin.ctx.llm, handler, telemetry


# ===== 回归用例 =====


def test_direct_route_used_when_configured():
    """直连条件满足 → 走直连，不触发 llm.generate。"""
    client, llm, _handler, _telemetry = _make_client(
        creator_enabled=True, base_url="https://api.example.com"
    )
    direct_calls: list = []

    async def fake_direct(prompt):
        direct_calls.append(prompt)
        return "直连结果"

    client._generate_direct = fake_direct
    result = asyncio.run(client.generate("提示"))

    assert result == "直连结果"
    assert direct_calls == ["提示"]
    assert llm.calls == [], "直连生效时不应再走 llm.generate"


def test_model_name_route_payload():
    """直连关闭 + 已配模型名 → 载荷为 task_name="utils" + model_name=配置值。"""
    client, llm, _handler, _telemetry = _make_client(
        creation_model="my-thinking-off-model",
        available=["my-thinking-off-model", "other-model"],
    )
    result = asyncio.run(client.generate("提示"))

    assert result == "生成结果"
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call.get("task_name") == "utils", "应显式指定任务名（1.2.5 新语义）"
    assert call.get("model_name") == "my-thinking-off-model", "应按名指定模型"
    assert "model" not in call, "不得再用旧式 model= 传任务名"
    assert call.get("temperature") == 0.9
    assert call.get("max_tokens") == 256
    assert llm.availability_queries == 1, "应校验模型名是否已注册"


def test_empty_creation_model_uses_default():
    """直连关闭 + 未配模型名 → 只传 task_name="utils"，不传 model_name。"""
    client, llm, _handler, _telemetry = _make_client(creation_model="")
    result = asyncio.run(client.generate("提示"))

    assert result == "生成结果"
    call = llm.calls[0]
    assert call.get("task_name") == "utils"
    assert "model_name" not in call


def test_generation_failure_returns_empty():
    """LLM 调用异常 → 返回空串且不向外抛（创作是附加动作，不能阻塞主流程）。"""
    client, _llm, _handler, _telemetry = _make_client(
        creation_model="bad-model",
        raise_on_generate=RuntimeError("未找到名为 'bad-model' 的模型"),
    )
    result = asyncio.run(client.generate("提示"))

    assert result == ""


def test_generate_records_token_cost():
    """生成成功后应向 telemetry 记录成本采样（指标 5：按字符粗估 token，task=creation）。"""
    client, _llm, _handler, telemetry = _make_client(creation_model="my-model")
    result = asyncio.run(client.generate("提示词"))

    assert result == "生成结果"
    assert len(telemetry.token_calls) == 1, "生成成功应记录一次成本采样"
    tokens, task = telemetry.token_calls[0]
    assert task == "creation"
    assert tokens >= 1, "token 估算至少为 1"


def test_unknown_model_name_warns_but_still_calls():
    """配置的模型名不在已注册列表 → 告警（含可用列表），但仍尝试调用。"""
    client, llm, handler, _telemetry = _make_client(
        creation_model="typo-model", available=["model-a", "model-b"]
    )
    result = asyncio.run(client.generate("提示"))

    assert result == "生成结果", "告警不应阻断调用（模型可能是刚注册的）"
    assert llm.availability_queries == 1
    assert any("不在已注册模型列表" in msg for msg in handler.messages), (
        f"应输出'模型未注册'告警，实际日志: {handler.messages}"
    )
    assert any("model-a" in msg for msg in handler.messages), "告警应附可用模型列表"


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
