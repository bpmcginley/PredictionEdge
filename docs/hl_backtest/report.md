# EVENT-CONVEX backtest report

Generated 2026-09-03 12:49 UTC by .github/workflows/hl_backtest.yml.
Rules and caveats: docs/HYPERLIQUID_CONVEX.md section 9. Dates in scripts/hl_events.csv are unverified unless verify=1.

```
Loaded 0/78 event windows from binance (BTC).

Events: 0   tickets taken: 0   (impulse filter 0.40%..1.50%, 20x, sl 1.5%, trail 1.0% after 1.5%, hold 60m)
No ticket passed the impulse filter.

Impulse-filter grid (mean stake return / n). Pick nothing from this table without a holdout; it is here to show how fragile the number is.
  min\max           1.0%          1.5%          2.5%
    0.2%           -/0           -/0           -/0  
    0.3%           -/0           -/0           -/0  
    0.4%           -/0           -/0           -/0  
    0.6%           -/0           -/0           -/0  
```

## Hyperliquid perp candles

```
Loaded 78/78 event windows from hyperliquid (BTC).
  NFP 2024-01-05     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-01-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-01-31    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-02-02     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-02-13     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-03-08     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-03-12     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-03-20    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-04-05     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-04-10     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-05-01    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-05-03     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-05-15     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-06-07     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-06-12     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-06-12    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-07-05     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-07-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-07-31    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-08-02     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-08-14     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-09-06     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-09-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-09-18    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-10-04     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-10-10     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-11-01     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-11-07    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-11-13     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2024-12-06     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2024-12-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2024-12-18    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-01-10     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-01-15     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-01-29    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-02-07     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-02-12     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-03-07     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-03-12     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-03-19    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-04-04     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-04-10     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-05-02     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-05-07    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-05-13     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-06-06     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-06-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-06-18    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-07-03     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-07-15     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-07-30    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-08-01     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-08-12     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-09-05     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-09-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-09-17    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-10-24     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-10-29    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-11-20     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2025-12-10    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2025-12-16     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2025-12-18     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2026-01-09     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2026-01-13     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2026-01-28    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2026-02-06     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2026-02-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2026-03-06     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2026-03-11     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2026-03-18    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2026-04-03     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2026-04-10     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2026-04-29    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2026-05-08     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2026-05-12     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  NFP 2026-06-05     NFP   skipped (no-candle, impulse +0.00%, activity x0.0)
  CPI 2026-06-10     CPI   skipped (no-candle, impulse +0.00%, activity x0.0)
  FOMC 2026-06-17    FOMC  skipped (no-candle, impulse +0.00%, activity x0.0)

Events: 78   tickets taken: 0   (impulse filter 0.40%..1.50%, 20x, sl 1.5%, trail 1.0% after 1.5%, hold 60m)
No ticket passed the impulse filter.
```
