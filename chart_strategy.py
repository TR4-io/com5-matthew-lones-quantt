"""Directional RIT COM5 strategy for the supplied repeat-path CL charts."""
import csv
import time
from datetime import datetime
from pathlib import Path

from chart_execution import Client, TradingHalt, risk_allows
from com5_template import API_KEY, BASE, HEADERS

# Period-local ticks, visually estimated from ten-tick candles.
TURNS = {1: [(0,-1),(120,1),(250,-1),(380,1),(530,-1),(570,1),(588,0)],
         2: [(0,1),(120,-1),(280,1),(310,-1),(460,1),(585,0)]}
TICKERS = ('CL', 'CL-1F', 'CL-2F', 'HO', 'RB', 'CL-AK', 'CL-NYC')
TANKS = {'CL':'CL-STORAGE', 'CL-AK':'AK-STORAGE', 'CL-NYC':'NYC-STORAGE'}


def direction(period, tick):
    return next(d for start, d in reversed(TURNS[period]) if start <= tick)


def next_order(rows, limits, period, side):
    eligible = [r for r in rows if r['ticker'] in TICKERS and r['is_tradeable']
                and not (period == 2 and r['ticker'] == 'CL-1F')]
    closing = [r for r in eligible if r['position'] and side * r['position'] <= 0]
    candidates = closing or sorted(eligible, key=lambda r: (abs(r['position']), TICKERS.index(r['ticker'])))
    for row in candidates:
        ticker, held = row['ticker'], row['position']
        action = ('SELL' if held > 0 else 'BUY') if closing else ('BUY' if side > 0 else 'SELL')
        if not closing and (side == 0 or (side < 0 and not row['is_shortable'])):
            continue
        if (row.get('ask' if action == 'BUY' else 'bid') or 0) <= 0:
            continue
        # Ten-contract entry clips distribute shared capacity across available names.
        maximum = min(row['max_trade_size'], abs(held) if closing else 10)
        for qty in range(int(maximum), 0, -1):
            if risk_allows(rows, limits, [], ticker, action, qty):
                return ticker, action, qty
    return None


def trade(api, rows, order, owned):
    ticker, action, qty = order
    before = next(r['position'] for r in rows if r['ticker'] == ticker)
    if ticker in TANKS and action == 'BUY':
        tank = TANKS[ticker]
        leases = api('GET','/leases')
        asset = next(a for a in api('GET','/assets') if a['ticker'] == tank)
        capacity = asset['containment']['quantity']
        if capacity <= 0:
            raise TradingHalt(f'Missing storage capacity for {tank}')
        available = capacity * sum(l['ticker'] == tank for l in leases)
        if available < before + qty:
            if not asset['is_available'] or asset['available_quantity'] < 1:
                raise TradingHalt(f'No storage available for {ticker}')
            lease = api('POST','/leases', {'ticker':tank})
            owned.add(lease['id'])
            for _ in range(20):
                if any(l['id'] == lease['id'] and l['ticker'] == tank for l in api('GET','/leases')):
                    break
                time.sleep(.1)
            else:
                raise TradingHalt(f'Unconfirmed {tank} lease {lease}')
            return 0  # Refresh time, limits and lease inventory before ordering.
    result = api('POST','/orders', dict(ticker=ticker, action=action, quantity=qty, type='MARKET'))
    oid = result['order_id']
    for i in range(30):
        if result.get('status') in ('TRANSACTED','CANCELLED'):
            break
        if i == 15:
            api('DELETE', f'/orders/{oid}')
        time.sleep(.1)
        result = api('GET', f'/orders/{oid}')
    else:
        raise TradingHalt(f'Unresolved order {oid}')
    filled = result['quantity_filled']
    if not 0 <= filled <= qty:
        raise TradingHalt(f'Invalid fill for {oid}')
    expected = before + (filled if action == 'BUY' else -filled)
    for _ in range(30):
        positions = api('GET','/securities')
        if next(r['position'] for r in positions if r['ticker'] == ticker) == expected:
            return filled
        time.sleep(.1)
    raise TradingHalt(f'Inventory does not confirm fill of order {oid}')


def main():
    if not API_KEY.strip():
        raise SystemExit('Set your RIT_API_KEY or API_KEY in com5_template.py.')
    api = Client(BASE, HEADERS, live=True).request
    owned = set()
    started = False
    last = None
    path = Path('runs') / datetime.now().strftime('chart_%Y%m%d_%H%M%S_%f')
    path.mkdir(parents=True)
    print(f'Chart strategy live. Waiting for ACTIVE. Log: {path}')
    with (path/'chart.csv').open('w',newline='') as stream:
        log = csv.writer(stream)
        log.writerow(['period','tick','direction','nlv_before_order','order','filled'])
        try:
            while True:
                case = api('GET','/case')
                if case['status'] != 'ACTIVE':
                    if started and last and last[0] == 2 and case['status'] == 'STOPPED':
                        break
                    time.sleep(.5)
                    continue
                if (case['ticks_per_period'], case['total_periods']) != (600,2):
                    raise TradingHalt('Chart requires two periods of 600 ticks')
                period, tick = case['period'], case['tick']
                side = direction(period,tick)
                if api('GET','/orders', {'status':'OPEN'}):
                    raise TradingHalt('Open orders require review before proceeding')
                rows = api('GET','/securities')
                leases = api('GET','/leases')
                if not started and (leases or any(r['position'] for r in rows)):
                    raise TradingHalt('Start on a clean practice account')
                started = True
                if last != (period,side):
                    print(f'P{period} tick {tick}: target direction {side:+d}')
                last = period,side
                # An empty tank can be released as soon as its crude is sold.
                for lease in leases:
                    if side <= 0 and lease['id'] in owned and lease.get('containment_usage') == 0:
                        api('DELETE', f"/leases/{lease['id']}")
                        owned.remove(lease['id'])
                order = next_order(rows, api('GET','/limits'), period, side)
                nlv = api('GET','/trader').get('nlv')
                filled = trade(api, rows, order, owned) if order else 0
                log.writerow([period,tick,side,nlv,order,filled])
                stream.flush()
                if period == 2 and side == 0 and not order and not any(r['position'] for r in rows) and not owned:
                    break
                time.sleep(.1 if order else .4)
        except (TradingHalt, KeyboardInterrupt, Exception) as exc:
            print(f'Stopped: {exc}. Check RIT positions, orders and leases.')
            raise
        finally:
            final_rows = api('GET','/securities')
            print('Final positions:', {r['ticker']:r['position'] for r in final_rows if r['position']})
            print('Remaining leases:', [l['id'] for l in api('GET','/leases')])
            print('Final NLV:', api('GET','/trader').get('nlv'))
