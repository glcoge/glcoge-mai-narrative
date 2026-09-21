"""创作模型路由测试（task 路由 → 按模型名路由改造）。

背景（2026-09-13）：MaiBot 1.2.5 修复 #2031（model_name 被吞入任务名解析）后，
插件回退路线从「复用内部 task（``model=<任务名>`` 旧式写法）」改为「按模型名路由」
（``task_name="utils"`` + ``model_name=<已注册模型名>``）。后者可调用**只注册、
未分配任务**的模型（``get_model_info_by_name`` 查全局模型列表，不校验任务分配），
用户可在 WebUI 自行注册"无思考版"模型（``extra_params.thinking.disabled``）按名绑定。

直连路线（[creator_model]）保持不变：独立供应商/独立额度场景仍需它。

⚠ 2026-09-14 修正（宿主能力语义澄清）：``ctx.llm.get_available_models()`` 返回的
**不是注册模型名，而是任务名**（链路：plugin_runtime/capabilities/core.py:755 →
services/service_task_resolver.py:12 返回 ``model_task_config`` 的 TaskConfig 键）。
因此"用任务列表校验模型名"必然 100% 误报——原 ``_warn_if_model_unknown`` 对任何
合法模型名都会打 WARN 并谎称"调用将失败"。改为**事后诊断**：不再预检，
调用失败时日志带模型名 + 排查指引（真拼错时主程序会报"未找到名为 'X' 的模型"）。

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
    # 路由提示是 info 级，默认 root 级别 WARNING 会吞掉 → 显式放行 DEBUG
    plugin.ctx.logger.setLevel(_stdlib_logging.DEBUG)
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
    # 2026-09-21：按名路由不再固定 256，改为取 [creator_model].max_tokens
    # （major 档 400 字会被 256 截断）；fixture 该值为 384。
    assert call.get("max_tokens") == 384, "按名路由应使用 [creator_model].max_tokens"
    assert llm.availability_queries == 0, (
        "宿主只提供任务名列表，无法校验模型名 → 不应再查询 get_available_models"
    )


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


def test_model_route_hint_logged_once():
    """按名路由生效 → 首次打一条 info（说明无法预校验），且**不重复刷屏**。

    背景：宿主 ``get_available_models()`` 只给任务名，无法校验模型名（见模块 docstring）。
    """
    client, llm, handler, _telemetry = _make_client(
        creation_model="my-model", available=["utils", "planner"]
    )

    asyncio.run(client.generate("提示1"))
    asyncio.run(client.generate("提示2"))

    hints = [m for m in handler.messages if "按名路由" in m]
    assert len(hints) == 1, f"路由提示应只打一次，实际: {handler.messages}"
    assert "my-model" in hints[0], "提示应含实际使用的模型名"
    assert llm.availability_queries == 0, "不应查询任务列表做预校验"


def test_model_call_failure_logs_model_name():
    """调用失败 → warning 带模型名 + 排查指引（宿主不预检，只能事后诊断）。"""
    client, _llm, handler, _telemetry = _make_client(
        creation_model="typo-model",
        raise_on_generate=RuntimeError("未找到名为 'typo-model' 的模型"),
    )
    result = asyncio.run(client.generate("提示"))

    assert result == "", "失败应返回空串不抛（创作是附加动作）"
    warnings = [m for m in handler.messages if "创作模型调用失败" in m]
    assert len(warnings) == 1, f"应有一条失败告警，实际: {handler.messages}"
    assert "typo-model" in warnings[0], "失败日志应带出模型名，便于核对"
    assert "WebUI" in warnings[0], "失败日志应给出排查指引（去 WebUI 模型列表核对）"


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
