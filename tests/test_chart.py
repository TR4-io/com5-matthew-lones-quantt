import unittest
from unittest.mock import patch
from chart_strategy import next_order, direction, TICKERS, TANKS, trade, TURNS

class ChartReplay(unittest.TestCase):
    def test_both_periods_full_turns(self):
        rows = [dict(ticker=t,position=0,bid=99,ask=100,is_tradeable=True,
                     is_shortable=t.endswith('F'),max_trade_size=30,
                     limits=[dict(name='P' if t in ('RB','HO') else 'C',units=1)]) for t in TICKERS]
        limits = [dict(name=n,net_limit=100,gross_limit=500) for n in ('P','C')]
        leases,owned=[],set()
        def api(method,endpoint,params=None):
            if endpoint=='/leases':
                if method=='POST':
                    l=dict(id=len(leases)+1,ticker=params['ticker'])
                    leases.append(l)
                    return l
                return leases
            if endpoint=='/assets':
                return [dict(ticker=t, containment={'quantity':10},is_available=True,available_quantity=100) for t in TANKS.values()]
            if endpoint=='/securities': return rows
            if endpoint=='/orders':
                r=next(r for r in rows if r['ticker']==params['ticker'])
                r['position']+=params['quantity']*(1 if params['action']=='BUY' else -1)
                return dict(order_id=13,status='TRANSACTED',quantity_filled=params['quantity'])
            self.fail(endpoint)
        for period,turns in TURNS.items():
            for tick,side in turns:
                self.assertEqual(direction(period,tick),side)
                for _ in range(100):
                    order=next_order(rows,limits,period,side)
                    if not order: break
                    trade(api,rows,order,owned)
                    for group in ('P','C'):
                        self.assertLessEqual(abs(sum(r['position'] for r in rows if r['limits'][0]['name']==group)),100)
                else: self.fail('target never reached')
                for group,target in [('C',side*100),('P',max(0,side)*100)]:
                    self.assertEqual(sum(r['position'] for r in rows if r['limits'][0]['name']==group),target)
                if period==2: self.assertEqual(rows[1]['position'],0)
