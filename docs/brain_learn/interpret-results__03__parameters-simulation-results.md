# Parameters in the Simulation results

- 튜토리얼: `interpret-results` (Interpreting Results)
- 페이지 ID: `parameters-simulation-results`
- 최종수정: 2025-04-04T04:35:07.278227-04:00
- 분량: PT6M

---

In the [Simulation](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=definition.-,Simulation,-Simulation) result page, you will find a ratings panel in the Stats tab of Results that says Spectacular, Excellent, Good, Average or Needs Improvement depending on your Alpha’s Fitness as shown below:

> [TABLE] {"data": [["Label", "Fitness for Delay 1", "Fitness for Delay 0"], ["Spectacular", "> 2.5", "> 3.25"], ["Excellent", "> 2", "> 2.6"], ["Good", "> 1.5", "> 1.95"], ["Average", "> 1", "> 1.3"], ["Needs Improvement", "<= 1", "<= 1.3"]], "firstRowIsTableHeader": true, "firstColIsHeader": false}

> [HEADING] {"level": "1", "content": "Return"}

Return is the gain or loss of a security or portfolio in a particular period. Return consists of the income received plus capital gains, relative to the amount of the investment. In BRAIN, return = annualized PnL / half of book size.

> [HEADING] {"level": "1", "content": "Sharpe and IR"}

[Information ratio (IR)](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details%C2%A0*).-,Information%20ratio,-Information) measures the prediction ability of a model. In BRAIN, it is defined as *the ratio of a portfolio’s mean daily* [*returns*](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details.-,Returns,-Returns)* to the volatility of those returns:*

> [EQUATION] IR = \frac{mean(PnL)}{stdev(PnL)}

where [PnL](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=consultants-,Profit%20and%20Loss%20(PnL),-Profit) is the daily profit and loss, in dollars.

[Sharpe](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details.-,Sharpe%20ratio,-Sharpe) is the annualized version of the IR statistic, i.e. Sharpe = sqrt (252)*IR ≈ 15.8*IR; where 252 is the average number of trading days (days the markets are open) in the USA in a year.

Sharpe or IR measures the returns of an Alpha while attempting to identify its consistency. The higher the IR, the more consistent the Alpha’s returns are, and consistency is an ideal trait. High Sharpe (or IR) is more desirable than just high return.

*Note: Sharpe and* *IR* *may be defined somewhat differently elsewhere than in* BRAIN*.*

> [HEADING] {"level": "1", "content": "Fitness"}

[Fitness](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=ratios.-,Fitness,-Fitness) of an [Alpha](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=A-,Alpha,-An) is a function of Returns, [Turnover](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details.-,Turnover,-Average) and Sharpe:

> [EQUATION] Fitness = Sharpe \cdot \sqrt{\frac{abs(Returns)}{max(Turnover,0.125)}}

Good Alphas have high fitness. You can optimize the performance of your Alphas by **increasing Sharpe (or returns) and reducing turnover**. Improving one factor normally has an adverse impact on the other factor. As you work on optimizing your Alpha, an improvement in its fitness is an indication that your changes are having a positive impact.

> [HEADING] {"level": "1", "content": "Cumulative PnL Chart"}

**Cumulative PnL Chart:** A graph (shown below) of an Alpha’s performance ([PnL](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=consultants-,Profit%20and%20Loss%20(PnL),-Profit)) over entire [simulation](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=definition.-,Simulation,-Simulation). This graph can be zoomed in by clicking and dragging below the plot area. Start and end dates for PnL plotting can also be changed here. Clicking the [Sharpe Ratio](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details.-,Sharpe%20ratio,-Sharpe) in dropdown menu at the upper right from PnL graph displays the Sharpe ratio graph (Sharpe over time). Make sure that the PnL graph has an upward trend, the Sharpe is high and the [Drawdown](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=today-,Drawdown,-Drawdown) is kept to a minimum.

> [IMAGE] {"title": "Cumulative PnL", "width": 525, "height": 360, "fileSize": 48049, "url": "https://api.worldquantbrain.com/content/images/q9drQriu1gSqBNnw2eoWE6U88VY=/9/original/Cumulative_PnL.png"}

> [HEADING] {"level": "1", "content": "IS Summary"}

**IS Summary:** Scrolling down to the Stats block (shown below) of the simulation results shows various metrics about the Alpha's performance.

> [IMAGE] {"title": "IS_Result.PNG", "width": 1187, "height": 388, "fileSize": 32259, "url": "https://api.worldquantbrain.com/content/images/Mo8HCP_leffAynw_hsX6NAj4XKc=/260/original/IS_Result.PNG"}

**Year:** The year on which the data was simulated. The last row shows the Alpha’s performance over all years.

