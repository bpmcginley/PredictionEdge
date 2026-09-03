# EVENT-CONVEX backtest report

Generated 2026-09-03 13:20 UTC by .github/workflows/hl_backtest.yml.
Rules and caveats: docs/HYPERLIQUID_CONVEX.md section 9. Dates in scripts/hl_events.csv are unverified unless verify=1.

```
Loaded 78/78 event windows (BTC); sources: {'coinbase': 78}
Data quality: 7254 candles, 0.0% flat (high == low)
  NFP 2024-01-05     NFP   skipped (no-impulse, impulse -0.12%, activity x4.3)
  CPI 2024-01-11     CPI   skipped (no-impulse, impulse -0.14%, activity x5.8)
  FOMC 2024-01-31    FOMC  skipped (no-impulse, impulse -0.18%, activity x4.3)
  NFP 2024-02-02     NFP   dir -1 impulse -0.46% time   60m  price +0.21%  stake +0.02  activity x14.2
  CPI 2024-02-13     CPI   skipped (no-impulse, impulse -0.32%, activity x8.3)
  NFP 2024-03-08     NFP   skipped (no-impulse, impulse +0.24%, activity x5.1)
  CPI 2024-03-12     CPI   skipped (no-impulse, impulse +0.34%, activity x6.8)
  FOMC 2024-03-20    FOMC  dir +1 impulse +0.54% time   60m  price +1.53%  stake +0.29  activity x3.7
  NFP 2024-04-05     NFP   dir -1 impulse -0.49% time   60m  price -0.85%  stake -0.19  activity x5.1
  CPI 2024-04-10     CPI   dir -1 impulse -0.70% time   60m  price +1.31%  stake +0.24  activity x8.1
  FOMC 2024-05-01    FOMC  skipped (no-impulse, impulse +0.20%, activity x3.2)
  NFP 2024-05-03     NFP   dir +1 impulse +1.06% time   60m  price +2.62%  stake +0.51  activity x15.8
  CPI 2024-05-15     CPI   dir +1 impulse +1.47% time   60m  price +0.93%  stake +0.17  activity x22.1
  NFP 2024-06-07     NFP   dir -1 impulse -0.86% time   60m  price -0.08%  stake -0.03  activity x14.4
  CPI 2024-06-12     CPI   skipped (no-impulse, impulse +1.69%, activity x36.8)
  FOMC 2024-06-12    FOMC  dir -1 impulse -0.76% time   60m  price +0.50%  stake +0.08  activity x21.5
  NFP 2024-07-05     NFP   skipped (no-impulse, impulse -0.03%, activity x13.5)
  CPI 2024-07-11     CPI   dir +1 impulse +1.07% stop   40m  price -1.52%  stake -0.32  activity x13.7
  FOMC 2024-07-31    FOMC  skipped (no-impulse, impulse -0.35%, activity x4.4)
  NFP 2024-08-02     NFP   skipped (no-impulse, impulse -0.04%, activity x14.2)
  CPI 2024-08-14     CPI   dir -1 impulse -0.77% time   60m  price +0.53%  stake +0.09  activity x13.9
  NFP 2024-09-06     NFP   dir +1 impulse +1.10% time   60m  price +0.56%  stake +0.09  activity x23.1
  CPI 2024-09-11     CPI   dir -1 impulse -0.50% time   60m  price -0.32%  stake -0.08  activity x9.1
  FOMC 2024-09-18    FOMC  dir +1 impulse +1.04% time   60m  price -0.12%  stake -0.04  activity x15.0
  NFP 2024-10-04     NFP   skipped (no-impulse, impulse +0.16%, activity x8.3)
  CPI 2024-10-10     CPI   skipped (no-impulse, impulse -0.38%, activity x10.5)
  NFP 2024-11-01     NFP   skipped (no-impulse, impulse -0.24%, activity x6.1)
  FOMC 2024-11-07    FOMC  skipped (no-impulse, impulse -0.32%, activity x2.8)
  CPI 2024-11-13     CPI   dir +1 impulse +0.46% time   60m  price +1.21%  stake +0.22  activity x6.4
  NFP 2024-12-06     NFP   skipped (no-impulse, impulse +0.21%, activity x4.3)
  CPI 2024-12-11     CPI   skipped (no-impulse, impulse +0.32%, activity x7.7)
  FOMC 2024-12-18    FOMC  skipped (no-impulse, impulse -0.22%, activity x2.9)
  NFP 2025-01-10     NFP   skipped (no-impulse, impulse -1.57%, activity x27.8)
  CPI 2025-01-15     CPI   dir +1 impulse +0.93% time   60m  price +0.52%  stake +0.09  activity x18.4
  FOMC 2025-01-29    FOMC  skipped (no-impulse, impulse -0.26%, activity x4.2)
  NFP 2025-02-07     NFP   skipped (no-impulse, impulse -0.21%, activity x10.7)
  CPI 2025-02-12     CPI   dir -1 impulse -1.25% time   60m  price +0.74%  stake +0.13  activity x19.9
  NFP 2025-03-07     NFP   dir +1 impulse +0.75% time   60m  price -1.14%  stake -0.25  activity x8.3
  CPI 2025-03-12     CPI   dir +1 impulse +0.93% time   60m  price -0.95%  stake -0.21  activity x12.5
  FOMC 2025-03-19    FOMC  skipped (no-impulse, impulse +0.38%, activity x10.0)
  NFP 2025-04-04     NFP   skipped (no-impulse, impulse +0.17%, activity x6.7)
  CPI 2025-04-10     CPI   dir +1 impulse +0.44% time   60m  price -0.67%  stake -0.15  activity x4.7
  NFP 2025-05-02     NFP   skipped (no-impulse, impulse +0.31%, activity x7.4)
  FOMC 2025-05-07    FOMC  skipped (no-impulse, impulse +0.12%, activity x8.2)
  CPI 2025-05-13     CPI   skipped (no-impulse, impulse +0.24%, activity x9.4)
  NFP 2025-06-06     NFP   skipped (no-impulse, impulse +0.33%, activity x10.4)
  CPI 2025-06-11     CPI   dir +1 impulse +0.42% time   60m  price -0.06%  stake -0.03  activity x8.6
  FOMC 2025-06-18    FOMC  skipped (no-impulse, impulse +0.23%, activity x4.1)
  NFP 2025-07-03     NFP   skipped (no-impulse, impulse -0.12%, activity x14.6)
  CPI 2025-07-15     CPI   skipped (no-impulse, impulse +0.31%, activity x6.2)
  FOMC 2025-07-30    FOMC  skipped (no-impulse, impulse +0.10%, activity x5.4)
  NFP 2025-08-01     NFP   skipped (no-impulse, impulse +0.11%, activity x6.1)
  CPI 2025-08-12     CPI   dir +1 impulse +0.49% time   60m  price +0.08%  stake -0.00  activity x13.8
  NFP 2025-09-05     NFP   dir +1 impulse +0.42% time   60m  price -0.19%  stake -0.06  activity x11.6
  CPI 2025-09-11     CPI   dir -1 impulse -0.45% time   60m  price -0.02%  stake -0.02  activity x13.4
  FOMC 2025-09-17    FOMC  dir +1 impulse +0.45% time   60m  price -0.80%  stake -0.18  activity x10.1
  CPI 2025-10-24     CPI   dir +1 impulse +0.67% time   60m  price -0.48%  stake -0.11  activity x22.5
  FOMC 2025-10-29    FOMC  skipped (no-impulse, impulse +0.02%, activity x5.5)
  NFP 2025-11-20     NFP   skipped (no-impulse, impulse +0.30%, activity x5.8)
  FOMC 2025-12-10    FOMC  dir +1 impulse +0.48% time   60m  price -0.15%  stake -0.05  activity x6.3
  NFP 2025-12-16     NFP   skipped (no-impulse, impulse +0.01%, activity x9.4)
  CPI 2025-12-18     CPI   dir +1 impulse +0.41% trail  41m  price +0.64%  stake +0.11  activity x9.3
  NFP 2026-01-09     NFP   skipped (no-impulse, impulse -0.02%, activity x9.0)
  CPI 2026-01-13     CPI   skipped (no-impulse, impulse +0.14%, activity x12.4)
  FOMC 2026-01-28    FOMC  skipped (no-impulse, impulse +0.10%, activity x3.6)
  NFP 2026-02-06     NFP   skipped (no-impulse, impulse +0.04%, activity x1.3)
  CPI 2026-02-11     CPI   skipped (no-impulse, impulse -0.17%, activity x6.0)
  NFP 2026-03-06     NFP   skipped (no-impulse, impulse -0.18%, activity x5.5)
  CPI 2026-03-11     CPI   skipped (no-impulse, impulse +0.02%, activity x4.9)
  FOMC 2026-03-18    FOMC  skipped (no-impulse, impulse +0.27%, activity x5.1)
  NFP 2026-04-03     NFP   skipped (no-impulse, impulse -0.15%, activity x14.6)
  CPI 2026-04-10     CPI   skipped (no-impulse, impulse +0.24%, activity x11.6)
  FOMC 2026-04-29    FOMC  skipped (no-impulse, impulse -0.10%, activity x4.8)
  NFP 2026-05-08     NFP   skipped (no-impulse, impulse -0.16%, activity x10.0)
  CPI 2026-05-12     CPI   skipped (no-impulse, impulse -0.18%, activity x9.7)
  NFP 2026-06-05     NFP   skipped (no-impulse, impulse -0.25%, activity x3.3)
  CPI 2026-06-10     CPI   dir +1 impulse +0.57% time   60m  price +0.34%  stake +0.05  activity x9.7
  FOMC 2026-06-17    FOMC  dir -1 impulse -0.98% time   60m  price -0.29%  stake -0.08  activity x14.2
  DATE CHECK: 1 release minutes were no busier than the prior half hour (activity < 2x); their dates/times may be wrong: NFP 2026-02-06

Events: 78   tickets taken: 29   (impulse filter 0.40%..1.50%, 20x, sl 1.5%, trail 1.0% after 1.5%, hold 60m)
Exit mix: {'trail': 1, 'time': 27, 'stop': 1, 'liq': 0}
Hit rate 44.8%   avg win +0.16x   avg loss 0.11x   Kelly +0.06
EV per ticket on stake: +0.010   median -0.021   90% bootstrap CI [-0.042, +0.064]
Random-direction baseline (same windows, coin-flip direction): -0.027
At 1.5% of bankroll per ticket: +0.015% of bankroll per ticket; over 40 tickets the 5th-pct outcome is -2.1% and the 95th-pct max drawdown is 2.9%
Verdict: edge not distinguishable from zero; n=29 tickets (too few to conclude; keep collecting)

Impulse-filter grid (mean stake return / n). Pick nothing from this table without a holdout; it is here to show how fragile the number is.
  min\max           1.0%          1.5%          2.5%
    0.2%      -0.005/46     +0.006/52     +0.001/54 
    0.3%      +0.002/33     +0.015/39     +0.009/41 
    0.4%      -0.010/23     +0.010/29     +0.002/31 
    0.6%      -0.020/9      +0.024/15     +0.007/17 
```

