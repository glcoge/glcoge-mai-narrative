"""创作模型客户端：把 engine 的 LLM 调用收敛到独立深模块。

接口只有一个 ``generate(prompt) -> str``（失败返回空串），内部走**宿主按名路由**，
并负责成本采样。engine 只依赖这个窄接口，测试可注入替身。

⚠️ v0.2.0 批 1（R10 退役）：原先的「插件 HTTP 直连 OpenAI 兼容端点」分支已删除。
它是宿主 1.2.0 吞 ``model_name``（issue #2031）时的绕行方案；2026-09-25 已在容器内
核实 1.2.5 的 ``_resolve_llm_capability_route`` 正常透传 model_name，绕行不再必要。
顺带消掉了 `[creator_model].api_key` 这个**明文密钥**配置项——插件配置里存密钥
既无必要也不该由插件承担。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


class CreatorClient:
    """创作模型客户端：按模型名路由（复用主程序已注册模型）。

    模型名路由依赖 MaiBot 1.2.5 的 #2031 修复（``task_name`` 与 ``model_name``
    可分别指定）；``model_name`` 指向**全局模型列表**中任意已注册模型，
    无需把模型分配给任何任务（``get_model_info_by_name`` 只查 models 列表）。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        # 按名路由提示只打一次（见 _log_route_hint_once）
        self._route_hint_logged = False

    async def generate(self, prompt: str, *, temperature: Optional[float] = None) -> str:
        """生成一段文本；失败返回空串（当前是唯一常规 LLM 调用点）。

        走 ``[llm].creation_model`` 按模型名路由（须为已注册模型名；
        留空则用主程序默认模型）。

        Args:
            temperature: 覆盖配置温度。批 4 的提案提炼需要**更确定**的输出
                （要解 JSON），而创作层默认 0.9 偏高；留 None 即沿用配置值。
        """
        text = await self._generate_via_model(prompt, temperature=temperature)
        if not text:
            return ""
        # 成本采样（指标 5）：按字符粗估 token，写入 llm_extra_tokens
        # （_telemetry 在 plugin.__init__ 显式置 None，直接属性访问即可，勿用 getattr 掩盖未初始化）
        telemetry = self._plugin._telemetry
        if telemetry is not None:
            tokens_approx = max(1, (len(prompt) + len(text)) // 3)
            telemetry.record_llm_tokens(float(tokens_approx), task="creation")
        return text

    async def _generate_via_model(
        self, prompt: str, *, temperature: Optional[float] = None
    ) -> str:
        """按模型名路由（``llm.generate``，``task_name`` 与 ``model_name`` 分别指定）。

        ``model_name`` 指向全局模型列表中任意已注册模型（含只注册、未分配任务的）；
        配置为空则不传 ``model_name``，走主程序默认模型（首次使用打 info 说明）。
        """
        cfg = self._plugin.config
        model_name = str(cfg.llm.creation_model or "").strip()
        payload_kwargs: dict[str, Any] = {"task_name": "utils"}
        if model_name:
            self._log_route_hint_once(model_name)
            payload_kwargs["model_name"] = model_name
        else:
            self._plugin.ctx.logger.info(
                "创作模型未配置（[llm].creation_model 为空），使用主程序默认模型；"
                "可在 WebUI 模型列表注册模型后填入该配置项"
            )
        try:
            result = await asyncio.wait_for(
                self._plugin.ctx.llm.generate(
                    prompt,
                    temperature=(
                        float(temperature) if temperature is not None else cfg.llm.temperature
                    ),
                    # major 档 400 字会截断（原固定 256）
                    max_tokens=int(cfg.llm.creation_max_tokens or 1024),
                    **payload_kwargs,
                ),
                timeout=30,
            )
        except Exception as exc:
            self._plugin.ctx.logger.warning(
                "创作模型调用失败: %s（[llm].creation_model=%r，请去 WebUI「模型列表」"
                "核对该模型名是否已注册且拼写一致）",
                exc,
                model_name,
            )
            return ""
        if isinstance(result, dict):
            return str(result.get("response") or result.get("content") or "").strip()
        return ""

    def _log_route_hint_once(self, model_name: str) -> None:
        """按名路由首次生效时打一条说明（每个进程一次，避免刷屏）。

        不做预校验的原因：宿主 ``ctx.llm.get_available_models()`` 返回的是**任务名**
        （utils/planner/…）而不是注册模型名，拿它比对模型名必然 100% 误报。
        模型名是否有误只能等主程序在调用时报错（"未找到名为 'X' 的模型"）。
        """
        if self._route_hint_logged:
            return
        self._route_hint_logged = True
        self._plugin.ctx.logger.info(
            "创作模型按名路由: %s（宿主未开放已注册模型名列表，无法预先校验；"
            "名称有误会在调用时报错）",
            model_name,
        )


__all__ = ["CreatorClient"]