**Long/Short Count:** The number of [instruments](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details.-,Instrument,-Instrument) in long or short positions, respectively.

**Sharpe**: Sharpe = IR * Sqrt(252), where IR = Avg(PnL)/Std_dev(PnL) over the observed time period.

**Fitness** is defined in the [Alpha Performance]($tutorialpage/interpret-results/alpha-performance) help page: Fitness = Sharpe * sqrt(abs([Returns](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details.-,Returns,-Returns)) / max([Turnover](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details.-,Turnover,-Average), 0.125)).

**Returns:** The return on capital traded: Annual Return = Annualized PnL / Half of Book Size. It signifies the amount you made or lost during the period observed and is expressed in %. Book size refers to the amount of capital (money) used to trade during the simulation. Book size is constant and is set to $20 million every day throughout the simulation. Profit is not reinvested, and losses are replaced by cash injection into the portfolio. BRAIN assumes you have $10 million and will invest in assets up to $20 million. This is called leverage. Performance (like Returns, Sharpe) is computed on a base of $10 million.

**Turnover:** Turnover signifies how often one trades. It can be defined as the ratio of value traded to book size. Daily Turnover = Dollar trading [volume](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=time.-,Volume,-Volume)/Book size. Good Alphas have low turnover, since low turnover means lower transaction costs.

**Margin:** The profit per dollar traded; calculated as PnL divided by total dollars traded for a given time period.

**PnL**: Profit and Loss (PnL) is the money that the positions and trades generate (which means it is the amount of money you lost or made during the year), expressed in dollars.

daily_PnL = sum of (size of position * daily_return) for all instruments, where the daily return per instrument = (today’s close / yesterday’s close) – 1.0.

**Drawdown** - the largest reduction in PnL during a time period, expressed as a percentage. It is calculated as follows: find the largest peak to trough drawdown in PnL, and divide its dollar amount by half of book size ($1

> [HEADING] {"level": "1", "content": "Self-Correlation"}

**Generate Self Correlation:** Clicking the Down Arrow button in a [Self Correlation](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=details%C2%A0*).-,Self%20correlation,-Maximum) row will produce a table with the performance statistics of up to the 5 most correlated Alphas you submitted that qualified for OS testing. This information is meant to help the user ensure they have a diverse set of Alphas. This information can also be accessed by clicking on the Alpha in the Alphas page.

> [IMAGE] {"title": "Correlation", "width": 967, "height": 381, "fileSize": 21715, "url": "https://api.worldquantbrain.com/content/images/6nYq2R2YwLwHF4Vx5OySMgMjbXo=/39/original/Correlation.PNG"}

> [HEADING] {"level": "1", "content": "IS, Semi-OS & OS"}

The rolling 5-year In-Sample simulation period begins seven years ago and ends two years ago, updating daily. Using simulation settings, you can divide your In-Sample (IS) period into a Train and Test period. The Train period can be utilized to develop your Alphas and SuperAlphas, while the Test period is ideal for validating them. An Alpha developed based on the simulation results of training period and performs well in both periods is likely a strong candidate for submission and may have avoided overfitting.

The latest two years of data, the Semi-OS, are hidden for scoring and testing purposes. Consultants have access to a 10-year In-sample period, instead of 5-year.

Keeping the last 2 years of data hidden leads to higher confidence in the Out-Sample (OS) performance of Alphas and their scores. Statistics shown in the OS Tab of **My Alphas** page will be populated as data becomes available by each passing day.

> [IMAGE] {"title": "IS, Semi OS and OS", "width": 985, "height": 239, "fileSize": 75388, "url": "https://api.worldquantbrain.com/content/images/GBxJv7oA55p6vNjbyby5-t8lTIw=/290/original/image_2.png"}

> [HEADING] {"level": "1", "content": "Alpha Statuses"}

- Following successful simulation, the Alpha is labeled as "UNSUBMITTED."
- Upon submission, the Alpha is assigned the "ACTIVE" status.
- For consultants, ACTIVE Alphas are qualified to accumulate weight and are eligible to contribute to the consultants' quarterly payments, as further described in their respective consulting or service agreements. This ACTIVE status will be kept until the dataset they rely upon is decommissioned or if WorldQuant otherwise decommissions an Alpha, in its discretion.
- In case the dataset in no longer available or there is prolonged underperformance of the Alpha in the Out-Sample period, the Alpha's status is revised to "DECOMMISSIONED".
- Decommissioned Alphas do not accrue weight and are not eligible to contribute to your quarterly payment.

> [IMAGE] {"title": "Alpha Lifecycle", "width": 842, "height": 110, "fileSize": 12321, "url": "https://api.worldquantbrain.com/content/images/YmSm8cBVOMDvyDAQbW2aqo7AiaU=/279/original/lifecycle_alpha_latest.png.jpg"}