## Unscheduled impulse scan, last 365 days, default filter

```
SCAN: 524769 minutes from coinbase over the last 365 days; 0.0% flat candles
388 impulse triggers (|1-min move| in 0.40%..1.50%, cooldown 60m)
  2025-09-05 12:30   IMP   dir +1 impulse +0.42% time   60m  price -0.19%  stake -0.06  activity x11.6
  2025-09-05 14:18   IMP   dir -1 impulse -0.40% time   60m  price +0.51%  stake +0.08  activity x3.9
  2025-09-05 21:16   IMP   dir -1 impulse -0.68% time   60m  price -0.17%  stake -0.05  activity x17.7
  2025-09-10 12:30   IMP   dir +1 impulse +0.69% time   60m  price +0.12%  stake +0.01  activity x15.7
  2025-09-11 12:30   IMP   dir -1 impulse -0.45% time   60m  price -0.02%  stake -0.02  activity x13.4
  2025-09-17 17:59   IMP   dir -1 impulse -0.41% time   60m  price +0.24%  stake +0.03  activity x8.2
  2025-09-22 05:59   IMP   dir -1 impulse -0.50% trail   7m  price +0.72%  stake +0.13  activity x26.1
  2025-10-03 17:00   IMP   dir -1 impulse -0.55% time   60m  price -0.07%  stake -0.03  activity x5.2
  2025-10-05 04:45   IMP   dir +1 impulse +0.50% time   60m  price +0.07%  stake -0.00  activity x11.4
  2025-10-05 09:30   IMP   dir -1 impulse -0.48% time   60m  price +0.35%  stake +0.05  activity x17.1
  2025-10-10 20:52   IMP   dir -1 impulse -0.49% trail   4m  price +1.48%  stake +0.28  activity x7.8
  2025-10-10 21:56   IMP   dir +1 impulse +0.45% stop    9m  price -1.52%  stake -0.32  activity x0.6
  2025-10-11 20:56   IMP   dir -1 impulse -0.40% time   60m  price -1.20%  stake -0.26  activity x3.0
  2025-10-12 03:09   IMP   dir +1 impulse +0.63% time   60m  price -0.27%  stake -0.07  activity x9.2
  2025-10-12 14:51   IMP   dir +1 impulse +0.67% time   60m  price +0.28%  stake +0.04  activity x9.9
  2025-10-12 16:44   IMP   dir +1 impulse +0.48% time   60m  price +0.07%  stake -0.00  activity x3.6
  2025-10-12 22:00   IMP   dir +1 impulse +0.51% time   60m  price -0.30%  stake -0.08  activity x6.9
  2025-10-13 13:30   IMP   dir +1 impulse +0.45% time   60m  price -0.72%  stake -0.16  activity x5.3
  2025-10-14 19:37   IMP   dir -1 impulse -0.56% time   60m  price -0.06%  stake -0.03  activity x7.7
  2025-10-16 09:34   IMP   dir -1 impulse -0.69% stop   10m  price -1.52%  stake -0.32  activity x13.1
  2025-10-16 14:17   IMP   dir -1 impulse -0.43% time   60m  price -0.52%  stake -0.12  activity x3.8
  2025-10-17 11:13   IMP   dir +1 impulse +0.46% time   60m  price +0.07%  stake -0.00  activity x4.4
  2025-10-19 09:48   IMP   dir +1 impulse +0.94% time   60m  price -0.26%  stake -0.07  activity x16.6
  2025-10-21 21:21   IMP   dir -1 impulse -0.49% time   60m  price +0.12%  stake +0.01  activity x7.8
  2025-10-21 22:31   IMP   dir -1 impulse -1.29% time   60m  price -0.45%  stake -0.11  activity x13.7
  2025-10-22 11:03   IMP   dir -1 impulse -0.55% time   60m  price -0.51%  stake -0.12  activity x20.0
  2025-10-22 13:58   IMP   dir +1 impulse +0.54% time   60m  price -0.30%  stake -0.08  activity x4.2
  2025-10-22 16:30   IMP   dir -1 impulse -0.51% time   60m  price +0.00%  stake -0.02  activity x6.5
  2025-10-22 21:01   IMP   dir -1 impulse -0.59% time   60m  price -0.24%  stake -0.07  activity x12.1
  2025-10-23 06:15   IMP   dir +1 impulse +0.44% time   60m  price +0.18%  stake +0.02  activity x16.0
  2025-10-24 12:30   IMP   dir +1 impulse +0.67% time   60m  price -0.48%  stake -0.11  activity x22.5
  2025-10-26 22:03   IMP   dir +1 impulse +1.20% time   60m  price -0.60%  stake -0.14  activity x21.8
  2025-10-28 21:07   IMP   dir -1 impulse -0.42% time   60m  price -0.70%  stake -0.16  activity x5.6
  2025-10-29 18:35   IMP   dir -1 impulse -1.16% time   60m  price -0.08%  stake -0.03  activity x10.3
  2025-10-30 04:27   IMP   dir -1 impulse -0.41% time   60m  price -0.43%  stake -0.10  activity x3.7
  2025-10-31 16:50   IMP   dir -1 impulse -0.43% time   60m  price -0.30%  stake -0.08  activity x7.1
  2025-11-03 15:26   IMP   dir -1 impulse -0.61% time   60m  price -0.11%  stake -0.04  activity x7.2
  2025-11-04 17:19   IMP   dir -1 impulse -0.43% time   60m  price +0.49%  stake +0.08  activity x3.5
  2025-11-04 18:36   IMP   dir +1 impulse +0.49% time   60m  price +0.25%  stake +0.03  activity x3.2
  2025-11-04 20:19   IMP   dir +1 impulse +0.45% time   60m  price +0.00%  stake -0.02  activity x3.2
  DATE CHECK: 22 release minutes were no busier than the prior half hour (activity < 2x); their dates/times may be wrong: 2025-10-10 21:56, 2025-11-04 21:55, 2025-12-17 16:05, 2025-12-18 17:27, 2026-01-31 17:33, 2026-02-02 04:19, 2026-02-05 15:05, 2026-02-05 17:23, 2026-02-05 19:25, 2026-02-05 21:29, 2026-02-05 22:31, 2026-02-06 00:01, 2026-02-06 01:04, 2026-02-06 02:19, 2026-02-06 15:50, 2026-02-06 17:00, 2026-02-06 18:34, 2026-02-11 14:36, 2026-03-02 15:01, 2026-03-11 14:00, 2026-06-05 07:37, 2026-06-05 20:06

Events: 388   tickets taken: 388   (impulse filter 0.40%..1.50%, 20x, sl 1.5%, trail 1.0% after 1.5%, hold 60m)
Exit mix: {'trail': 32, 'time': 315, 'stop': 41, 'liq': 0}
Hit rate 42.3%   avg win +0.14x   avg loss 0.14x   Kelly -0.14
EV per ticket on stake: -0.020   median -0.033   90% bootstrap CI [-0.035, -0.005]
Random-direction baseline (same windows, coin-flip direction): -0.026
At 1.5% of bankroll per ticket: -0.030% of bankroll per ticket; over 40 tickets the 5th-pct outcome is -3.9% and the 95th-pct max drawdown is 4.3%
Verdict: NEGATIVE at 90%: the ticket loses money; n=388 tickets
```

