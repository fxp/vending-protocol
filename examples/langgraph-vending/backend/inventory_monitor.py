#!/usr/bin/env python3
"""
Automated inventory monitor and replenishment agent.

Periodically checks stock levels across all vending machines, creates purchase
orders for low-stock / out-of-stock items, and simulates delivery to restock.

Usage:
    python inventory_monitor.py                   # run forever, check every 90s
    python inventory_monitor.py --once            # single analysis + restock cycle
    python inventory_monitor.py --interval 60     # check every 60s
    python inventory_monitor.py --dry-run         # analyze only, no orders
    python inventory_monitor.py --restock-delay 10  # faster delivery simulation
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import httpx

SC_URL = os.getenv("SUPPLY_CHAIN_URL", "https://supply-chain-mock.fxp007.workers.dev")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _stock_bar(qty: int, min_qty: int, capacity: int) -> str:
    filled = min(qty, capacity)
    bar_len = min(capacity, 10)
    filled_blocks = round(filled / max(capacity, 1) * bar_len)
    bar = "█" * filled_blocks + "░" * (bar_len - filled_blocks)
    if qty == 0:
        color = "31"   # red
    elif qty < min_qty:
        color = "33"   # yellow
    else:
        color = "32"   # green
    return _c(f"[{bar}]", color)


# --------------------------------------------------------------------------- #
# Core analysis & replenishment
# --------------------------------------------------------------------------- #

async def fetch_all_inventory(client: httpx.AsyncClient) -> list[dict]:
    """Return flat list of all inventory rows across all machines."""
    r = await client.get(f"{SC_URL}/machines")
    r.raise_for_status()
    machines = r.json()
    all_inv: list[dict] = []
    for m in machines:
        r2 = await client.get(f"{SC_URL}/inventory", params={"machine_id": m["id"]})
        if r2.status_code == 200:
            rows = r2.json()
            for row in rows:
                row.setdefault("machine_name", m.get("name", m["id"]))
            all_inv.extend(rows)
    return all_inv


async def fetch_pending_preorders(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{SC_URL}/preorders")
    if r.status_code != 200:
        return []
    return [p for p in r.json() if p.get("status", "pending") not in ("fulfilled", "cancelled")]


async def print_inventory_table(inv: list[dict]) -> None:
    print(f"\n  {'机器':<10} {'货道':<5} {'商品':<22} {'库存':>4}/{' cap'} {'状态':>5}")
    print(f"  {'─'*58}")
    for row in inv:
        qty = row["qty"]
        cap = row.get("capacity", "?")
        min_q = row.get("min_qty", 0)
        bar = _stock_bar(qty, min_q, cap if isinstance(cap, int) else 15)
        if qty == 0:
            status = _c("缺货", "1;31")
        elif qty < min_q:
            status = _c("低库存", "1;33")
        else:
            status = _c("正常", "32")
        print(f"  {row['machine_id']:<10} {row['lane_id']:<5} {row['name']:<22} "
              f"{qty:>3}/{cap:<3} {bar} {status}")


async def get_sku_map(client: httpx.AsyncClient) -> dict[str, dict]:
    """Return {sku_id: sku_obj} for all SKUs."""
    r = await client.get(f"{SC_URL}/skus")
    if r.status_code != 200:
        return {}
    return {s["sku_id"]: s for s in r.json()}


async def create_direct_purchase_order(
    client: httpx.AsyncClient,
    supplier_id: str,
    machine_id: str,
    items: list[dict],
) -> dict | None:
    """Create a purchase order for a specific machine (bypasses minimum-order checks)."""
    po_items = [
        {"sku_id": item["sku_id"], "qty": item["reorder_qty"],
         "unit_cost_fen": item.get("cost_fen", 100),
         "sku_name": item.get("sku_name", "")}
        for item in items
    ]
    r = await client.post(f"{SC_URL}/purchase-orders", json={
        "supplier_id": supplier_id,
        "machine_id": machine_id,
        "items": po_items,
    })
    if r.status_code in (200, 201):
        return r.json()
    print(f"  ⚠️  创建采购单失败 [{r.status_code}]: {r.text[:120]}")
    return None


async def check_and_replenish(
    client: httpx.AsyncClient,
    dry_run: bool = False,
    restock_delay_s: int = 30,
    force: bool = False,
) -> dict:
    """One monitoring cycle. Returns summary dict."""
    summary = {"low_stock": 0, "orders_created": 0, "restocked": 0, "preorders": 0}

    sep = "─" * 62
    print(f"\n{'═'*62}")
    print(f"  {_c('📦 库存分析 & 补货', '1;36')}  [{ts()}]")
    print(f"{'═'*62}")

    # ── 1. Fetch all inventory ──────────────────────────────────────────────
    inv = await fetch_all_inventory(client)
    low_inv = [r for r in inv if r.get("low_stock")]
    summary["low_stock"] = len(low_inv)

    if not inv:
        print(f"  ⚠️  无法获取库存数据")
        return summary

    await print_inventory_table(inv)

    # ── 2. Pending preorders ────────────────────────────────────────────────
    preorders = await fetch_pending_preorders(client)
    summary["preorders"] = len(preorders)
    if preorders:
        sku_demand: dict[str, int] = {}
        for p in preorders:
            key = p.get("sku_name") or p.get("sku_id", "?")
            sku_demand[key] = sku_demand.get(key, 0) + p.get("qty", 1)
        print(f"\n  {_c('📋 待履行预订单', '1;35')} ({len(preorders)} 条):")
        for name, qty in sorted(sku_demand.items(), key=lambda x: -x[1]):
            print(f"     • {name}: {qty} 件等待到货通知")

    # ── 3. Decision ─────────────────────────────────────────────────────────
    if not low_inv:
        print(f"\n  {_c('✅ 所有货道库存充足，无需补货', '32')}")
        return summary

    print(f"\n  {_c(f'⚠️  发现 {len(low_inv)} 个需补货货道', '1;33')}:")
    for row in low_inv:
        fill_to = row.get("capacity", 15)
        reorder = fill_to - row["qty"]
        print(f"     {row['machine_id']}/{row['lane_id']} {row['name']:<22} "
              f"当前 {row['qty']} → 补至 {fill_to} (补 {reorder})")

    if dry_run:
        print(f"\n  {_c('[dry-run] 分析完成，跳过下单', '90')}")
        return summary

    # ── 4. Build per-machine, per-supplier replenishment plan ───────────────
    print(f"\n  {_c('🛒 构建补货计划...', '36')}")
    sku_map = await get_sku_map(client)

    # Group low-stock lanes by (machine_id, supplier_id)
    plan: dict[tuple[str, str], list[dict]] = {}
    for row in low_inv:
        sku = sku_map.get(row["sku_id"], {})
        supplier_id = sku.get("supplier_id")
        if not supplier_id:
            continue
        fill_qty = row.get("capacity", 15) - row["qty"]
        key = (row["machine_id"], supplier_id)
        plan.setdefault(key, []).append({
            "sku_id": row["sku_id"],
            "sku_name": row["name"],
            "reorder_qty": fill_qty,
            "cost_fen": sku.get("cost_fen", 100),
        })

    if not plan:
        print(f"  ℹ️  无法确定供应商信息，跳过补货")
        return summary

    created_orders: list[dict] = []
    for (machine_id, supplier_id), items in sorted(plan.items()):
        total_cost = sum(it["reorder_qty"] * it["cost_fen"] for it in items) / 100
        items_txt = ", ".join(f"{it['sku_name']} ×{it['reorder_qty']}" for it in items)
        print(f"  📨 {machine_id}/{supplier_id}: {items_txt}  (¥{total_cost:.0f})")
        po = await create_direct_purchase_order(client, supplier_id, machine_id, items)
        if po:
            created_orders.append(po)

    if not created_orders:
        print(f"  ℹ️  未创建采购单（建议使用 --force 强制补货）")
        return summary

    summary["orders_created"] = len(created_orders)
    print(f"\n  {_c(f'✅ 已创建 {len(created_orders)} 张采购单', '32')}:")
    for order in created_orders:
        items_txt = ", ".join(
            f"{it.get('sku_name', it.get('sku_id', '?'))} ×{it.get('qty', '?')}"
            for it in order.get("items", [])
        )
        print(f"     {order['id']}  {order.get('supplier_id', '?')}  [{items_txt}]")

    # ── 5. Simulate delivery ─────────────────────────────────────────────────
    print(f"\n  {_c(f'⏳ 模拟运输中 ({restock_delay_s}s)...', '90')}")
    await asyncio.sleep(restock_delay_s)

    restocked: list[str] = []
    for order in created_orders:
        oid = order["id"]
        r2 = await client.post(
            f"{SC_URL}/purchase-orders/{oid}/advance",
            json={"to_status": "stocked"},
        )
        if r2.status_code == 200:
            restocked.append(oid)
            result2 = r2.json()
            for it in result2.get("items", []):
                print(f"  📦 {it.get('sku_name', it.get('sku_id', '?'))}: "
                      f"{it.get('qty_before', '?')} → {it.get('qty_after', '?')}")
        else:
            print(f"  ⚠️  收货失败 {oid}: {r2.text[:80]}")

    summary["restocked"] = len(restocked)

    # ── 6. Updated inventory for previously-low lanes ───────────────────────
    if restocked:
        print(f"\n  {_c('📊 补货后库存快照（原低库存货道）:', '36')}")
        for row in low_inv:
            r3 = await client.get(
                f"{SC_URL}/inventory",
                params={"machine_id": row["machine_id"]},
            )
            if r3.status_code == 200:
                fresh = {
                    f"{x['machine_id']}/{x['lane_id']}": x
                    for x in r3.json()
                }
                key = f"{row['machine_id']}/{row['lane_id']}"
                if key in fresh:
                    new_row = fresh[key]
                    bar = _stock_bar(new_row["qty"], new_row.get("min_qty", 0), new_row.get("capacity", 15))
                    delta = new_row["qty"] - row["qty"]
                    delta_str = _c(f"+{delta}", "32") if delta > 0 else str(delta)
                    print(f"     {key}  {row['name']:<22} {row['qty']} → {new_row['qty']}  ({delta_str})  {bar}")

    print(f"\n  {_c('─'*58, '90')}")
    print(f"  周期完成: 低库存 {summary['low_stock']} | "
          f"采购单 {summary['orders_created']} | "
          f"已补货 {summary['restocked']} | "
          f"预订单 {summary['preorders']}")
    return summary


# --------------------------------------------------------------------------- #
# Monitor loop (used both standalone and embedded in simulate.py)
# --------------------------------------------------------------------------- #

async def run_monitor(
    interval_s: float = 90.0,
    dry_run: bool = False,
    once: bool = False,
    restock_delay_s: int = 30,
    force: bool = False,
    initial_delay_s: float = 0.0,
) -> None:
    """Async monitor loop. Call from simulate.py via asyncio.create_task()."""
    print(_c("📦 库存监控 & 自动补货 Agent 已启动", "1;36"))
    print(f"   间隔: {interval_s}s | 到货延迟: {restock_delay_s}s | "
          f"dry_run={dry_run} | force={force}")
    print(f"   SC: {SC_URL}")

    if initial_delay_s > 0:
        print(f"   初始等待 {initial_delay_s}s 后开始首次检查...")
        await asyncio.sleep(initial_delay_s)

    async with httpx.AsyncClient(timeout=30) as client:
        cycle = 0
        while True:
            cycle += 1
            try:
                await check_and_replenish(client, dry_run=dry_run,
                                          restock_delay_s=restock_delay_s,
                                          force=force)
            except asyncio.CancelledError:
                print(_c("\n📦 库存监控已停止", "90"))
                return
            except Exception as exc:
                print(f"  {_c('💥 监控异常:', '31')} {exc}")

            if once:
                break

            print(f"\n  📦 下次库存检查: {interval_s:.0f}s 后...")
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                print(_c("\n📦 库存监控已停止", "90"))
                return


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="贩卖机库存监控 & 自动补货")
    parser.add_argument("--interval", "-i", type=float, default=90.0,
                        help="检查间隔秒数（默认90）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只分析不下单")
    parser.add_argument("--once", action="store_true",
                        help="只运行一次")
    parser.add_argument("--restock-delay", type=int, default=30,
                        help="模拟到货延迟秒数（默认30）")
    parser.add_argument("--force", action="store_true",
                        help="强制下单（忽略最低起订量限制）")
    args = parser.parse_args()

    try:
        asyncio.run(run_monitor(
            interval_s=args.interval,
            dry_run=args.dry_run,
            once=args.once,
            restock_delay_s=args.restock_delay,
            force=args.force,
        ))
    except KeyboardInterrupt:
        print("\n已中断。")
