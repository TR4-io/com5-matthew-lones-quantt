"""Portfolio limit checks used by the original strategy engines."""
import math


class TradingHalt(BaseException):
    """Stop entries on uncertain writes; bypass strategy-level except Exception."""


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

