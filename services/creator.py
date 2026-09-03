"""创作模型客户端：把 engine 的 LLM 调用收敛到独立深模块。

接口只有一个 ``generate(prompt) -> str``（失败返回空串），内部按配置每次调用时
选择直连或 task 路由，并负责成本采样。engine 只依赖这个窄接口，测试可注入替身。
"""

from __future__ import annotations

import asyncio
from typing import Any


class CreatorClient:
    """创作模型客户端：直连优先，回退 MaiBot task 路由。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    async def generate(self, prompt: str) -> str:
        """生成一段文本；失败返回空串（当前是唯一常规 LLM 调用点）。

        优先直连：``[creator_model]`` 启用且 base_url 非空时，直接 POST
        OpenAI 兼容 /chat/completions（body 固定 ``thinking={type:"disabled"}``，
        关闭推理模型的思维链，避免挤占 max_tokens 导致正文截断）。
        否则回退 MaiBot task 路由（``[llm] creation_task``）。
        """
        cfg = self._plugin.config
        creator = cfg.creator_model
        if creator.enabled and str(creator.base_url or "").strip():
            text = await self._generate_direct(prompt)
        else:
            text = await self._generate_via_task(prompt)

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

    async def _generate_via_task(self, prompt: str) -> str:
        """回退：走 MaiBot 内部 task 路由（llm.generate）。"""
        cfg = self._plugin.config
        try:
            result = await asyncio.wait_for(
                self._plugin.ctx.llm.generate(
                    prompt,
                    model=cfg.llm.creation_task or "",
                    temperature=cfg.llm.temperature,
                    max_tokens=256,
                ),
                timeout=30,
            )
        except Exception as exc:
            self._plugin.ctx.logger.warning("创作模型调用失败: %s", exc)
            return ""
        if isinstance(result, dict):
            return str(result.get("response") or result.get("content") or "").strip()
        return ""


__all__ = ["CreatorClient"]
