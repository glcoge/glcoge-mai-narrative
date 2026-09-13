"""创作模型客户端：把 engine 的 LLM 调用收敛到独立深模块。

接口只有一个 ``generate(prompt) -> str``（失败返回空串），内部按配置每次调用时
选择直连或 task 路由，并负责成本采样。engine 只依赖这个窄接口，测试可注入替身。
"""

from __future__ import annotations

import asyncio
from typing import Any


class CreatorClient:
    """创作模型客户端：直连优先，回退按模型名路由（复用主程序已注册模型）。

    模型名路由依赖 MaiBot 1.2.5 的 #2031 修复（``task_name`` 与 ``model_name``
    可分别指定）；``model_name`` 指向**全局模型列表**中任意已注册模型，
    无需把模型分配给任何任务（``get_model_info_by_name`` 只查 models 列表）。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    async def generate(self, prompt: str) -> str:
        """生成一段文本；失败返回空串（当前是唯一常规 LLM 调用点）。

        优先直连：``[creator_model]`` 启用且 base_url 非空时，直接 POST
        OpenAI 兼容 /chat/completions（body 固定 ``thinking={type:"disabled"}``，
        关闭推理模型的思维链，避免挤占 max_tokens 导致正文截断）。
        否则按模型名路由（``[llm].creation_model``，须为已注册模型名；
        留空则用主程序默认模型）。
        """
        cfg = self._plugin.config
        creator = cfg.creator_model
        if creator.enabled and str(creator.base_url or "").strip():
            text = await self._generate_direct(prompt)
        else:
            text = await self._generate_via_model(prompt)

        if not text:
            return ""
        # 成本采样（指标 5）：按字符粗估 token，写入 llm_extra_tokens
        telemetry = getattr(self._plugin, "_telemetry", None)
        if telemetry is not None:
            tokens_approx = max(1, (len(prompt) + len(text)) // 3)
            telemetry.record_llm_tokens(float(tokens_approx), task="creation")
        return text

    async def _generate_direct(self, prompt: str) -> str:
        """直连 OpenAI 兼容端点生成（自带 thinking disabled，绕开推理模型思维链）。"""
        import httpx

        cfg = self._plugin.config
        creator = cfg.creator_model
        base_url = str(creator.base_url or "").strip().rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        payload = {
            "model": str(creator.model_id or "").strip(),
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "max_tokens": int(creator.max_tokens or 384),
            "temperature": float(cfg.llm.temperature),
            # 关闭思考：生成短文本无需思维链，防止推理模型挤占 max_tokens
            "thinking": {"type": "disabled"},
        }
        headers = {"Content-Type": "application/json"}
        api_key = str(creator.api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=float(creator.timeout_seconds or 30.0)) as client:
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
        except Exception as exc:
            self._plugin.ctx.logger.warning("创作模型直连失败: %s", exc)
            return ""
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return str(content or "").strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._plugin.ctx.logger.warning("创作模型直连响应解析失败: %s", exc)
            return ""

    async def _generate_via_model(self, prompt: str) -> str:
        """回退：按模型名路由（``llm.generate``，``task_name`` 与 ``model_name`` 分别指定）。

        ``model_name`` 指向全局模型列表中任意已注册模型（含只注册、未分配任务的）；
        配置为空则不传 ``model_name``，走主程序默认模型（首次使用打 info 说明）。
        """
        cfg = self._plugin.config
        model_name = str(cfg.llm.creation_model or "").strip()
        payload_kwargs: dict[str, Any] = {"task_name": "utils"}
        if model_name:
            await self._warn_if_model_unknown(model_name)
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
                    temperature=cfg.llm.temperature,
                    max_tokens=256,
                    **payload_kwargs,
                ),
                timeout=30,
            )
        except Exception as exc:
            self._plugin.ctx.logger.warning("创作模型调用失败: %s", exc)
            return ""
        if isinstance(result, dict):
            return str(result.get("response") or result.get("content") or "").strip()
        return ""

    async def _warn_if_model_unknown(self, model_name: str) -> None:
        """配置的模型名不在已注册列表时告警（附可用列表）；查询失败不阻塞调用。"""
        try:
            available = await self._plugin.ctx.llm.get_available_models()
        except Exception as exc:
            self._plugin.ctx.logger.debug("获取可用模型列表失败: %s", exc)
            return
        names = [str(item) for item in (available or [])]
        if model_name not in names:
            preview = ", ".join(names[:8]) or "（空）"
            self._plugin.ctx.logger.warning(
                "创作模型 %r 不在已注册模型列表中（可用: %s%s），调用将失败；"
                "请核对 WebUI 模型列表中的名称",
                model_name,
                preview,
                "…" if len(names) > 8 else "",
            )


__all__ = ["CreatorClient"]
