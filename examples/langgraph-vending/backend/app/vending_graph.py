"""LangGraph agent wired to the real vending machine UCP backend.

Replaces the Alpine Outfitters demo with our actual deployed services:
  supply-chain-mock  — inventory, machines, preorders
  ucp-mock           — OAuth, cart-sessions, checkout-sessions, dispense

The system prompt is grounded in UCP Agent Skills from the agentic-commerce-skills
repo AND the vending-agent / supply-chain skills from ucp-agent.
"""
from __future__ import annotations

from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from .llm import build_llm
from .skills import load_skills, skills_catalog
from .vending_tools import ALL_VENDING_TOOLS

_SKILLS = load_skills()


def _condense(body: str, limit: int = 1200) -> str:
    body = body.strip()
    return body if len(body) <= limit else body[:limit] + "\n…(truncated)…"


def _build_system_prompt() -> str:
    catalog = skills_catalog(_SKILLS)
    skill_sections = "\n\n".join(
        f"### Skill: {s.name}  ·  stage: {s.stage}\n{_condense(s.body)}"
        for s in _SKILLS
    )
    return f"""你是**贩卖机购物助手**，基于 UCP（Universal Commerce Protocol）帮助用户在自动贩卖机上完成购物。

你的后端：
- 供应链服务: https://supply-chain-mock.fxp007.workers.dev（机器列表、库存、预订单）
- UCP 服务: https://ucp-mock.fxp007.workers.dev（结账、出货）

## 购买流程（严格按顺序）

1. **发现商品** — 用户提到具体商品时调用 `find_item`；无具体商品时调用 `list_machines`。
2. **获取 Token** — 调用 `get_ucp_token(user_id)` 获取 Bearer token。
3. **浏览商品** — 调用 `browse_catalog(machine_id, token)` 获取 lane_id 和价格。
4. **创建结账** — 调用 `create_vending_checkout(lane_id, token)` 创建会话。
5. **折扣（可选）** — 用户有折扣码时调用 `apply_vending_discount`（SAVE10=九折, VEND20=八折）。
6. **用户确认** — 向用户展示商品名和总价，**明确获得用户"确认购买"后**再继续。
7. **完成购买** — 调用 `complete_vending_checkout(checkout_session_id, token)` 触发出货。
8. **出货成功** — 告知用户取货码和等待取货提示。

## 缺货处理
- `find_item` 返回 found=false 时：告知缺货，询问是否创建预订单，若同意调用 `create_preorder`。

## 硬性规则
- **禁止**在用户明确确认前调用 `complete_vending_checkout`。
- 始终展示实际价格（从工具返回值读取，不要猜测）。
- 用中文回复用户，保持简洁友好。
- `get_vending_checkout` 用于查询当前会话状态（状态已知时无需重复调用）。
- 若结账失败（error 字段非空），向用户解释原因并提供替代方案。

## 可用折扣码
| 码 | 折扣 |
|----|------|
| SAVE10 | 九折（10% off） |
| VEND20 | 八折（20% off） |

## 可用技能（每项对应上面的工具）
{catalog}

## 技能参考文档
{skill_sections}
"""


_SYSTEM_PROMPT = _build_system_prompt()


def build_vending_graph():
    llm = build_llm(streaming=True)
    llm_with_tools = llm.bind_tools(ALL_VENDING_TOOLS)
    system = SystemMessage(content=_SYSTEM_PROMPT)

    async def agent(state: MessagesState):
        response = await llm_with_tools.ainvoke([system, *state["messages"]])
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(ALL_VENDING_TOOLS))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=MemorySaver())


vending_graph = build_vending_graph()
