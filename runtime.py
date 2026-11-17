"""Bounded REST transport and portfolio-wide risk checks. No strategy signals."""
import math
import time
import requests


class TradingHalt(BaseException):
    """Stop all engines; cannot be swallowed by an upstream except Exception."""


class Client:
    def __init__(self, base, headers, live=False, session=None):
        self.base = base.rstrip('/')
        self.session = session or requests.Session()
        self.session.headers.update(headers)
        self.live = live
        self.uncertain_write = False

    def request(self, method, endpoint, params=None):
        write = method != 'GET'
        if write and not self.live:
            raise TradingHalt('Read-only mode blocks order and asset writes')
        for attempt in range(4):
            try:
                response = self.session.request(method, self.base + endpoint,
                                                params=params, timeout=(3, 5))
            except KeyboardInterrupt:
                if write:
                    self.uncertain_write = True
                raise
            except requests.RequestException as exc:
                if write:
                    self.uncertain_write = True
                    raise TradingHalt(f'Uncertain {method} {endpoint}; NOT retried') from exc
                raise
            if response.status_code == 429:
                if attempt == 3:
                    raise TradingHalt('Rate limit persists; stopping entries')
                try:
                    wait = float(response.headers.get('Retry-After', response.json().get('wait', .5)))
                except (ValueError, TypeError):
                    wait = .5
                time.sleep(min(max(wait, .05), 5))
                continue
            if write and response.status_code >= 500:
                self.uncertain_write = True
                raise TradingHalt(f'Uncertain server result for {method} {endpoint}; NOT retried')
            if not response.ok:
                if write:
                    # Preserve RIT's rejection reason; never retry a rejected conversion.
                    detail = ' '.join(response.text.split())[:1000]
                    raise TradingHalt(f'{method} {endpoint} rejected: HTTP {response.status_code}; server: {detail or "(empty response)"}')
                response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {'success': True}
            try:
                return response.json()
            except ValueError as exc:
                if write:
                    self.uncertain_write = True
                    raise TradingHalt(f'Invalid write response for {endpoint}; NOT retried') from exc
                raise


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Nonfinite server value')
    return value


def risk_allows(securities, limits, orders, ticker, action, quantity):
    """Worst-case open-order fills, using every server limit group and weight."""
    rows = {r['ticker']: r for r in securities}
    if ticker not in rows or action not in ('BUY', 'SELL') or quantity <= 0:
        return False
    if not rows[ticker].get('is_tradeable', False):
        return False
    positions = {t: finite(r['position']) for t, r in rows.items()}
    buys = dict.fromkeys(rows, 0.)
    sells = dict.fromkeys(rows, 0.)
    for order in orders:
        t = order['ticker']
        if t not in rows or order['action'] not in ('BUY', 'SELL'):
            raise ValueError('Unknown open order')
        remaining = max(0, finite(order['quantity']) - finite(order['quantity_filled']))
        (buys if order['action'] == 'BUY' else sells)[t] += remaining
    before_buys, before_sells = dict(buys), dict(sells)
    (buys if action == 'BUY' else sells)[ticker] += quantity
    if not rows[ticker].get('is_shortable', False) and positions[ticker] - sells[ticker] < 0:
        return False
    groups = {r['name']: r for r in limits}
    if not groups:
        raise ValueError('No live limit groups')
    weights = {name: {} for name in groups}
    for t, row in rows.items():
        if not row.get('limits'):
            raise ValueError(f'Missing limit weights for {t}')
        for member in row['limits']:
            name, weight = member['name'], finite(member['units'])
            if name not in groups or weight <= 0 or t in weights[name]:
                raise ValueError('Invalid limit membership')
            weights[name][t] = weight
    delta = quantity if action == "BUY" else -quantity
    position = positions[ticker]
    reduces_position = abs(position + delta) < abs(position) and position * (position + delta) >= 0
    for name, group in groups.items():
        w = weights[name]
        def exposure(b, s):
            gross = sum(max(abs(positions[t] + b[t]), abs(positions[t] - s[t])) * v for t, v in w.items())
            net = max(abs(sum((positions[t] + b[t]) * v for t, v in w.items())),
                      abs(sum((positions[t] - s[t]) * v for t, v in w.items())))
            return gross, net
        old = exposure(before_buys, before_sells)
        new = exposure(buys, sells)
        caps = [finite(group['gross_limit']), finite(group['net_limit'])]
        for previous, projected, cap in zip(old, new, caps):
            if cap <= 0:
                raise ValueError('Invalid live limit')
            # Permit an exit from an already-breached account only if it improves it.
            if projected > cap and (projected > previous or not reduces_position):
                return False
    return True


def conversion_spec(asset, source, outputs):
    """Verify the supplied COM5 contract-unit conversion, never infer units."""
    expected_input = 30 if asset['ticker'] == 'CL-REFINERY' else 10
    incoming = {r['ticker']: finite(r['quantity']) for r in asset.get('convert_from', [])}
    outgoing = {r['ticker']: finite(r['quantity']) for r in asset.get('convert_to', [])}
    if incoming != {source: expected_input} or outgoing != outputs:
        raise ValueError(f"Unsupported conversion units/batch for {asset['ticker']}: {incoming} -> {outgoing}")
    if finite(asset.get('ticks_per_conversion', 0)) <= 0:
        raise ValueError('Missing conversion timing')
    return {'from1': source, 'quantity1': expected_input}
