#!/usr/bin/env python3
"""
Continuous vending machine consumer simulation.

Drives the LangGraph vending agent with multiple user personas, runs indefinitely,
and prints rolling metrics. Each session simulates a real consumer conversation.

Usage:
    cd examples/langgraph-vending/backend
    cp .env.example .env          # fill in BIGMODEL_API_KEY
    python simulate.py            # run forever
    python simulate.py --runs 5   # fixed number of runs
    python simulate.py --interval 15 --runs 10  # 15s between sessions
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re as _re
import random
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Load env from .env in backend/
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from langchain_core.messages import HumanMessage

from app.vending_graph import vending_graph


# --------------------------------------------------------------------------- #
# Personas
# --------------------------------------------------------------------------- #
@dataclass
class Persona:
    name: str
    user_id: str
    machine_id: str | None   # None = online mode (no specific machine)
    scenarios: list[str]     # list of conversation scripts (one per scenario type)


PERSONAS = [
    Persona(
        name="晓飞（冲动购买）",
        user_id="xiaofei-sim-001",
        machine_id="vm-001",
        scenarios=[
            "我想买可乐",
            "来瓶雪碧",
            "有没有冰红茶",
            "我要一瓶苏打水",
            "来杯咖啡",
        ],
    ),
    Persona(
        name="李四（使用折扣码）",
        user_id="lisi-sim-002",
        machine_id="vm-001",
        scenarios=[
            "我要买可乐，我有优惠码 SAVE10",
            "给我来瓶绿茶，用折扣码 VEND20",
            "雪碧一瓶，优惠码 SAVE10",
            "来杯拿铁，用 VEND20",
        ],
    ),
    Persona(
        name="王五（在线模式）",
        user_id="wangwu-sim-003",
        machine_id=None,
        scenarios=[
            "哪里能买到可乐？",
            "我想买绿茶，附近哪台机器有？",
            "哪里有雪碧？",
            "我想喝咖啡，哪台有？",
        ],
    ),
    Persona(
        name="小明（缺货预订）",
        user_id="xiaoming-sim-004",
        machine_id="vm-001",
        scenarios=[
            "有没有燕京啤酒？",
            "我想买矿泉水",
        ],
    ),
    Persona(
        name="老张（咖啡爱好者）",
        user_id="laozhang-sim-005",
        machine_id="vm-001",
        scenarios=[
            "我要一杯拿铁咖啡",
            "有美式咖啡吗",
            "来杯黑咖啡",
        ],
    ),
    Persona(
        name="小红（健康饮品）",
        user_id="xiaohong-sim-006",
        machine_id="vm-001",
        scenarios=[
            "有没有橙汁",
            "我想买东方树叶",
            "来瓶苏打气泡水",
        ],
    ),
]

# Auto-confirm phrases the simulation injects after payment_request
AUTO_CONFIRM = ["确认购买", "好的，确认", "就这个了，付款"]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
@dataclass
class Metrics:
    runs: int = 0
    successes: int = 0          # completed purchases
    out_of_stocks: int = 0      # out-of-stock encounters
    preorders: int = 0          # preorders created
    discounts_applied: int = 0  # discount codes used
    errors: int = 0             # exceptions / tool errors
    canceled: int = 0           # user canceled
    total_duration_s: float = 0.0
    run_log: list[dict] = field(default_factory=list)

    def record(self, run: dict):
        self.runs += 1
        self.total_duration_s += run.get("duration_s", 0)
        if run.get("success"):
            self.successes += 1
        if run.get("out_of_stock"):
            self.out_of_stocks += 1
        if run.get("preorder"):
            self.preorders += 1
        if run.get("discount"):
            self.discounts_applied += 1
        if run.get("error"):
            self.errors += 1
        if run.get("canceled"):
            self.canceled += 1
        self.run_log.append(run)

    def summary(self) -> str:
        avg = self.total_duration_s / max(self.runs, 1)
        lines = [
            f"\n{'━'*60}",
            f"  模拟统计  [{datetime.now().strftime('%H:%M:%S')}]",
            f"{'━'*60}",
            f"  总次数:       {self.runs}",
            f"  成功购买:     {self.successes}  ({100*self.successes//max(self.runs,1)}%)",
            f"  缺货:         {self.out_of_stocks}",
            f"  预订单:       {self.preorders}",
            f"  折扣使用:     {self.discounts_applied}",
            f"  取消:         {self.canceled}",
            f"  错误:         {self.errors}",
            f"  平均耗时:     {avg:.1f}s",
            f"{'━'*60}",
        ]
        return "\n".join(lines)


metrics = Metrics()


# --------------------------------------------------------------------------- #
# Session runner
# --------------------------------------------------------------------------- #
def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _role_prefix(role: str) -> str:
    if role == "human":
        return _color("👤 用户", "36")
    if role == "ai":
        return _color("🤖 Agent", "32")
    return _color("🔧 Tool", "33")


async def run_session(persona: Persona, opening_message: str, run_id: int) -> dict:
    """Run one simulated shopping session. Returns a result dict."""
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    start = time.time()

    result = {
        "run_id": run_id,
        "persona": persona.name,
        "user_id": persona.user_id,
        "machine_id": persona.machine_id,
        "opening": opening_message,
        "success": False,
        "out_of_stock": False,
        "preorder": False,
        "discount": "SAVE10" in opening_message or "VEND20" in opening_message,
        "canceled": False,
        "error": False,
        "pickup_code": None,
        "messages_exchanged": 0,
        "duration_s": 0.0,
    }

    print(f"\n{'─'*60}")
    print(f"  Run #{run_id}  |  {persona.name}  |  {datetime.now().strftime('%H:%M:%S')}")
    if persona.machine_id:
        print(f"  机器: {persona.machine_id}  |  消息: {opening_message}")
    else:
        print(f"  在线模式  |  消息: {opening_message}")
    print(f"{'─'*60}")

    # Build first message, optionally prepend machine context for kiosk mode
    if persona.machine_id:
        first_msg = f"[machine_id={persona.machine_id}] {opening_message}"
    else:
        first_msg = opening_message

    max_turns = 14  # prevent infinite loops
    turn = 0
    last_ai_text = ""

    try:
        messages = [HumanMessage(content=first_msg)]

        while turn < max_turns:
            turn += 1
            print(f"  {_role_prefix('human')}: {messages[-1].content}")

            # Invoke LangGraph
            output = await vending_graph.ainvoke({"messages": messages}, config=config)
            ai_msgs = output["messages"]
            ai_reply = ai_msgs[-1]
            last_ai_text = ai_reply.content if hasattr(ai_reply, "content") else str(ai_reply)

            print(f"  {_role_prefix('ai')}: {last_ai_text[:300]}{'...' if len(last_ai_text) > 300 else ''}")
            result["messages_exchanged"] = turn * 2

            # --- Detect terminal states ---

            # Success: pickup code pattern (e.g. KXQO_HO4) or "取货码：XXXX" without "无法"
            pickup_pattern = bool(_re.search(r"[A-Z0-9]{4}_[A-Z0-9]{3}", last_ai_text))
            has_code_word = "取货码" in last_ai_text and "无法" not in last_ai_text
            if pickup_pattern or has_code_word or "awaiting_pickup" in last_ai_text:
                result["success"] = True
                print(f"  ✅ 购买成功！")
                break

            # Out of stock
            if any(kw in last_ai_text for kw in ["缺货", "没有库存", "无库存", "暂无", "所有机器都缺"]):
                result["out_of_stock"] = True

            # Preorder created
            if "已为您登记预订" in last_ai_text or "预订单已创建" in last_ai_text:
                result["preorder"] = True
                print(f"  \U0001f4cb 预订单已创建")
                break

            # Persistent failure — give up
            if last_ai_text.count("无法") >= 3 or "系统故障" in last_ai_text:
                result["error"] = True
                print(f"  ❌ 系统持续错误")
                break

            # Price/confirm prompt — check BEFORE selection to avoid false matches
            if ("¥" in last_ai_text or "总价" in last_ai_text or "合计" in last_ai_text) and \
               any(kw in last_ai_text for kw in ["确认", "是否购买", "支付", "请确认"]):
                messages = [HumanMessage(content=random.choice(AUTO_CONFIRM))]
                continue

            # Agent presenting a numbered list and asking user to choose an item
            needs_selection = any(kw in last_ai_text for kw in [
                "哪种", "哪一种", "哪款", "请选", "1️⃣", "2️⃣",
            ]) and ("？" in last_ai_text or "?" in last_ai_text)
            # Also trigger on numbered list with prices but NOT when it's a confirm message
            if not needs_selection and _re.search(r"[12]\.\s*\*{0,2}[^\n]{2,15}\*{0,2}.*¥", last_ai_text):
                if "确认" not in last_ai_text[:80] and "合计" not in last_ai_text[:80]:
                    needs_selection = True

            if needs_selection:
                # Try to extract first item name from numbered list
                m = _re.search(r"[1①]\W{0,3}\*{0,2}([^\n*|]{2,15})\*{0,2}\W*¥", last_ai_text)
                if m:
                    name = m.group(1).strip()
                    # Reduce to a short keyword
                    for kw in ["经典", "无糖", "矿泉", "红牛", "可乐"]:
                        if kw in name:
                            reply = kw
                            break
                    else:
                        reply = "1"
                else:
                    reply = "经典" if "经典" in last_ai_text else "1"
                print(f"  [sim: 选 '{reply}']")
                messages = [HumanMessage(content=reply)]
                continue

            # Other question
            if "？" in last_ai_text or "?" in last_ai_text:
                if "预订" in last_ai_text:
                    reply = "好的，帮我预订"
                elif "哪台" in last_ai_text or "哪个机器" in last_ai_text:
                    reply = persona.machine_id or "随便一台"
                else:
                    reply = "好的，继续"
                messages = [HumanMessage(content=reply)]
                continue

            # No more questions — session ended naturally
            break

    except Exception as exc:
        result["error"] = True
        print(f"  💥 异常: {exc}")
        traceback.print_exc()

    result["duration_s"] = time.time() - start
    print(f"  耗时: {result['duration_s']:.1f}s")
    return result


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
async def run_batch(batch: list[tuple[Persona, str, int]]) -> list[dict]:
    """Run a batch of sessions concurrently, return results in order."""
    tasks = [run_session(p, msg, rid) for p, msg, rid in batch]
    return list(await asyncio.gather(*tasks, return_exceptions=False))


async def main(total_runs: int | None, interval_s: float, concurrency: int = 1):
    print(_color("贩卖机 LangGraph Agent 消费者模拟", "1;35"))
    print(f"目标: {total_runs or '∞'} 次 | 并发: {concurrency} | 间隔: {interval_s}s | 模型: {os.getenv('BIGMODEL_MODEL', 'glm-5.1')}")
    print(f"UCP: {os.getenv('UCP_MOCK_URL', 'https://ucp-mock.fxp007.workers.dev')}")
    print(f"SC:  {os.getenv('SUPPLY_CHAIN_URL', 'https://supply-chain-mock.fxp007.workers.dev')}")
    print()

    run_id = 0
    summary_every = 5

    while True:
        # Build next batch
        batch: list[tuple[Persona, str, int]] = []
        for _ in range(concurrency):
            run_id += 1
            if total_runs and run_id > total_runs:
                break
            persona = random.choice(PERSONAS)
            opening = random.choice(persona.scenarios)
            batch.append((persona, opening, run_id))

        if not batch:
            break

        if concurrency > 1 and len(batch) > 1:
            print(f"\n{'═'*60}")
            print(f"  并发批次 #{(run_id - len(batch) + 1)}–#{run_id}  ({len(batch)} 个会话同时运行)")
            print(f"{'═'*60}")

        results = await run_batch(batch)
        for r in results:
            metrics.record(r)

        completed = metrics.runs
        if completed % summary_every == 0 or (total_runs and completed >= total_runs):
            print(metrics.summary())

        remaining = (total_runs - completed) if total_runs else None
        if remaining is None or remaining > 0:
            jitter = interval_s * (0.5 + random.random())
            print(f"  ⏳ 等待 {jitter:.1f}s ...")
            await asyncio.sleep(jitter)

        if total_runs and metrics.runs >= total_runs:
            break

    print(metrics.summary())
    print("\n模拟结束。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="贩卖机消费者行为模拟")
    parser.add_argument("--runs", type=int, default=None, help="总运行次数（默认无限）")
    parser.add_argument("--interval", type=float, default=10.0, help="批次间平均间隔秒数（默认10）")
    parser.add_argument("--concurrency", "-c", type=int, default=1, help="并发会话数（默认1）")
    parser.add_argument("--persona", type=str, default=None, help="指定用户角色名（模糊匹配）")
    args = parser.parse_args()

    if args.persona:
        matched = [p for p in PERSONAS if args.persona in p.name or args.persona in p.user_id]
        if matched:
            PERSONAS.clear()
            PERSONAS.extend(matched)
            print(f"使用角色: {[p.name for p in PERSONAS]}")

    try:
        asyncio.run(main(args.runs, args.interval, args.concurrency))
    except KeyboardInterrupt:
        print(metrics.summary())
        print("\n已中断。")
