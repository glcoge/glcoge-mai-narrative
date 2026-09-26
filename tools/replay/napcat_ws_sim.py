"""真 WS 灌帧仿真器：冒充 NapCat 的 OneBot11 WebSocket 服务端。

用途（收尾里程碑三件套 ②：真 WS 灌帧仿真 E2E）
------------------------------------------------
MaiBot 的 NapCat 适配器是 **WS 客户端**，主动连到 ``napcat_server.host:port``。
本脚本在宿主机起一个 **真 WS 服务端** 顶替 NapCat，用 **OneBot11 协议** 把
``MaiBot-export`` 里的**真实聊天内容**灌进 MaiBot 主程序，走完整链路：

    WS 帧 → NapCat 适配器 → Host → planner hook（生活线注入）
          → replyer hook（漂移层注入） → LLM → 回发动作（send_msg）

与 ``pytests/test_replyer_hook.py`` 的「合同级 E2E」互补：
- 合同级：直接调 handler，断言 items / kwargs 契约
- 本脚本：**真 WS + 真帧 + 真主程序**，断言端到端链路确实跑通

用法
----
1. 停掉真 NapCat（避免真 QQ 登录）：``docker compose stop napcat``
2. 把适配器配置指向宿主机：
   ``MaiBot-Napcat-Adapter/config.toml`` 的 ``[napcat_server].host = "host.docker.internal"``
3. 起本服务：``python tools/replay/napcat_ws_sim.py --port 3001``
4. 重启 MaiBot：``docker compose restart core``
5. 看本脚本打印 + ``docker logs maim-bot-core``

断言口径（人工核对，脚本也会打印）
----------------------------------
- 适配器连上并收到 meta_event
- 真实消息帧被主程序接收并处理（core 日志出现该消息）
- planner 注入（生活线上下文）不回归
- replyer hook 注入计数增加（漂移层）
- 主程序回发动作（send_private_msg / send_msg）被本服务端收到

⚠️ 不要在生产/真凭证环境长期开着：它是**假 NapCat**，收不到真 QQ 消息。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import websockets
except ImportError:  # pragma: no cover - 环境缺依赖时的明确报错
    sys.stderr.write("需要 websockets 库：pip install websockets\n")
    raise

#: 机器人 QQ（与 docker-config/napcat/onebot11_3795418670.json 一致）
BOT_QQ = 3795418670
#: 用户本人 QQ（收尾里程碑白名单只留这一个）
USER_QQ = 2111957354


def _now() -> int:
    return int(time.time())


def build_private_message(text: str, *, user_id: int = USER_QQ, seq: int) -> Dict[str, Any]:
    """构造一条 **OneBot11 私聊消息事件**（array 格式，与 NapCat 配置一致）。"""
    return {
        "time": _now(),
        "self_id": BOT_QQ,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 700000000 + seq,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
        "sender": {
            "user_id": user_id,
            "nickname": "Oranger_橙儿",
            "sex": "unknown",
            "age": 0,
        },
    }


def build_meta_event() -> Dict[str, Any]:
    """OneBot11 元事件：连接建立。适配器据此确认 self_id 与连接可用。"""
    return {
        "time": _now(),
        "self_id": BOT_QQ,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
    }


def handle_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """应答 OneBot11 API 调用（只答够适配器用，未识别的一律 retcode=0）。"""
    action = str(payload.get("action") or "")
    params = payload.get("params") or {}
    echo = payload.get("echo")

    if action == "get_login_info":
        data: Any = {"user_id": BOT_QQ, "nickname": "曦瞳"}
    elif action == "get_version_info":
        data = {"app_name": "NapCat(Sim)", "protocol_version": "v11", "app_version": "sim-1.0"}
    elif action in {"send_msg", "send_private_msg", "send_group_msg"}:
        data = {"message_id": 800000000 + int(time.time()) % 1000000}
    elif action == "get_friend_list":
        data = [{"user_id": USER_QQ, "nickname": "Oranger_橙儿", "remark": "Oranger_橙儿"}]
    elif action == "get_group_list":
        data = []
    else:
        data = None

    if data is not None:
        return {"status": "ok", "retcode": 0, "data": data, "echo": echo}
    # 未识别动作：仍回 ok，避免适配器把整条链路判死
    return {"status": "ok", "retcode": 0, "data": {}, "echo": echo}


async def _serve_client(ws, frames: List[Dict[str, Any]], *, log_path: Optional[Path]) -> None:
    peer = getattr(ws, "remote_address", "?")
    print(f"[sim] 客户端已连接: {peer}", flush=True)

    log: List[Dict[str, Any]] = []

    async def send(frame: Dict[str, Any], label: str) -> None:
        await ws.send(json.dumps(frame, ensure_ascii=False))
        print(f"[sim] → 灌帧 {label}: {str(frame)[:160]}", flush=True)
        log.append({"dir": "out", "label": label, "frame": frame})

    # 1) 元事件：告知 self_id
    await send(build_meta_event(), "meta_event/lifecycle")
    await asyncio.sleep(0.6)

    # 2) 逐条灌真实消息帧
    for i, text in enumerate(frames, 1):
        await send(build_private_message(text, seq=i), f"message#{i}")
        await asyncio.sleep(2.0)

    # 3) 之后持续应答动作调用；收到 send_* 说明链路走到回发
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[sim] ← 非 JSON: {raw[:120]}", flush=True)
                continue
            action = payload.get("action")
            print(f"[sim] ← 动作调用: {action} params={str(payload.get('params'))[:160]}", flush=True)
            log.append({"dir": "in", "label": str(action), "frame": payload})
            await ws.send(json.dumps(handle_action(payload), ensure_ascii=False))
    except websockets.exceptions.ConnectionClosed:
        print("[sim] 客户端断开", flush=True)
    finally:
        if log_path:
            log_path.write_text(
                json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[sim] 交互日志已写入 {log_path}", flush=True)


def load_real_texts(export: Optional[Path], *, fallback: List[str], limit: int) -> List[str]:
    """从 MaiBot-export 抽真实私聊文本；抽不到就用内置样本。"""
    texts: List[str] = []
    if export and export.exists():
        import json as _json

        for line in export.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if d.get("sender_id") == str(USER_QQ) and not d.get("is_bot"):
                kind = d.get("kind")
                text = (d.get("text") or "").strip()
                # 命令消息会走命令通道，不测注入；只要普通文本
                if kind == "text" and text:
                    texts.append(text)
            if len(texts) >= limit:
                break
    return texts or fallback


def main() -> int:
    ap = argparse.ArgumentParser(description="NapCat OneBot11 WS 仿真服务端（真 WS 灌帧 E2E）")
    ap.add_argument("--port", type=int, default=3001)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument(
        "--export",
        default=r"E:\Downloads\MaiBot-export\messages.jsonl",
        help="MaiBot-export 的 messages.jsonl（取真实私聊文本）",
    )
    ap.add_argument("--limit", type=int, default=3, help="灌几条真实消息")
    ap.add_argument("--log", default="", help="交互日志写入路径")
    args = ap.parse_args()

    fallback = ["你好呀", "你叫什么名字？", "今天过得怎么样？"]
    texts = load_real_texts(Path(args.export) if args.export else None, fallback=fallback, limit=args.limit)
    print(f"[sim] 待灌真实消息 {len(texts)} 条:", flush=True)
    for t in texts:
        print(f"       - {t[:60]}", flush=True)

    log_path = Path(args.log) if args.log else None

    async def runner():
        async with websockets.serve(
            lambda ws: _serve_client(ws, texts, log_path=log_path), args.host, args.port
        ):
            print(f"[sim] 监听 ws://{args.host}:{args.port}（等待 MaiBot 适配器连入）", flush=True)
            await asyncio.Future()  # 永不退出

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("[sim] 已停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
