#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aster-maker15.py  (Clean & Full)
- เปิดคู่แบบ Maker: BUY Spot + SELL Futures (short) ด้วย GTX (ไม่กินคิว)
- แนบโพสิชันที่มี (attach-existing): เติม/ตัดให้เข้าเป้าทุน (target qty) พร้อมกันสองฝั่ง
- วัด 'กำไรสุทธิหลังปิด' = open_basis - open_fee + close_gain - close_fee - slippage
- ถึงเป้า → ปิดคู่ (maker-first, มี IOC/MARKET fallback) แล้วเปิดใหม่อัตโนมัติ (ถ้า --always-reopen)
- บังคับกริดทุกคำสั่ง (qty ตาม LOT_SIZE.step, price ตาม tick) เพื่อกัน "-1111 Precision"
- ตรวจ notional ขั้นต่ำ 5 USDT ทั้ง Spot/Futures ก่อนยิง
- ป้องกันปิดในตลาดบาง (สเปรด/เด็ปธ์) + ยืนยันกำไรต่อเนื่อง (confirm-hits)
- มี retry/backoff/cooldown + reset state อัตโนมัติเมื่อ error
- Log สี: ฟ้า(ข้อมูล), เขียว(ซื้อ/กำไร), แดง(ขาย/ขาดทุน/ข้อผิดพลาด), ส้ม(เตือน)

