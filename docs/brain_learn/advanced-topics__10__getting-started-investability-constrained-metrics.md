# Investability Constrained Metrics

- 튜토리얼: `advanced-topics` (Getting Started)
- 페이지 ID: `getting-started-investability-constrained-metrics`
- 최종수정: 2025-11-04T02:32:14.611009-05:00
- 분량: PT3M

---

> [HEADING] {"level": "1", "content": "Introduction"}

When evaluating Alpha performance, various metrics can be used. One metric of them is Alpha Performance after Investability Constraint.

Investability Constraints ensure that the positions taken by an Alpha are within the liquidity limits of the instruments. This helps avoid significant market impact, which can negatively affect the Alpha's profitability. An Alpha that performs well under Investability Constraints will potentially have higher capacity and liquidity.

**For example:**

Consider two Alphas that allocate weights to stock X, scaled to a book size of $20 million:

> [TABLE] {"data": [["Alphas", "Allocation (million $)", "Average Daily Volume of Stock X (million $)"], ["A", "5", "50"], ["B", "0.1", "50"]], "firstRowIsTableHeader": false, "firstColIsHeader": true}

Alpha A's position is harder to change because it represents 10% of the average daily volume (ADV) of stock X. In contrast, Alpha B's position has more room for adjustment. This difference can have a significant impact when considering thousands of stocks in a universe.

> [HEADING] {"level": "1", "content": "Where to find the metrics"}

You can find the Investability Constrained Metrics on the new simulation results page. The page displays the Alpha's PnL under Investability Constraint conditions.

> [IMAGE] {"title": "Investability _1.png", "width": 624, "height": 421, "fileSize": 51789, "url": "https://api.worldquantbrain.com/content/images/ogx8yz2CbMhzIOjee65t2RsQPx8=/380/original/Investability__1.png"}

Additionally, the IS Summary now includes new aggregate data for Alpha performance after applying Investability Constraints.

> [IMAGE] {"title": "Investability _2.png", "width": 548, "height": 560, "fileSize": 122374, "url": "https://api.worldquantbrain.com/content/images/XeYvX8AWP2zvTcamIhFrpYjkaZ8=/381/original/Investability__2.png"}

> [HEADING] {"level": "1", "content": "How to use the metric in your Alpha research"}

- **Performance Consistency**: Ensure that Alphas do not lose too much performance under Investability Constraints compared to their base performance. Alphas that have good Investability Constraints Sharpe are likely to have good After-cost Sharpe also which will help improve your Combine Alpha Performance in Genius.
- **Optimization Metrics**: Consultants can leverage Investability Constrained Metrics as a metric to enhance your Alpha optimization process. Here’re some tips to create Alphas which such performance:An Alpha which captures signal in the liquid instruments space of the market may have higher Investability Constrained Performance. This is especially true for cases of medium to high turnover like Alphas in Price Volume, News, Options, etc. category. You should either control the liquidity profile of your signals or lower the turnover of the illiquid part of them. Please checkout some forum post about these technique in the References.
- An Alpha with low turnover in general may be less impacted by these constraint for example Alphas in category Fundamental, Model, … Because it changes position in low frequency, thus its performance relies less on the liquidity of the instruments. Please checkout some forum post about creating low turnover Alphas in the References.
- **Alpha Submissions Selection**: Use these metrics as a filter for selecting Alphas, especially in regions like ASI and GLB. Alphas that perform well under these constraints can possibily add significant value and may result in higher value factor and weight factor because they scale better when the booksize increases.
- **Apply Max Trade Settings to your Alpha**: In order to convert your Alphas into Investability Constrained Alphas, you can change the Max Trade option in Simulation Settings to ON. Please read [this documentation](https://platform.worldquantbrain.com/learn/documentation/consultant-information/consultant-features) for further information.

> [IMAGE] {"title": "max_trade_settings.png", "width": 624, "height": 442, "fileSize": 46781, "url": "https://api.worldquantbrain.com/content/images/JwWVUWRjohnOUsF0t5joWH7bsVs=/394/original/max_trade_settings.png"}

> [HEADING] {"level": "1", "content": "References"}

Research Papers:

- [The market impact of large trading orders: Correlated order flow, asymmetric liquidity and efficient prices](https://haas.berkeley.edu/wp-content/uploads/hiddenImpact13.pdf)
- [The Impact of Large Orders in Electronic Markets](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2480954)BRAIN Forum posts & Articles:

- [Tips for Success in the High Capacity Alphas Competition](https://support.worldquantbrain.com/hc/en-us/community/posts/29520890528151-Tips-for-Success-in-the-High-Capacity-Alphas-Competition)
- [How to Improve After Cost Performance](https://support.worldquantbrain.com/hc/en-us/community/posts/29647491881623-How-to-Improve-After-Cost-Performance)
- [What is liquidity?](https://support.worldquantbrain.com/hc/en-us/articles/5972783716887-What-is-liquidity)
- [What's After cost Sharpe ratio?](https://support.worldquantbrain.com/hc/en-us/articles/25960264611351-What-s-After-cost-Sharpe-ratio)
- [Can you explain what decay is?](https://support.worldquantbrain.com/hc/en-us/articles/5973492634903-Can-you-explain-what-decay-is)
- [Do you recommend higher or lower Decay?](https://support.worldquantbrain.com/hc/en-us/articles/5973359129239-Do-you-recommend-higher-or-lower-Decay)