## Same scan, smaller impulses (0.25%..1.0%)

```
SCAN: 524769 minutes from coinbase over the last 365 days; 0.0% flat candles
1220 impulse triggers (|1-min move| in 0.25%..1.00%, cooldown 60m)

Events: 1220   tickets taken: 1220   (impulse filter 0.25%..1.00%, 20x, sl 1.5%, trail 1.0% after 1.5%, hold 60m)
Exit mix: {'trail': 54, 'time': 1101, 'stop': 65, 'liq': 0}
Hit rate 40.5%   avg win +0.12x   avg loss 0.12x   Kelly -0.19
EV per ticket on stake: -0.023   median -0.031   90% bootstrap CI [-0.030, -0.015]
Random-direction baseline (same windows, coin-flip direction): -0.026
At 1.5% of bankroll per ticket: -0.034% of bankroll per ticket; over 40 tickets the 5th-pct outcome is -3.7% and the 95th-pct max drawdown is 4.0%
Verdict: NEGATIVE at 90%: the ticket loses money; n=1220 tickets
```

## Hyperliquid perp candles

```
Loaded 0/78 event windows (BTC); sources: {}
  failed: NFP 2024-01-05 13:30Z: hyperliquid: empty response
  failed: CPI 2024-01-11 13:30Z: hyperliquid: empty response
  failed: FOMC 2024-01-31 19:00Z: hyperliquid: empty response
  failed: NFP 2024-02-02 13:30Z: hyperliquid: empty response
  failed: CPI 2024-02-13 13:30Z: hyperliquid: empty response
  ... and 73 more failures

Events: 0   tickets taken: 0   (impulse filter 0.40%..1.50%, 20x, sl 1.5%, trail 1.0% after 1.5%, hold 60m)
No ticket passed the impulse filter.
```
