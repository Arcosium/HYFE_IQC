# Consultant Features

- 튜토리얼: `consultant-information` (Consultant Information)
- 페이지 ID: `consultant-features`
- 최종수정: 2026-03-19T01:21:55.747360-04:00
- 분량: PT10M

---

> [HEADING] {"level": "1", "content": "Consultant-Only Features"}

Welcome to BRAIN platform consulting! Consultants have access to numerous exclusive features including:

- and to trade in European and Asian markets
- A [visualization tool](https://platform.worldquantbrain.com/learn/documentation/consultant-information/visualization-tool) in simulation results to analyze and fine-tune Alphas
- Potential to have your Alphas actually traded, and receive a portion of the associated PnL
- [Consultant competitions](https://platform.worldquantbrain.com/competitions)
- [Simulating multiple Alphas](https://platform.worldquantbrain.com/learn/documentation/create-alphas/multi-alpha-simulation) simultaneously
- Creating teams to generate Alphas and share a portion of the PnL generated

> [HEADING] {"level": "1", "content": "Out Sample Tests"}

Below is the list of out sample tests run on the Alphas:

> [TABLE] {"data": [["Test", "Description", ""], ["Bias*Mandatory*", "Biases are human tendencies that lead us to follow a particular quasi-logical path. The goal of the Bias test is to detect forward bias. An Alpha passes the Bias test if it generates an identical set of positions (for the previous business day), when it is run twice, at different times (as of today's date).", ""], ["Checkpoint*Mandatory*", "This test is designed to test for checkpoint support. It will run the simulation twice with two different EndDates. The first simulation produces the checkpoint file and the second simulation loads the checkpoint file. The overlap between the two PnL files produced by the two simulations must be identical.", ""], ["ISSharpe*Mandatory*", "The goal of the ISSharpe test is to weed out random noise from the true Alpha, and it does so by comparing the IS distribution of the Alpha's PnL series with the distribution of a random walk (with mean zero).", ""], ["CorrAll*Mandatory*", "The The correlation test ensures that incoming Alphas are not overly similar to Alphas that are already in the existing PROD Alpha Pool by checking the position, PNL and trade correlation of the Alpha. For the Alphas that are similar, the performance needs to be higher than the Alphas that they are highly correlated with.", ""], ["Memory Usage*Mandatory*", "Alphas are tested on the maximum size of non-swappable physical memory usage (resident size) for each simulation. Those using more than 6 Gb fail on checkMemoryUsage test, and will be skipped for other out of sample tests.", ""], ["Weight*Mandatory*", "An equity Alpha passes this test if the maximum weight in any one stock is < 8% in USA.", ""], ["Drawdown", "Drawdown is the peak-to-trough decline during a specific record period. We calculate the biggest Drawdown for the full Out-of-Sample period, i.e. from Birthday to EndDate.", ""], ["NewHigh", "Buy low, sell high. This test checks that Alpha reached its First New High after Birthday.", ""], ["OSSharpe", "The goal of the OSSharpe test is to weed out random noise from true Alpha, and it does so by comparing the OS distribution of the Alpha's PnL series with the distribution of a random walk (with mean zero).", ""], ["ProdDate", "ProdDate is the date on which an Alpha graduates from Out-of-Sample Testing and reaches Production.", ""], ["RankSharpe", "Ranked Sharpe is defined as Sharpe of the Alpha after applying the operators rank, and power(exp=3) separately to long and short sides of the Alpha, then re-scaling each side to its original size.\nAlpha passes the test if:\n    1.  The Sharpe is positive.\n    2.  Rank Sharpe over past 2 years is greater than 0.5*Sharpe or 0.15.", ""], ["SubUniverse", "Threshold1 = max(M,sqrt(subUnivSize/largestUnivSize)*N) For Delay 1: M = 0.065, N = 0.15 For Delay 0: M = 0.1, N = 0.25 Threshold2 = 0.75* OriginalSharpe/sqrt(original_universe_size/smaller_universe_size) RULE: SubuniverseSharpe >= min(Threshold1, Threshold2)", ""], ["SuperUniv", "SuperUniverse value is the Sharpe of the next larger standard universe. An Alpha will pass SuperUniverse test if its Sharpe when applied to the next larger universe is greater than 0.7 * Sharpe of the Alpha itself.", ""]], "firstRowIsTableHeader": true, "firstColIsHeader": false}

> [HEADING] {"level": "1", "content": "Prod Correlation"}

Under the Stats tab of Alpha simulation results, there are the buttons [Generate Self Correlation]($tutorialpage/interpret-results/simulation-results) and Generate Prod Correlation. Generate Prod Correlation is only available to consultants. Clicking it displays both the current Alpha’s self-correlation (with up to the 5 most and 5 least correlated Alphas in your Alpha pool) and a log-scale histogram of the current Alpha’s correlation with all Alphas in the pool. Correlation is based on daily PnL over the simulation period.

> [HEADING] {"level": "1", "content": "Additional Simulation Settings for Consultants"}

> [HEADING] {"level": "1", "content": "Pasteurize"}

Pasteurization replaces input values with NaN (pasteurizes) for instruments not in the Alpha universe. When Pasteurize = ‘On’, inputs to will be converted to NaN for instruments not in the universe selected in Simulation Settings. When Pasteurize = ‘Off’, this operation does not happen and all available inputs are used.

Pasteurized data has non-NaN values only for instruments in the Alpha universe. While pasteurized data contains less information, it may be more appropriate when considering cross-sectional or group operations. The default Pasteurize setting is ‘On’. Researchers can switch it to ‘Off’ and use pasteurize(x) operator for manual pasteurization.

*Example*

Assume the following settings are used: Universe TOP500, Pasteurize: ‘Off’. The following code calculates the difference between sector rank of sales_growth in Alpha universe and sector rank of sales_growth among all instruments:

group_rank(pasteurize(sales_growth),sector) - group_rank(sales_growth,sector)

The pasteurize operator in the first group_rank pasteurizes input to the Alpha universe (TOP500), while the second group_rank ranks sales_growth among all instruments.

> [HEADING] {"level": "1", "content": "NaNHandling"}

NaNHandling replaces NaN values with other values. If NaNHandling: ‘On’, NaN values are handled based on operator type. For time series operators, if all inputs are NaN, 0 is returned. For group operators returning one value per group (e.g. groupmedian, groupcount), if the input value for an instrument is NaN, the value for the group is returned.

If NaNHandling : ‘Off’, NaNs are preserved. For time series operators, if all inputs are NaN, NaN is returned. For group operators, if the input value for an instrument is NaN, NaN is returned. Researchers should handle NaNs manually in this case. The default setting NaNHandling value is ‘Off’. Some ways to manually handle NaN values can replicate “On” behavior.

*Example*

ts_zscore(etz_eps, 252)

Assume NaNHandling = ‘On’. Then for a stock with etz_eps == NaN for all 252 days, 0 is returned. However, ts_zscore(x, d) also returns 0 when x == tsmean(x, d), which is different from x == NaN (“no data is available”). This means that NaNHandling = ‘On’ increases coverage, but may introduce ambiguous information into the Alpha.

If NaNHandling = ‘Off’, NaNs can be handled other ways:

is_nan(ts_zscore(etz_eps, 252)) ? ts_zscore(est_eps, 252) : ts_zscore(etz_eps, 252)

Here, est_eps is used when etz_eps has NaN value for all 252 days.

*Example*

groupmax(sales, industry)

When NaNHandling = ‘Off’ and sales is NaN for a given instrument, the operator’s output is NaN. When NaNHandling = ‘On’ and sales is NaN for a given instrument, the operator’s output is the maximum value of sales in the instrument’s industry.

> [HEADING] {"level": "1", "content": "Test Period"}

> [IMAGE] {"title": "Settings dropdown", "width": 692, "height": 411, "fileSize": 19538, "url": "https://api.worldquantbrain.com/content/images/KzCW120eYiUuNb6ERHERlWEUqLk=/288/original/Settings_dropdown.png"}

The Test Period is a feature designed to enhance your Alpha and SuperAlpha testing process. This tool allows you to set a separate test period from your IS period, providing a more flexible approach to testing your research ideas.

Using the Feature:

The Test Period feature is designed to help you avoid overfitting. It allows you to divide your In-Sample (IS) period into a Train and Test period. The Train period can be utilized to develop your Alphas and SuperAlphas, while the Test period is ideal for validating them. An Alpha or SuperAlpha that is developed based on the simulation results of Training Period and performs well in both periods is likely a strong candidate for submission and may have avoided overfitting.

While choosing a Test period does not directly affect the simulation, it influences the statistics and the visualization. The submission tests will run on the entire 10-year period, with the simulation running on the entire 10-year IS. However, if a testing period is chosen, the simulation stats will be divided into two sections: one covering the training period and another for the test period.

Navigating the Feature:

- Selecting the Test Period: In Simulation Settings, you can define a test period corresponding to the final 0-6 years of the IS period. By default, no test period is set (0 years).
- Visualizing the Test Period: The Stats Summary defaults to the training period. You can view the stats for the test period by clicking on the “Show test period” button.
- Identifying the Test Period on Graphs: The lines representing the test period on the graphs are colored orange.
- Choosing the Stats Summary: You can select between the Stats Summary for the test period or the entire IS period by choosing the “TEST” or “IS” in the Summary section, respectively.
- Hiding the Test Period: A button “Hide test period” allows you to hide the test period, if desired. Note that an Alpha or SuperAlpha can only be submitted when the Test Period is revealed by clicking on the “Show test period” button.
- Understanding the Stats: The yearly IS stats are divided between Train and Test periods, represented by blue and orange indicators respectively.

> [IMAGE] {"title": "image_edit.png", "width": 1045, "height": 442, "fileSize": 196993, "url": "https://api.worldquantbrain.com/content/images/il84U1-fe60rE0lhHg_oXWEf_S4=/285/original/image_edit.png"}

A. Orange - test period PnL, Blue - Train period PnL. B. View IS summary by selecting different periods

> [HEADING] {"level": "1", "content": "Max Trade"}

Max Trade is a feature designed to enhance the liquidity and scalability of your Alphas. It limits the daily position changes of instruments based on a fraction of their 20-day average daily volume (ADV20). This ensures that your Alphas adjust positions less frequently in illiquid instruments, which can significantly reduce turnover and improve the Sharpe ratio of your sub-universe.

When this feature is turned ON, the Investability Constrained metrics will not be calculated for your Alphas. Generally, you may observe a drop in performance metrics (Sharpe ratio and returns) compared to the original Alpha performance. For a better understanding of the Investability Constraint concept, please refer to this [documentation](https://platform.worldquantbrain.com/learn/documentation/advanced-topics/getting-started-investability-constrained-metrics).

The Max Trade option must be set to ON for all Alphas in the ASI, JPN, HKG, KOR, TW regions. This setting optimizes ASI Alphas and improves After Cost Performance at combo level.

> [IMAGE] {"title": "max_trade_settings.png", "width": 624, "height": 442, "fileSize": 46781, "url": "https://api.worldquantbrain.com/content/images/JwWVUWRjohnOUsF0t5joWH7bsVs=/394/original/max_trade_settings.png"}

> [HEADING] {"level": "1", "content": "Max Position"}

Max Position is an investability constraint seeking to improve the liquidity and scalability of Alphas on the BRAIN. It constrains position on an instrument as a fraction of its daily dollar volume, reducing unrealistic positions on illiquid instruments.

In practice, the Alpha’s position in each stock is capped at a fixed fraction of its dollar volume, so that positions in illiquid names remain small and more realistic from a capacity perspective.

When Max Position is turned ON, separate Investability Constrained metrics are not computed for that Alpha. You should generally expect lower raw performance metrics (e.g., Sharpe ratio and returns) versus the unconstrained Alpha, as aggressive, non-investable positions are scaled back. This is typically offset by more realistic after cost behavior at the combo level. For a deeper discussion of the Investability Constraint framework, please refer to the linked [documentation](https://platform.worldquantbrain.com/learn/documentation/advanced-topics/getting-started-investability-constrained-metrics).

The Max Position option can be enabled for all Alphas in the USA, ASI, and EUR regions. As a best practice, enabling Max Position in these regions helps steer capacity toward more liquid names and improves after cost performance when Alphas are combined.

> [IMAGE] {"title": "Picture1.png", "width": 624, "height": 481, "fileSize": 58619, "url": "https://api.worldquantbrain.com/content/images/9jqVEXCe3sTfibFA7SZpvY1OyjA=/456/original/Picture1.png"}

> [HEADING] {"level": "1", "content": "BRAIN Labs"}

BRAIN Labs is a new feature accessible to only consultants. This allows to interact with the platform through coding. With the help of BRAIN Labs, you can access data field values and visualize distributions across instruments, time & groups, using popular Python libraries. You can also build models, test ideas and replicate BRAIN operators to study Alpha weights. Check out these videos to get started:

- [Video 1](https://share.vidyard.com/watch/CdpiecTgBJu39JiXCrGAiL)
- [Video 2](https://share.vidyard.com/watch/27Vjmc3EnJuyhJvTGLPgid)