ENV ที่ต้องมี: ASTERDEX_API_KEY, ASTERDEX_API_SECRET
"""

import os, time, hmac, hashlib, argparse, logging, requests, math, datetime as dt
from decimal import Decimal, ROUND_DOWN, getcontext
from urllib.parse import urlencode

getcontext().prec = 28

# ---------- endpoints ----------
SAPI = "https://sapi.asterdex.com"
FAPI = "https://fapi.asterdex.com"

# ---------- ENV ----------
API_KEY    = os.environ.get("ASTERDEX_API_KEY")
API_SECRET = os.environ.get("ASTERDEX_API_SECRET","").encode("utf-8")

# ---------- ANSI colors ----------
C = {
    "RST":"\033[0m",
    "BLU":"\033[36m",
    "GRN":"\033[32m",
    "RED":"\033[31m",
    "YEL":"\033[33m",
    "B":"\033[1m",
}

def cinfo(s):  return C["BLU"]+s+C["RST"]
def cgood(s):  return C["GRN"]+s+C["RST"]
def cbad(s):   return C["RED"]+s+C["RST"]
def cwarn(s):  return C["YEL"]+s+C["RST"]

# ---------- decimal helpers (precision guard) ----------
def D(x): return Decimal(str(x))

def _decimals(step: Decimal) -> int:
    e = -step.as_tuple().exponent
    return max(0, e)

def _quant_floor(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0: return value
    n = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (n * step).quantize(step, rounding=ROUND_DOWN)

def _fmt(value: Decimal, step: Decimal) -> str:
    d = _decimals(step)
    q = _quant_floor(value, step)
    return f"{q:.{d}f}"

# ---------- time & signing ----------
def now_ms(): return int(time.time()*1000)
def now_utc_str(): return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def sign(q: dict) -> str:
    s = urlencode(q, doseq=True)
    sig = hmac.new(API_SECRET, s.encode(), hashlib.sha256).hexdigest()
    return f"{s}&signature={sig}"

def _hdr():
    if not API_KEY: raise RuntimeError("Missing ASTERDEX_API_KEY")
    return {"X-MBX-APIKEY": API_KEY, "User-Agent": "AsterMaker15/1.0"}

def _server_time(base):
    path = "/api/v1/time" if base==SAPI else "/fapi/v1/time"
    try:
        r = requests.get(base+path, headers=_hdr(), timeout=5)
        return int(r.json().get("serverTime"))
    except Exception:
        return None

def _req(base, path, method="GET", params=None, signed=False, timeout=10, retries=2):
    params = params or {}
    last = None
    for i in range(retries+1):
        try:
            if signed:
                if not API_SECRET: raise RuntimeError("Missing ASTERDEX_API_SECRET")
                p = dict(params)
                p.setdefault("recvWindow", 20000)
                p["timestamp"] = now_ms()
                url = f"{base}{path}?{sign(p)}"
                r = requests.request(method, url, headers=_hdr(), timeout=timeout)
            else:
                url = f"{base}{path}"
                r = requests.request(method, url, headers=_hdr(), params=params, timeout=timeout)
            last = r
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "code" in data and str(data.get("code")) not in ("0","200"):
                    code = str(data.get("code"))
                    if code in ("-1021","-1000","-429"):
                        time.sleep(1.0 + i)
                        _server_time(base)
                        continue
                    raise RuntimeError(f"API error: {data}")
                return data
            else:
                if r.status_code in (400,401,418,429,500):
                    time.sleep(1.0 + i)
                    continue
                raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}: {r.text}")
        except Exception as e:
            last = e
            time.sleep(1.0 + i)
            continue
    if isinstance(last, requests.Response):
        raise RuntimeError(f"{method} {base}{path} -> HTTP {last.status_code}: {last.text}")
    raise RuntimeError(f"request failed: {last}")

# ---------- exchange info ----------
def _exinfo(base):
    path = "/api/v1/exchangeInfo" if base==SAPI else "/fapi/v1/exchangeInfo"
    return _req(base, path)

def exinfo_spot(symbol):
    d = _exinfo(SAPI)
    for s in d.get("symbols", []):
        if s.get("symbol")==symbol: return s
    raise RuntimeError(f"Spot symbol not found: {symbol}")

def exinfo_fut(symbol):
    d = _exinfo(FAPI)
    for s in d.get("symbols", []):
        if s.get("symbol")==symbol: return s
    raise RuntimeError(f"Futures symbol not found: {symbol}")

def _flt(info, key):
    for f in info.get("filters", []):
        if f.get("filterType")==key: return f
    return {}

def get_tick_lot_notional(info):
    tick = D(_flt(info,"PRICE_FILTER").get("tickSize","0.0001"))
    lot  = _flt(info,"LOT_SIZE")
    step = D(lot.get("stepSize","0.01"))
    minq = D(lot.get("minQty","0.01"))
    fmn  = _flt(info,"MIN_NOTIONAL")
    min_notional = D(fmn.get("minNotional", fmn.get("notional", "5")))
    if min_notional <= 0: min_notional = D("5")
    return tick, step, minq, min_notional

# ---------- market data ----------
def depth(base, symbol, limit=5):
    path = "/api/v1/depth" if base==SAPI else "/fapi/v1/depth"
    d = _req(base, path, params={"symbol":symbol,"limit":limit})
    def conv(key): return [(D(px), D(q)) for px,q in d.get(key,[])]
    return conv("bids"), conv("asks")

def spot_last(symbol):
    d = _req(SAPI,"/api/v1/ticker/price",params={"symbol":symbol})
    return D(d.get("price","0"))

def mark_price(symbol):
    d = _req(FAPI,"/fapi/v1/premiumIndex",params={"symbol":symbol})
    return D(d.get("markPrice","0"))

# ---------- balances/positions ----------
def spot_bal(asset):
    d = _req(SAPI,"/api/v1/account",signed=True)
    for b in d.get("balances", []):
        if b.get("asset")==asset:
            return D(b.get("free","0")) + D(b.get("locked","0"))
    return D("0")

def usdt_free():
    d = _req(SAPI,"/api/v1/account",signed=True)
    for b in d.get("balances", []):
        if b.get("asset")=="USDT":
            return D(b.get("free","0"))
    return D("0")

def fut_pos(symbol):
    d = _req(FAPI,"/fapi/v2/positionRisk",params={"symbol":symbol},signed=True)
    rows = d if isinstance(d,list) else [d]
    p = next((x for x in rows if x.get("symbol")==symbol), None)
    if not p: return None
    return {
        "qty": D(p.get("positionAmt","0") or "0"),
        "entry": D(p.get("entryPrice","0") or "0"),
        "mark": D(p.get("markPrice","0") or "0"),
        "lev": D(p.get("leverage","0") or "0"),
        "liq": D(p.get("liquidationPrice","0") or "0")
    }

# ---------- orders (ทุกคำสั่งบังคับกริดก่อนส่ง) ----------
def spot_limit_gtx(symbol, side, qty, px, s_step, s_tick):
    p = {"symbol":symbol,"side":side,"type":"LIMIT","timeInForce":"GTX",
         "quantity":_fmt(qty, s_step),"price":_fmt(px, s_tick),"newOrderRespType":"RESULT"}
    logging.info(cgood(f"SPOT {side} {_fmt(qty,s_step)} @ {_fmt(px,s_tick)} (LIMIT/GTX)"))
    return _req(SAPI,"/api/v1/order","POST",p,True)

def fut_limit_gtx(symbol, side, qty, px, f_step, f_tick, reduce_only=True):
    p = {"symbol":symbol,"side":side,"type":"LIMIT","timeInForce":"GTX",
         "quantity":_fmt(qty, f_step),"price":_fmt(px, f_tick),
         "reduceOnly":"true" if reduce_only else "false","newOrderRespType":"RESULT"}
    logging.info(cgood(f"FUT  {side} {_fmt(qty,f_step)} @ {_fmt(px,f_tick)} (LIMIT/GTX, ro={reduce_only})"))
    return _req(FAPI,"/fapi/v1/order","POST",p,True)

def spot_limit_ioc(symbol, side, qty, px, s_step, s_tick):
    p = {"symbol":symbol,"side":side,"type":"LIMIT","timeInForce":"IOC",
         "quantity":_fmt(qty, s_step),"price":_fmt(px, s_tick),"newOrderRespType":"RESULT"}
    logging.warning(cwarn(f"SPOT {side} {_fmt(qty,s_step)} @ {_fmt(px,s_tick)} (IOC)"))
    return _req(SAPI,"/api/v1/order","POST",p,True)

def fut_limit_ioc(symbol, side, qty, px, f_step, f_tick, reduce_only=True):
    p = {"symbol":symbol,"side":side,"type":"LIMIT","timeInForce":"IOC",
         "quantity":_fmt(qty, f_step),"price":_fmt(px, f_tick),
         "reduceOnly":"true" if reduce_only else "false","newOrderRespType":"RESULT"}
    logging.warning(cwarn(f"FUT  {side} {_fmt(qty,f_step)} @ {_fmt(px,f_tick)} (IOC, ro={reduce_only})"))
    return _req(FAPI,"/fapi/v1/order","POST",p,True)

def spot_market_sell(symbol, qty, s_step):
    p = {"symbol":symbol,"side":"SELL","type":"MARKET",
         "quantity":_fmt(qty, s_step),"newOrderRespType":"ACK"}
    logging.warning(cbad(f"SPOT SELL {_fmt(qty,s_step)} (MARKET)"))
    return _req(SAPI,"/api/v1/order","POST",p,True)

def fut_market_close(symbol, qty, side, f_step):
    p = {"symbol":symbol,"side":side,"type":"MARKET","quantity":_fmt(qty, f_step),
         "reduceOnly":"true","newOrderRespType":"RESULT"}
    logging.warning(cbad(f"FUT  {side} {_fmt(qty,f_step)} (MARKET ro=true)"))
    return _req(FAPI,"/fapi/v1/order","POST",p,True)

# ---------- margin/leverage ----------
def set_isolated_and_leverage(symbol, isolated, lev):
    try:
        _req(FAPI,"/fapi/v1/marginType","POST",
             {"symbol":symbol,"marginType":"ISOLATED" if isolated else "CROSSED"},True)
    except Exception as e:
        logging.info(cwarn(f"margin/leverage set fail (non-fatal): {e}"))
    try:
        _req(FAPI,"/fapi/v1/leverage","POST",{"symbol":symbol,"leverage":int(lev)},True)
    except Exception as e:
        logging.info(cwarn(f"leverage set fail (non-fatal): {e}"))

# ---------- sizing / economics ----------
def target_qty_from_capital(capital_usd: Decimal, spot_px: Decimal, s_step: Decimal, whole_qty=False)->Decimal:
    if spot_px<=0: return D("0")
    qty = capital_usd/spot_px
    if whole_qty:
        qty = D(int(qty))  # ปัดเป็นจำนวนเต็ม
    return _quant_floor(qty, s_step)

def meets_notional(qty:Decimal, px:Decimal, min_notional:Decimal)->bool:
    return qty>0 and (qty*px)>=min_notional

def est_open_basis_minus_fee(qty, s_px, f_px, maker_spot_bps, maker_fut_bps):
    fee = qty*s_px*D(maker_spot_bps)/D(10000) + qty*f_px*D(maker_fut_bps)/D(10000)
    basis = (f_px - s_px)*qty
    return basis - fee

def est_close_gain_minus_fee_slip(qty, s_px, f_px, taker_spot_bps, taker_fut_bps, slippage_bps):
    fee = qty*s_px*D(taker_spot_bps)/D(10000) + qty*f_px*D(taker_fut_bps)/D(10000)
    slip = qty*(s_px+f_px)/D(2) * D(slippage_bps)/D(10000)
    basis = -(f_px - s_px)*qty
    return basis - fee - slip

# ---------- helpers ----------
def s_bid(sbids): return sbids[0][0] if sbids else D("0")
def s_ask(sasks): return sasks[0][0] if sasks else D("0")
def f_bid(fbids): return fbids[0][0] if fbids else D("0")
def f_ask(fasks): return fasks[0][0] if fasks else D("0")

# ---------- attach/open pair ----------
def attach_or_open_pair(args, s_step,f_step, s_tick,f_tick, s_min_notional,f_min_notional):
    sbids,sasks = depth(SAPI,args.symbol,args.depth_limit)
    fbids,fasks = depth(FAPI,args.symbol,args.depth_limit)
    if not (sbids and sasks and fbids and fasks):
        raise RuntimeError("orderbook empty")
    # price for maker open: BUY spot near bid (<=bid), SELL fut near ask (>=ask)
    s_px = sbids[max(0,args.nth-1)][0]
    f_px = fasks[max(0,args.nth-1)][0]

    spot_now = spot_bal(args.asset)
    pos = fut_pos(args.symbol); fut_now = abs(pos["qty"]) if pos else D("0")

    target = target_qty_from_capital(D(args.capital), s_px, s_step, whole_qty=args.whole_qty)

    need_s = max(target - spot_now, D("0"))
    need_f = max(target - fut_now,  D("0"))
    over_s = max(spot_now - target, D("0"))
    over_f = max(fut_now  - target, D("0"))

    # add spot (BUY maker)
    cash = usdt_free()
    add_s = min(need_s, _quant_floor(cash/s_px, s_step))
    if add_s>0 and meets_notional(add_s,s_px,s_min_notional):
        logging.info(cgood(f"OPEN[MKR] Spot BUY {_fmt(add_s,s_step)} @ {_fmt(s_px,s_tick)}"))
        spot_limit_gtx(args.symbol,"BUY",add_s,s_px,s_step,s_tick)
        cash -= add_s*s_px

    # add fut (SELL maker, non-RO)
    add_f = _quant_floor(need_f, f_step)
    if add_f>0 and meets_notional(add_f,f_px,f_min_notional):
        logging.info(cgood(f"OPEN[MKR] Futures SELL {_fmt(add_f,f_step)} @ {_fmt(f_px,f_tick)}"))
        fut_limit_gtx(args.symbol,"SELL",add_f,f_px,f_step,f_tick,False)

    # trim excess
    if over_s>0:
        cut = _quant_floor(over_s, s_step)
        if cut>0:
            px = s_ask(sasks)
            if meets_notional(cut, px, s_min_notional):
                logging.info(cwarn(f"TRIM[MKR] Spot SELL {_fmt(cut,s_step)} @ {_fmt(px,s_tick)}"))
                spot_limit_gtx(args.symbol,"SELL",cut,px,s_step,s_tick)

    if over_f>0:
        cut = _quant_floor(over_f, f_step)
        if cut>0:
            px = f_bid(fbids)
            if meets_notional(cut, px, f_min_notional):
                logging.info(cwarn(f"TRIM[MKR] Fut BUY(ro) {_fmt(cut,f_step)} @ {_fmt(px,f_tick)}"))
                fut_limit_gtx(args.symbol,"BUY",cut,px,f_step,f_tick,True)

    # ประเมินคู่ที่พร้อมใช้หลังซิงก์
    spot_eff = spot_bal(args.asset)
    fut_eff  = abs((fut_pos(args.symbol) or {"qty":D("0")})["qty"])
    qty_eff  = min(spot_eff, fut_eff)

    open_basis = est_open_basis_minus_fee(qty_eff, s_px, f_px,
                                          args.maker_spot_bps, args.maker_fut_bps)
    return qty_eff, s_px, f_px, open_basis

# ---------- guard ก่อนปิด ----------
def guards_ok_for_close(args, qty, s_step,f_step, s_tick,f_tick):
    sbids,sasks = depth(SAPI,args.symbol,3)
    fbids,fasks = depth(FAPI,args.symbol,3)
    if not (sbids and sasks and fbids and fasks): return False
    s_ask_px = s_ask(sasks); f_bid_px = f_bid(fbids)
    mid = (s_ask_px + f_bid_px)/D(2) if s_ask_px>0 and f_bid_px>0 else D(0)
    spread_bps = ((s_ask_px - f_bid_px)/mid*D(10000)) if mid>0 else D(99999)

    sum_ask = sum([q for _,q in sasks[:2]])
    sum_bid = sum([q for _,q in fbids[:2]])

    depth_ok = (sum_ask >= qty*D(args.min_close_depth_mult)) and (sum_bid >= qty*D(args.min_close_depth_mult))
    spread_ok = (spread_bps <= D(args.max_close_spread_bps))
    if not depth_ok:
        logging.info(cwarn(f"close-guard depth not ok: askDepth={sum_ask:.2f} bidDepth={sum_bid:.2f} need>={qty*D(args.min_close_depth_mult):.2f}"))
    if not spread_ok:
        logging.info(cwarn(f"close-guard spread not ok: spread={spread_bps:.2f} bps > {args.max_close_spread_bps}"))
    return depth_ok and spread_ok

# ---------- close pair ----------
def close_pair_maker_first(args, qty, s_step,f_step, s_tick,f_tick):
    qty = _quant_floor(qty, min(s_step,f_step))
    if qty<=0: return False
    sbids,sasks = depth(SAPI,args.symbol,2)
    fbids,fasks = depth(FAPI,args.symbol,2)
    if not (sbids and sasks and fbids and fasks): return False
    s_px = s_ask(sasks)  # SELL spot maker
    f_px = f_bid(fbids)  # BUY fut maker (reduceOnly)

    ok = True
    # Spot close
    try:
        spot_limit_gtx(args.symbol,"SELL",qty,s_px,s_step,s_tick)
    except Exception as e:
        logging.warning(cwarn(f"spot maker close fail: {e}"))
        if args.close_mode=="taker":
            try:
                spot_limit_ioc(args.symbol,"SELL",qty,s_px,s_step,s_tick)
            except Exception as e2:
                logging.warning(cwarn(f"spot IOC fail: {e2}"))
                try:
                    spot_market_sell(args.symbol,qty,s_step)
                except Exception as e3:
                    logging.error(cbad(f"spot market fail: {e3}"))
                    ok = False
        else: ok = False

    # Futures close (reduceOnly)
    try:
        fut_limit_gtx(args.symbol,"BUY",qty,f_px,f_step,f_tick,True)
    except Exception as e:
        logging.warning(cwarn(f"fut maker close fail: {e}"))
        if args.close_mode=="taker":
            try:
                fut_limit_ioc(args.symbol,"BUY",qty,f_px,f_step,f_tick,True)
            except Exception as e2:
                logging.warning(cwarn(f"fut IOC fail: {e2}"))
                try:
                    fut_market_close(args.symbol,qty,"BUY",f_step)
                except Exception as e3:
                    logging.error(cbad(f"fut market fail: {e3}"))
                    ok = False
        else: ok = False
    return ok

# ---------- main loop ----------
def run(args):
    logging.info(cinfo(f"Start Maker v15 | {args.symbol} | asset={args.asset}"))
    if not API_KEY or not API_SECRET:
        raise RuntimeError("ต้องตั้ง ENV ASTERDEX_API_KEY / ASTERDEX_API_SECRET ก่อน")

    set_isolated_and_leverage(args.symbol, args.isolated, args.leverage)

    s_info = exinfo_spot(args.symbol)
    f_info = exinfo_fut(args.symbol)
    s_tick,s_step,s_minq,s_min_notional = get_tick_lot_notional(s_info)
    f_tick,f_step,f_minq,f_min_notional = get_tick_lot_notional(f_info)

    logging.info(cinfo(f"Spot filters  : tick={s_tick} step={s_step} minQty={s_minq} minNotional={s_min_notional}"))
    logging.info(cinfo(f"Futures filter: tick={f_tick} step={f_step} minQty={f_minq} minNotional={f_min_notional}"))

    confirm_hits = 0
    state = None

    while True:
        try:
            qty, s_px_open, f_px_open, open_basis = attach_or_open_pair(
                args, s_step,f_step, s_tick,f_tick, s_min_notional,f_min_notional
            )
            if qty<=0:
                logging.info("Waiting for pair to be ready…")
                time.sleep(args.poll); continue

            if state is None:
                state = {"qty": qty, "open_basis": open_basis, "t0": time.time()}

            s_last = spot_last(args.symbol)
            f_mark = mark_price(args.symbol)

            net_if_open  = state["open_basis"]
            net_if_close = est_close_gain_minus_fee_slip(qty, s_last, f_mark,
                                                         args.taker_spot_bps, args.taker_fut_bps, args.slippage_bps)
            net_total = net_if_open + net_if_close

            line = (f"[{now_utc_str()}] Net≈{net_total:.4f} "
                    f"(open≈{net_if_open:.4f} + close≈{net_if_close:.4f}) | "
                    f"S={_fmt(s_last,s_tick)} F={_fmt(f_mark,f_tick)} | qty={_fmt(qty,min(s_step,f_step))} "
                    f"| target={D(args.target_profit):.4f}")
            print((cgood(line) if net_total>=0 else cbad(line)))

            # close guards & confirm
            if net_total >= D(args.target_profit) and guards_ok_for_close(args, qty, s_step,f_step, s_tick,f_tick):
                confirm_hits += 1
            else:
                confirm_hits = 0

            hold_ok = (time.time() - state["t0"]) <= args.max_hold_sec if args.max_hold_sec>0 else True

            if (confirm_hits >= args.confirm_hits) or (not hold_ok):
                logging.info(cinfo("Hit target (confirmed) or max-hold → closing…"))
                ok = close_pair_maker_first(args, qty, s_step,f_step, s_tick,f_tick)
                if ok:
                    logging.info(cgood("Closed. Reopen…"))
                    state = None
                    if not args.always_reopen:
                        time.sleep(args.cooldown_sec)
                else:
                    logging.warning(cwarn("Close failed (partial?) → reset & cooldown"))
                    state = None
                    time.sleep(args.cooldown_sec)
            else:
                time.sleep(args.poll)

        except KeyboardInterrupt:
            logging.warning(cwarn("User stop"))
            break
        except Exception as e:
            logging.error(cbad(f"Loop error: {e}"))
            state = None
            time.sleep(args.cooldown_sec)
            continue

# ---------- argparse ----------
def parse_args():
    ap = argparse.ArgumentParser(description="AsterDex Maker v15 (roll with net profit)")
    ap.add_argument("--symbol", default="ASTERUSDT")
    ap.add_argument("--asset",  default="ASTER")
    ap.add_argument("--capital", type=float, default=300.0, help="USDT ต่อรอบ")
    ap.add_argument("--whole-qty", action="store_true", help="ปัดจำนวนให้เป็นเลขจำนวนเต็ม (เช่น ไม่เอาเศษทศนิยม)")

    # maker/taker fees & slippage bps
    ap.add_argument("--maker-spot-bps", type=float, default=0.0)
    ap.add_argument("--maker-fut-bps",  type=float, default=0.0)
    ap.add_argument("--taker-spot-bps", type=float, default=5.0)
    ap.add_argument("--taker-fut-bps",  type=float, default=5.0)
    ap.add_argument("--slippage-bps",   type=float, default=3.0)

    # open behavior
    ap.add_argument("--depth-limit", type=int, default=5)
    ap.add_argument("--nth", type=int, default=1, help="เลือกระดับ Nth ของ bid/ask สำหรับ maker")

    # rolling / close behavior
    ap.add_argument("--target-profit", type=float, default=1.0)
    ap.add_argument("--confirm-hits", type=int, default=2, help="ต้องเห็นกำไรถึงเป้าต่อเนื่องกี่ครั้งก่อนปิด")
    ap.add_argument("--max-hold-sec", type=int, default=3600)
    ap.add_argument("--max-close-spread-bps", type=float, default=20.0,
                    help="สเปรด (Spot ask - Fut bid)/mid เป็น bps สูงสุดที่ยอมให้ปิด")
    ap.add_argument("--min-close-depth-mult", type=float, default=1.2,
                    help="เด็ปธ์รวมระดับ 2 ชั้น ≥ qty*mult ทั้งสองฝั่ง ก่อนปิด")
    ap.add_argument("--close-mode", choices=["maker","taker"], default="taker",
                    help="ปิดแบบ maker-first แล้ว fallback เป็น taker หรือไม่ (แนะนำ taker)")

    # risk / margin
    ap.add_argument("--isolated", action="store_true")
    ap.add_argument("--leverage", type=int, default=4)

    # loop control
    ap.add_argument("--poll", type=int, default=3)
    ap.add_argument("--cooldown-sec", type=int, default=2)
    ap.add_argument("--always-reopen", action="store_true",
                    help="ปิดแล้วเปิดใหม่ทันทีโดยไม่รอ cooldown")

    ap.add_argument("--log-level", default="INFO", choices=["DEBUG","INFO","WARNING","ERROR"])
    return ap.parse_args()

if __name__=="__main__":
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s | %(levelname)s | %(message)s")
    run(args)

