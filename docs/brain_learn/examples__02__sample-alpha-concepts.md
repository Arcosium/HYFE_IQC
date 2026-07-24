# ⭐ Alpha Examples for Bronze Users 🥉

- 튜토리얼: `examples` (Getting Started)
- 페이지 ID: `sample-alpha-concepts`
- 최종수정: 2026-03-11T07:08:03.328919-04:00
- 분량: PT2M

---

> [HEADING] {"level": "1", "content": "Valuation based on cash flow"}

**Hypothesis**

A lower EV/CF usually suggests the company is becoming cheaper relative to its cash-generating ability; a higher multiple suggests it’s getting more expensive.

**Implementation**

Use ts_zscore to standardize the chang of the ratio and group_rank to control the turnover.

**Hint to Improve Alpha**

There are various types of cash flow, and switching the type used in the metric may improve its performance.

> [SIMULATION_EXAMPLE] {"settings": {"instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000", "delay": 1, "decay": 0, "neutralization": "INDUSTRY", "truncation": 0.08, "pasteurization": "ON", "unitHandling": "VERIFY", "nanHandling": "OFF", "language": "FASTEXPR", "maxTrade": "OFF", "maxPosition": "OFF"}, "type": "REGULAR", "regular": "group_rank(-ts_zscore(enterprise_value/cashflow, 63),industry)"}

> [HEADING] {"level": "1", "content": "Overpriced stocks"}

**Hypothesis**

When analyst price target estimates (est_ptp) and free cashflow estimates (est_fcf) move highly in sync over the past month (high positive correlation), it may signal that the market has already fully priced in the cash flow expectations into price targets — leaving little room for further upside.

**Implementation**

Using est_ptp to capture price estimate and est_fcf to capture free cash flow and calculate the dynamics between them with ts_corr.

**Hint to Improve Alpha**

The window of 1 year might be too long to react on the price correction. Try shorter window.

> [SIMULATION_EXAMPLE] {"settings": {"instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000", "delay": 1, "decay": 0, "neutralization": "MARKET", "truncation": 0.08, "pasteurization": "ON", "unitHandling": "VERIFY", "nanHandling": "OFF", "language": "FASTEXPR", "maxTrade": "OFF", "maxPosition": "OFF"}, "type": "REGULAR", "regular": "-ts_corr(est_ptp,est_fcf,252)"}

> [HEADING] {"level": "1", "content": "Volatility arbitrage"}

**Hypothesis**

Higher volatility is often observed during bearish markets, while lower volatility is typically seen during bullish markets. A lower Parkinson's volatility coupled with a higher implied volatility may suggest that there could be a stronger bullish sentiment for the stock in the future.

**Implementation**

Long the stock if its implied volatility significantly exceeds its historical volatility and short the opposite

**Hint to Improve Alpha**

Can you use ts_backfill to avoid missing data on certain days?

> [SIMULATION_EXAMPLE] {"settings": {"instrumentType": "EQUITY", "region": "USA", "universe": "TOP200", "delay": 1, "decay": 0, "neutralization": "SECTOR", "truncation": 0.08, "pasteurization": "ON", "unitHandling": "VERIFY", "nanHandling": "OFF", "language": "FASTEXPR", "maxTrade": "OFF", "maxPosition": "OFF"}, "type": "REGULAR", "regular": "implied_volatility_call_120/parkinson_volatility_120"}
