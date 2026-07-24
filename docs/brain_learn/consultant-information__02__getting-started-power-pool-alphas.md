# Power Pool Alphas

- 튜토리얼: `consultant-information` (Consultant Information)
- 페이지 ID: `getting-started-power-pool-alphas`
- 최종수정: 2026-05-19T08:55:53.278328-04:00
- 분량: PT5M

---

Power Pool Alphas are simpler, smaller, higher quality Alphas that can help you do well across Genius, competitions and themes.

> [HEADING] {"level": "1", "content": "Eligibility Criteria for Power Pool Alphas"}

Following are the list of criteria for an Alpha to be considered eligible for Power Pool Alphas:

- Sharpe >= 1.0
- Number of unique operators, including repeat operators <= 8 (ts_backfill and group_backfill are not counted)
- Number of unique data fields (excluding grouping fields) <= 3, grouping fields: country, industry, subindustry, currency, market, sector, exchange
- Power Pool Correlation < 0.5.
- Turnover tests PASS
- Sub-universe test PASS
- Robust-universe test (where applicable) PASSNote: If the Alpha has Self-Correlation among Power Pool Alphas > 0.5, it should have Sharpe 10% higher than the most correlated Alpha to be considered eligible for submission.

If in addition Alpha passes all other performance related tests it is [Power Pool + Regular] - meaning both - Power Pool and Regular alpha.

If Power Pool alpha uses data from only one dataset and passes all tests except IS Ladder - [Power Pool + ATOM] - meaning that this alpha is simultaneously Power Pool and ATOM alpha.

If [Power Pool + ATOM] alpha passes all performance related tests then it is [Power Pool + ATOM + Regular].

> [HEADING] {"level": "1", "content": "Power Pool Thematic Competition"}

- To submit Power Pool Alpha that does not PASS regular Alpha tests (pure Power Pool Alpha) – this Alpha must match Power Pool Theme
- At least 5 tagged Power Pool Alphas are required per Power Pool Thematic leaderboard to be eligible for Power Pool Thematic Competition award.You can find more information [here](https://support.worldquantbrain.com/hc/en-us/categories/37443642117783-Power-Pool-Alphas-Thematic-Competition).

> [HEADING] {"level": "1", "content": "Submission Quotas After 3 Months"}

The below quota will apply when consultants cross 3 months since date of first submitting a Power Pool Alpha:

- Each  consultant can submit up to 10 pure Power Pool Alphas per calendar month, in one single scope: **USA D0 and D1, EUR D0 and D1, ASI D1, GLB D1 and OTHER ( JPN, CHN, HKG, TWN, KOR, AMR , IND) D0 and D1**. Pure Power Pool Alphas are those that do not meet submission criteria of either Atom or Regular Alphas
- Each  consultant can submit up to 1 pure Power Pool Alpha per day
- Alpha classified as [Power Pool + ATOM] or [Power Pool + Regular] or [Power Pool + ATOM + Regular] is excluded from these two limits
- Example of daily submissions:1 Pure Power Pool Alpha
- 1 [Power Pool + ATOM] Alpha
- Total: 2 Power Pool-related submissions
- For consultants who have not crossed 3 months since their first Power Pool Alpha submission, standard submission limits for BRAIN consultants apply. Max 4 alpha submissions in a day.
- Pure Power Pool Alpha submissions must match Power Pool Theme.

> [HEADING] {"level": "1", "content": "Description of the Power Pool Alpha"}

To submit a Power Pool Alpha, it is mandatory to describe the idea in at least 100 characters. In the PROPERTIES section at the bottom of the Simulation results. Using the template of Idea and Rationale. Otherwise the Alpha will not be eligible for Power Pool.

Here is an example description:

- **Idea**: In normal market conditions, if a stock is shorted more, its likelihood of  bouncing back may also increase (reversion). However, in extreme cases where the consensus in the market is high reflecting in extremely high/low level of short interest, it may potentially be better to follow that trend
- **Rationale for data used:** shrt3_bar field is a vector data field representing the demand to borrow stock, with higher values indicating higher demand
- **Rationale for operators used:**vec_avg():  Calculates the average value of shrt3_bar for a given day
- Conditional operator: Separates normal cases from extreme ones
- ts_backfill:  Handles NaN values in the data field, detected by checking the coverage with a visualization tool

> [HEADING] {"level": "1", "content": "How do I add or remove Alphas from the Power Pool?"}

You can view the list of your Power Pool Alphas on the Alphas Page under the [Submitted](https://platform.qa.worldquantbrain.com/alphas/submitted) tab. To remove a submitted Alpha from the Power Pool, go to the Submitted tab, open the Alpha description, and click the cross next to the "PowerPoolSelected" tag in the Properties section.

**Please note that even after removing the tag, this Alpha will still be part of the self-correlation pool, so new Power Pool Alphas will still be checked for correlation against it.**

To retag a submitted Alpha later to the Power Pool, in the Properties section of the Alpha, click on the Tags dropdown and retag the alpha as "PowerPoolSelected"

> [HEADING] {"level": "1", "content": "Tips to create Power Pool Alphas"}

- Review Merged Performance before tagging the Alpha
- Explore Low turnover Alphas and liquid (small) universes, which can help improve [After Cost performance](https://support.worldquantbrain.com/hc/en-us/community/posts/29647491881623-How-to-Improve-After-Cost-Performance)
- Explore your existing Alpha pool for eligible signals
- Make use of the newly released [Investability Constrained PNL](https://support.worldquantbrain.com/hc/en-us/articles/30816357468183-How-can-I-make-use-of-Investability-Constrained-PnL-for-excelling-in-Power-Pool-Alphas-Competition)
- Your pool should have diverse signals to ensure robust performance of your pool in OS. Diversify your pool across datasets, ideas, operators, universes and even turnover (to some extent)
- After creating a sizable pool of Power Pool Alphas, consider removing Alphas which have high correlation with other Alphas in your Power Pool. This may potentially improve combo performance while reducing the Alpha count penalty
- Reach out to your research advisor for specific tips on creating good Power Pool Alphas
