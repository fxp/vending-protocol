#!/usr/bin/env python3
"""
Inventory monitor with scheduled batch ordering and cross-machine demand aggregation.

Key behaviours
──────────────
• 友宝 universal distributor — auto-registered at startup; covers all SKUs,
  no minimum order, used as fallback when brand-supplier MOQ is not reached
• Unified order time — inventory is scanned every --check-interval seconds,
  but orders are only placed every --order-interval seconds (all suppliers
  ordered simultaneously, simulating a daily procurement window)
• Aggregated demand — all machines' needs for the same SKU are summed before
  supplier selection: if the combined value meets the brand supplier's MOQ,
  use the brand; otherwise route the entire SKU to 友宝

Usage:
    python inventory_monitor.py                       # defaults
    python inventory_monitor.py --check-interval 30 --order-interval 120
    python inventory_monitor.py --dry-run             # scan only, no orders
    python inventory_monitor.py --once                # one scan + one order cycle
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

YOUBAO_ID = "YOUBAO"
YOUBAO_INFO = {
    "id": YOUBAO_ID,
    "name": "友宝（中国）零售科技有限公司",
    "contact": "supply@youbao.com.cn",
    "lead_time_days": 1,
    "min_order_yuan": 0,        # no minimum order
}

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _bar(qty: int, min_qty: int, capacity: int, width: int = 10) -> str:
    cap = max(capacity, 1)
    filled = round(min(qty, cap) / cap * width)
    bar = "█" * filled + "░" * (width - filled)
    code = "31" if qty == 0 else ("33" if qty < min_qty else "32")
    return _c(f"[{bar}]", code)

def _status(qty: int, min_qty: int) -> str:
    if qty == 0:
        return _c("缺货", "1;31")
    if qty < min_qty:
        return _c("低库存", "1;33")
    return _c("正常", "32")

def _yuan(fen: int) -> str:
    return f"¥{fen/100:.0f}"


# --------------------------------------------------------------------------- #
# Setup: register 友宝 if absent
# --------------------------------------------------------------------------- #

async def ensure_youbao(client: httpx.AsyncClient) -> None:
    r = await client.get(f"{SC_URL}/suppliers/{YOUBAO_ID}")
    if r.status_code == 200:
        return
    r2 = await client.post(f"{SC_URL}/suppliers", json=YOUBAO_INFO)
    if r2.status_code in (200, 201):
        print(f"  {_c('✅ 已注册供应商: 友宝（中国）零售科技有限公司', '32')}")
    else:
        print(f"  ⚠️  注册友宝失败 [{r2.status_code}]: {r2.text[:80]}")


# --------------------------------------------------------------------------- #
# Data fetching
# --------------------------------------------------------------------------- #

async def fetch_all_inventory(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{SC_URL}/machines")
    r.raise_for_status()
    machines = r.json()
    rows: list[dict] = []
    for m in machines:
        r2 = await client.get(f"{SC_URL}/inventory", params={"machine_id": m["id"]})
        if r2.status_code == 200:
            for row in r2.json():
                row["machine_name"] = m.get("name", m["id"])
                rows.append(row)
    return rows


async def fetch_sku_map(client: httpx.AsyncClient) -> dict[str, dict]:
    r = await client.get(f"{SC_URL}/skus")
    return {s["sku_id"]: s for s in r.json()} if r.status_code == 200 else {}


async def fetch_supplier_map(client: httpx.AsyncClient) -> dict[str, dict]:
    r = await client.get(f"{SC_URL}/suppliers")
    return {s["id"]: s for s in r.json()} if r.status_code == 200 else {}


async def fetch_pending_preorders(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{SC_URL}/preorders")
    if r.status_code != 200:
        return []
    return [p for p in r.json() if p.get("status", "pending") not in ("fulfilled", "cancelled")]


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #

def print_inventory_table(inv: list[dict]) -> None:
    print(f"\n  {'机器':<10} {'货道':<5} {'商品':<22} {'库存':>4}/cap  {'状态':>8}")
    print(f"  {'─'*62}")
    prev_machine = None
    for row in inv:
        if row["machine_id"] != prev_machine:
            if prev_machine is not None:
                print()
            prev_machine = row["machine_id"]
        qty = row["qty"]
        cap = row.get("capacity", 15)
        min_q = row.get("min_qty", 0)
        bar = _bar(qty, min_q, cap if isinstance(cap, int) else 15)
        st  = _status(qty, min_q)
        print(f"  {row['machine_id']:<10} {row['lane_id']:<5} {row['name']:<22} "
              f"{qty:>3}/{cap:<3}  {bar} {st}")


# --------------------------------------------------------------------------- #
# Core: aggregated order plan
# --------------------------------------------------------------------------- #

def build_aggregated_plan(
    low_inv: list[dict],
    sku_map: dict[str, dict],
    supplier_map: dict[str, dict],
) -> tuple[dict[tuple[str, str], list[dict]], dict[str, str]]:
    """
    Returns:
      plan       — {(machine_id, supplier_id): [order_items]}
      sku_routing — {sku_id: chosen_supplier_id}  (for display)

    Aggregation logic:
      For each SKU, sum fill quantities across ALL machines.
      If combined order value ≥ primary-supplier MOQ → use primary supplier.
      Otherwise → route the whole SKU to 友宝.
    """
    # Step 1: collect per-SKU needs (across all machines)
    sku_needs: dict[str, dict] = {}
    for item in low_inv:
        sku_id = item["sku_id"]
        fill = item.get("capacity", 15) - item["qty"]
        if fill <= 0:
            continue
        sku = sku_map.get(sku_id, {})
        if sku_id not in sku_needs:
            sku_needs[sku_id] = {
                "sku_name": item["name"],
                "cost_fen": sku.get("cost_fen", 100),
                "primary_supplier": sku.get("supplier_id", YOUBAO_ID),
                "machines": [],
            }
        sku_needs[sku_id]["machines"].append({
            "machine_id": item["machine_id"],
            "lane_id": item["lane_id"],
            "fill_qty": fill,
        })

    # Step 2: supplier routing per SKU
    sku_routing: dict[str, str] = {}
    for sku_id, need in sku_needs.items():
        total_fill = sum(m["fill_qty"] for m in need["machines"])
        total_cost_fen = total_fill * need["cost_fen"]
        primary = need["primary_supplier"]
        primary_info = supplier_map.get(primary, {})
        min_fen = primary_info.get("min_order_yuan", 0) * 100

        if primary in supplier_map and (min_fen == 0 or total_cost_fen >= min_fen):
            chosen = primary
        else:
            chosen = YOUBAO_ID
        sku_routing[sku_id] = chosen

    # Step 3: build per-(machine, supplier) order lists
    plan: dict[tuple[str, str], list[dict]] = {}
    for sku_id, need in sku_needs.items():
        chosen = sku_routing[sku_id]
        for m in need["machines"]:
            key = (m["machine_id"], chosen)
            plan.setdefault(key, []).append({
                "sku_id": sku_id,
                "sku_name": need["sku_name"],
                "reorder_qty": m["fill_qty"],
                "cost_fen": need["cost_fen"],
            })

    return plan, sku_routing


def print_aggregated_plan(
    low_inv: list[dict],
    sku_needs_summary: dict[str, dict],   # sku_id -> {total_fill, total_cost_fen, primary, chosen, machines}
    supplier_map: dict[str, dict],
) -> None:
    """Display the aggregation logic before placing orders."""
    print(f"\n  {_c('📊 跨机器需求聚合 & 供应商选择', '1;36')}")
    print(f"  {'─'*62}")
    print(f"  {'商品':<22} {'需求机器':<22} {'合计':<6} {'合计金额':>8}  {'供应商'}")
    print(f"  {'─'*62}")
    for sku_id, info in sorted(sku_needs_summary.items(), key=lambda x: x[1]["total_cost_fen"], reverse=True):
        machines_txt = "+".join(f"{m['machine_id']}({m['fill_qty']})" for m in info["machines"])
        chosen_name = supplier_map.get(info["chosen"], {}).get("name", info["chosen"])
        primary_name = supplier_map.get(info["primary"], {}).get("name", info["primary"])
        # Show why this supplier was chosen
        if info["chosen"] == info["primary"]:
            sup_note = _c(f"✓ {chosen_name[:10]}", "32")
        else:
            primary_min = supplier_map.get(info["primary"], {}).get("min_order_yuan", 0)
            sup_note = _c(f"友宝↑ (原{primary_name[:6]} MOQ¥{primary_min}未达)", "33")
        print(f"  {info['sku_name']:<22} {machines_txt:<22} {info['total_fill']:>4}件 "
              f"{_yuan(info['total_cost_fen']):>8}  {sup_note}")


# --------------------------------------------------------------------------- #
# Ordering & delivery
# --------------------------------------------------------------------------- #

async def place_batch_orders(
    client: httpx.AsyncClient,
    plan: dict[tuple[str, str], list[dict]],
) -> list[dict]:
    """Place all orders simultaneously (unified order time)."""
    created: list[dict] = []
    for (machine_id, supplier_id), items in sorted(plan.items()):
        items_txt = ", ".join(f"{it['sku_name']} ×{it['reorder_qty']}" for it in items)
        total_cost = sum(it["reorder_qty"] * it["cost_fen"] for it in items)
        print(f"  📨 {machine_id} ← {supplier_id}: {items_txt}  ({_yuan(total_cost)})")

        po_items = [
            {"sku_id": it["sku_id"], "sku_name": it["sku_name"],
             "qty": it["reorder_qty"], "unit_cost_fen": it["cost_fen"]}
            for it in items
        ]
        r = await client.post(f"{SC_URL}/purchase-orders", json={
            "supplier_id": supplier_id,
            "machine_id": machine_id,
            "items": po_items,
        })
        if r.status_code in (200, 201):
            created.append(r.json())
        else:
            print(f"  ⚠️  下单失败 [{r.status_code}]: {r.text[:80]}")
    return created


async def deliver_batch_orders(
    client: httpx.AsyncClient,
    orders: list[dict],
    restock_delay_s: int,
) -> list[dict]:
    """Simulate delivery: wait, then advance all POs to stocked simultaneously."""
    print(f"\n  {_c(f'⏳ 模拟统一配送中 ({restock_delay_s}s)...', '90')}")
    await asyncio.sleep(restock_delay_s)

    restocked: list[dict] = []
    for po in orders:
        oid = po["id"]
        r = await client.post(
            f"{SC_URL}/purchase-orders/{oid}/advance",
            json={"to_status": "stocked"},
        )
        if r.status_code == 200:
            restocked.append(r.json())
        else:
            print(f"  ⚠️  收货失败 {oid}: {r.text[:60]}")
    return restocked


# --------------------------------------------------------------------------- #
# One full order cycle
# --------------------------------------------------------------------------- #

async def run_order_cycle(
    client: httpx.AsyncClient,
    all_inv: list[dict],
    sku_map: dict[str, dict],
    supplier_map: dict[str, dict],
    dry_run: bool,
    restock_delay_s: int,
) -> dict:
    """Build plan, display aggregation, place orders, deliver. Returns summary."""
    summary = {"orders_created": 0, "restocked": 0, "skus": 0}

    low_inv = [r for r in all_inv if r.get("low_stock")]
    if not low_inv:
        print(f"  {_c('✅ 所有货道库存充足，跳过本次补货', '32')}")
        return summary

    # Build and display aggregation
    plan, sku_routing = build_aggregated_plan(low_inv, sku_map, supplier_map)
    summary["skus"] = len(sku_routing)

    # Build summary dict for display
    sku_needs_summary: dict[str, dict] = {}
    for item in low_inv:
        sku_id = item["sku_id"]
        fill = item.get("capacity", 15) - item["qty"]
        if fill <= 0:
            continue
        sku = sku_map.get(sku_id, {})
        if sku_id not in sku_needs_summary:
            sku_needs_summary[sku_id] = {
                "sku_name": item["name"],
                "cost_fen": sku.get("cost_fen", 100),
                "primary": sku.get("supplier_id", YOUBAO_ID),
                "chosen": sku_routing.get(sku_id, YOUBAO_ID),
                "total_fill": 0,
                "total_cost_fen": 0,
                "machines": [],
            }
        sku_needs_summary[sku_id]["total_fill"] += fill
        sku_needs_summary[sku_id]["total_cost_fen"] += fill * sku.get("cost_fen", 100)
        sku_needs_summary[sku_id]["machines"].append({
            "machine_id": item["machine_id"], "fill_qty": fill,
        })

    print_aggregated_plan(low_inv, sku_needs_summary, supplier_map)

    if dry_run:
        print(f"\n  {_c('[dry-run] 分析完成，跳过下单', '90')}")
        return summary

    # Place all orders at once (unified time)
    print(f"\n  {_c('🛒 统一补货时刻 — 批量下单', '1;33')}  [{ts()}]")
    orders = await place_batch_orders(client, plan)
    summary["orders_created"] = len(orders)

    if not orders:
        print(f"  ℹ️  未创建任何采购单")
        return summary

    print(f"\n  {_c(f'✅ 已创建 {len(orders)} 张采购单', '32')} (来自 "
          f"{len({po.get('supplier_id') for po in orders})} 个供应商)")

    # Deliver
    restocked = await deliver_batch_orders(client, orders, restock_delay_s)
    summary["restocked"] = len(restocked)

    return summary


# --------------------------------------------------------------------------- #
# Monitor loop
# --------------------------------------------------------------------------- #

async def run_monitor(
    check_interval: float = 30.0,
    order_interval: float = 120.0,
    dry_run: bool = False,
    once: bool = False,
    restock_delay_s: int = 30,
    initial_delay_s: float = 0.0,
) -> None:
    """
    Two-speed loop:
      - Every check_interval: scan inventory, display status, preorders
      - Every order_interval: run aggregated order cycle (all suppliers at once)
    """
    print(_c("📦 库存监控 & 统一补货 Agent 已启动", "1;36"))
    print(f"   扫描间隔: {check_interval}s | 补货间隔: {order_interval}s | "
          f"配送延迟: {restock_delay_s}s | dry_run={dry_run}")
    print(f"   SC: {SC_URL}")

    if initial_delay_s > 0:
        print(f"   初始等待 {initial_delay_s:.0f}s 后开始首次扫描...")
        await asyncio.sleep(initial_delay_s)

    async with httpx.AsyncClient(timeout=30) as client:
        # One-time setup
        await ensure_youbao(client)

        last_order_at: float = 0.0  # trigger first order cycle immediately
        scan_count = 0

        while True:
            scan_count += 1
            now_ts = __import__("time").time()

            # ── Inventory scan ───────────────────────────────────────────────
            print(f"\n{'═'*62}")
            print(f"  {_c('📦 库存扫描', '1;34')}  [{ts()}]  (第 {scan_count} 次)")
            print(f"{'═'*62}")

            try:
                all_inv, sku_map, supplier_map, preorders = await asyncio.gather(
                    fetch_all_inventory(client),
                    fetch_sku_map(client),
                    fetch_supplier_map(client),
                    fetch_pending_preorders(client),
                )
            except Exception as e:
                print(f"  {_c('💥 获取数据失败:', '31')} {e}")
                if once:
                    break
                await asyncio.sleep(check_interval)
                continue

            print_inventory_table(all_inv)

            low_inv = [r for r in all_inv if r.get("low_stock")]

            # Preorders summary
            if preorders:
                sku_demand: dict[str, int] = {}
                for p in preorders:
                    k = p.get("sku_name") or p.get("sku_id", "?")
                    sku_demand[k] = sku_demand.get(k, 0) + p.get("qty", 1)
                print(f"\n  {_c(f'📋 待履行预订单 ({len(preorders)} 条):', '1;35')}")
                for name, qty in sorted(sku_demand.items(), key=lambda x: -x[1]):
                    print(f"     • {name}: {qty} 件")

            # Low stock summary
            if low_inv:
                print(f"\n  {_c(f'⚠️  {len(low_inv)} 个需补货货道:', '1;33')}")
                for row in low_inv:
                    fill = row.get("capacity", 15) - row["qty"]
                    print(f"     {row['machine_id']}/{row['lane_id']} "
                          f"{row['name']:<22} 当前 {row['qty']} → 补 {fill}")

            # ── Order cycle check ────────────────────────────────────────────
            time_since_order = now_ts - last_order_at
            time_to_next     = max(0.0, order_interval - time_since_order)

            if time_since_order >= order_interval:
                last_order_at = now_ts
                print(f"\n{'─'*62}")
                print(f"  {_c('🕐 统一补货时刻到达', '1;33')}  [{ts()}]")
                print(f"{'─'*62}")
                try:
                    summary = await run_order_cycle(
                        client, all_inv, sku_map, supplier_map, dry_run, restock_delay_s
                    )
                    if not dry_run and summary["restocked"] > 0:
                        # Show updated snapshot for previously-low lanes
                        print(f"\n  {_c('📊 补货后库存快照:', '36')}")
                        fresh_inv = await fetch_all_inventory(client)
                        fresh = {f"{r['machine_id']}/{r['lane_id']}": r for r in fresh_inv}
                        for row in low_inv:
                            key = f"{row['machine_id']}/{row['lane_id']}"
                            if key in fresh:
                                nw = fresh[key]
                                delta = nw["qty"] - row["qty"]
                                delta_s = _c(f"+{delta}", "32") if delta > 0 else str(delta)
                                b = _bar(nw["qty"], nw.get("min_qty", 0), nw.get("capacity", 15))
                                print(f"     {key}  {row['name']:<22} "
                                      f"{row['qty']} → {nw['qty']}  ({delta_s})  {b}")
                    print(f"\n  周期完成: 低库存 {len(low_inv)} | "
                          f"采购单 {summary['orders_created']} | "
                          f"已补货 {summary['restocked']} | "
                          f"预订单 {len(preorders)}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"  {_c('💥 补货周期异常:', '31')} {e}")
                    import traceback; traceback.print_exc()
            else:
                status = (f"  {_c('⏰ 下次统一补货:', '90')} "
                          f"{time_to_next:.0f}s 后  |  "
                          f"{'需补货' if low_inv else '库存充足'}")
                print(f"\n{status}")

            if once:
                break

            print(f"  {_c(f'💤 下次扫描: {check_interval:.0f}s 后...', '90')}")
            try:
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                print(_c("\n📦 库存监控已停止", "90"))
                return


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="贩卖机库存监控 & 统一批量补货")
    parser.add_argument("--check-interval", type=float, default=30.0,
                        help="库存扫描间隔秒数（默认30）")
    parser.add_argument("--order-interval", type=float, default=120.0,
                        help="统一补货间隔秒数（默认120）")
    parser.add_argument("--restock-delay", type=int, default=30,
                        help="模拟配送延迟秒数（默认30）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只分析不下单")
    parser.add_argument("--once", action="store_true",
                        help="执行一次扫描+补货后退出")
    args = parser.parse_args()

    try:
        asyncio.run(run_monitor(
            check_interval=args.check_interval,
            order_interval=args.order_interval,
            dry_run=args.dry_run,
            once=args.once,
            restock_delay_s=args.restock_delay,
        ))
    except KeyboardInterrupt:
        print("\n已中断。")
