# Understanding PnL Realization Horizon

- 튜토리얼: `interpret-results` (Interpreting Results)
- 페이지 ID: `understanding-pnl-realization-horizon`
- 최종수정: 2026-04-08T05:13:55.474954-04:00
- 분량: PT3M

---

> [HEADING] {"level": "1", "content": "Overview"}

**PnL Realization Horizon** is an important metric that measures how quickly an Alpha's positions translate into realized PnL. This metric helps consultants understand the time characteristics of their Alpha signals and make informed decisions about Alpha quality and suitability for different ideas.

This feature is being deployed as part of the High Turnover (HTVR) Campaign.

> [HEADING] {"level": "1", "content": "Understanding Signal Components"}

An Alpha's PnL realization can have two components:

- **Short-term Component**: Alpha positions seek to predict returns and realize PnL over 1-5 days. Current positions have little return predictability beyond 5 days.
- **Long-term Component**: Alpha positions seek to predict returns over 10-20+ days. Positions may not realize PnL quickly but accumulate over longer horizons.**How to use:** By resimulating all your submitted Alphas, you could have a picture of PnL horizon of your Alpha pool. In most cases, you may have a horizon of 20+ days, with lower turnover components reaching 40+ days. In this case, short-term realization Alphas naturally provide orthogonality to your existing Alpha pool and enhance its robustness.

> [HEADING] {"level": "1", "content": "HTVR Eligibility Criteria"}

For the High Turnover Campaign, Alphas must meet:

- Turnover greater than 20%
- PnL Realization Horizon less than 20 days OR High TVR Returns greater than 75% of total returnShort realization ensures high turnover position change activity actually captures value quickly.

> [HEADING] {"level": "1", "content": "Tips for Alpha Research"}

> [HEADING] {"level": "2", "content": "1. Validate Your Alpha Idea with Realization Horizon"}

**Before submission**, check if the PnL realization horizon matches your Alpha's thesis:

- **Momentum/News Alphas**: Should have short horizon (less than 10 days) - information decays quickly
- **Fundamental Alphas**: May have longer horizon (20-40 days) - value takes time to realize
- **High turnover Alphas**: Must have short horizon (less than 20 days) - position change costs require quick realization

> [HEADING] {"level": "2", "content": "2. Use Realization to Identify Orthogonal Signals"}

Alphas with short realization horizon are naturally orthogonal to the existing daily pool:

- Lower correlation to existing positions
- Lower impact on the book
- Better diversification benefits

> [HEADING] {"level": "2", "content": "3. Improve Alpha Quality by Checking Horizon"}

If your Alpha has:

- **High Sharpe but long horizon (greater than 40 days)**: Consider if this matches your data characteristics
- **Low Sharpe but appropriate horizon**: May still be valuable for orthogonal Alpha ideas
- **Inconsistent horizon vs idea**: May indicate overfitting or data issues

> [HEADING] {"level": "2", "content": "4. Reducing Long-Term Component"}

If you want to create shorter horizon signals, consider:

- Removes long horizon component and keeps short realization
- Subtract moving average of positions
- Use faster-changing data (tick data, news, options)

> [HEADING] {"level": "1", "content": "Common Questions"}

> [HEADING] {"level": "2", "content": "Q: How does turnover relate to realization horizon?"}

Generally, higher turnover correlates with shorter realization horizon, but not always. High turnover with long horizon may indicate unnecessary position change that does not capture value quickly.

> [HEADING] {"level": "2", "content": "Q: Should I always seek short horizon?"}

No. Match horizon to your Alpha thesis. Fundamental Alphas may legitimately have longer horizons. But for HTVR classification, short horizon (less than 20 days) is required.
