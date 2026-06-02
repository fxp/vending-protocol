#!/usr/bin/env python3
"""微米第三方 API 客户端骨架。

⚠️ sign() 是占位实现——必须先从微米对接人拿到正式的 SIGN 公式 + 测试向量，
替换 sign() 内部之后才能用。在那之前调任何接口都会签名失败。

依赖：仅 stdlib + requests
    pip install requests
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from typing import Any

import requests


DEFAULT_BASE_URL = "https://vm.weimi24.com/v8/third-center-web"
ORDER_QUERY_BASE_URL = "http://api.weimi24.com/v2022/third-center-web"


class WeimiClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        order_query_base_url: str = ORDER_QUERY_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.order_query_base_url = order_query_base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ---------- SIGN ----------
    def sign(self, method: str, path: str, body_or_query: str) -> str:
        """⚠️ 占位实现——把这里换成微米给的真正算法。

        method: "GET" / "POST"
        path:   "/ext/device-info"
        body_or_query: GET 的 query string ("a=1&b=2") 或 POST 的 raw JSON body

        Returns: SIGN 字符串
        """
        # placeholder — almost certainly wrong, replace with vendor demo
        ts = str(int(time.time() * 1000))
        nonce_for_sign = secrets.token_hex(12)
        raw = f"{self.app_id}{ts}{nonce_for_sign}{body_or_query}{self.app_secret}"
        return hashlib.sha256(raw.encode()).hexdigest().upper()

    def _headers(self, sig: str) -> dict[str, str]:
        return {
            "Client-Type": "EXTERNAL",
            "APP_ID": self.app_id,
            "SIGN": sig,
            "TIMESTAMP": str(int(time.time() * 1000)),
            "NONCE": secrets.token_hex(12),
            "Content-Type": "application/json",
        }

    # ---------- core requests ----------
    def get(self, path: str, params: dict[str, Any] | None = None, base_url: str | None = None) -> dict:
        url_base = (base_url or self.base_url).rstrip("/")
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        sig = self.sign("GET", path, query)
        url = f"{url_base}{path}"
        if query:
            url = f"{url}?{query}"
        r = self.session.get(url, headers=self._headers(sig), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        sig = self.sign("POST", path, raw)
        url = f"{self.base_url}{path}"
        r = self.session.post(url, data=raw.encode("utf-8"), headers=self._headers(sig), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ---------- domain methods ----------
    def device_profile(self, device_codes: str = "") -> dict:
        return self.get("/ext/device-profile", {"deviceCodes": device_codes})

    def device_info(self, device_codes: str) -> dict:
        return self.get("/ext/device-info", {"deviceCodes": device_codes})

    def notify_shipment(
        self,
        user_id: str,
        trade_no: str,
        device_code: str,
        aisle_goods: list[dict],
        pay_channel_code_int: int = 11001,
        auth_type: int = 7,
        pay_end_time: int = 0,
    ) -> dict:
        return self.post(
            "/ext/notify-shipment",
            {
                "userId": user_id,
                "tradeNo": trade_no,
                "deviceCode": device_code,
                "aisleGoodsList": aisle_goods,
                "payChannelCodeInt": pay_channel_code_int,
                "authType": auth_type,
                "payEndTime": pay_end_time,
            },
        )

    def query_order_list(self, trade_no: str, device_code: str) -> dict:
        return self.get(
            "/ext/query-order-list",
            {"tradeNo": trade_no, "deviceCode": device_code},
            base_url=self.order_query_base_url,
        )

    def send_serial_cmd(self, device_code: str, address: str, serial_cmd: str) -> dict:
        return self.post(
            "/ext/sendSerialCmd",
            {"deviceCode": device_code, "address": address, "serialCmd": serial_cmd},
        )


# ---------- CLI for quick checks ----------
def _from_env() -> WeimiClient:
    app_id = os.environ.get("WEIMI_APP_ID")
    app_secret = os.environ.get("WEIMI_APP_SECRET")
    if not (app_id and app_secret):
        print(
            "ERROR: set WEIMI_APP_ID and WEIMI_APP_SECRET in env first.\n"
            "  Also note: sign() is a placeholder until you replace it with\n"
            "  vendor's algorithm — calls will fail with bad signature.",
            file=sys.stderr,
        )
        sys.exit(2)
    return WeimiClient(app_id=app_id, app_secret=app_secret)


def main() -> int:
    p = argparse.ArgumentParser(description="Weimi vending API quick client")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("profile", help="GET /ext/device-profile")
    s.add_argument("--codes", default="", help="comma-separated device codes (default: all)")

    s = sub.add_parser("info", help="GET /ext/device-info")
    s.add_argument("--codes", required=True)

    s = sub.add_parser("dispense", help="POST /ext/notify-shipment")
    s.add_argument("--device", required=True)
    s.add_argument("--aisle", required=True, help='e.g. 0-A01')
    s.add_argument("--goods-id", required=True)
    s.add_argument("--price", type=int, default=1, help="in fen (¥0.01 = 1)")
    s.add_argument("--user", default="cli")
    s.add_argument("--trade-no", default=None, help="default: cli-<unix-ts-ms>")

    s = sub.add_parser("order", help="GET query-order-list")
    s.add_argument("--device", required=True)
    s.add_argument("--trade-no", required=True)

    s = sub.add_parser("serial", help="POST /ext/sendSerialCmd")
    s.add_argument("--device", required=True)
    s.add_argument("--address", required=True)
    s.add_argument("--cmd", required=True, help='raw hex like "EE010...1122"')

    s = sub.add_parser("sign", help="(debug) print SIGN for given inputs")
    s.add_argument("method")
    s.add_argument("path")
    s.add_argument("body_or_query")

    args = p.parse_args()
    c = _from_env()

    if args.cmd == "profile":
        out = c.device_profile(args.codes)
    elif args.cmd == "info":
        out = c.device_info(args.codes)
    elif args.cmd == "dispense":
        trade_no = args.trade_no or f"cli-{int(time.time() * 1000)}"
        out = c.notify_shipment(
            user_id=args.user,
            trade_no=trade_no,
            device_code=args.device,
            aisle_goods=[{"aisleCode": args.aisle, "goodsId": args.goods_id, "price": args.price, "count": 1}],
        )
        out["_tradeNo"] = trade_no
    elif args.cmd == "order":
        out = c.query_order_list(args.trade_no, args.device)
    elif args.cmd == "serial":
        out = c.send_serial_cmd(args.device, args.address, args.cmd)
    elif args.cmd == "sign":
        print(c.sign(args.method, args.path, args.body_or_query))
        return 0
    else:
        p.error("unknown cmd")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
