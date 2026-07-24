# Getting Started: China Research for Consultants [Gold]

- 튜토리얼: `regions-and-universes` (Regions and Universes)
- 페이지 ID: `china-region-consultants`
- 최종수정: 2025-06-02T02:36:59.225739-04:00
- 분량: PT3M

---

> [HEADING] {"level": "1", "content": "REGION SPECIFICS – Things to consider"}

With BRAIN you can create alphas on China’s Stock market - the second largest in the world by capitalization. There are two primary exchanges:

- Shanghai Stock Exchange (SSE): >2000 listed companies, market cap >7 trillion USD
- Shenzhen Stock Exchange (SZSE): >2000 listed companies, market cap >5 trillion USD

> [HEADING] {"level": "2", "content": "Different submission criteria"}

The China market has a high cost of trading, thus requiring higher returns than other regions.

- D1 criteria: Sharpe >= 2.08; Returns >= 8%; Fitness >= 1.0
- D0 criteria: Sharpe >= 3.5; Returns >= 12%; Fitness >= 1.5[**Daily trading limit**](https://www.investopedia.com/terms/d/daily_trading_limit.asp#:~:text=A%20daily%20trading%20limit%20is,occurring%20over%20one%20trading%20day.)**:** price can change in 5-10% range based on stock type. The daily price limit does not apply on the first five trading days after an IPO.[1]

[**Short-selling Restriction:**](https://www.investopedia.com/ask/answers/09/short-selling-china.asp) both short selling and margin buying is allowed only for eligible "blue chip" stocks with good earnings performance and is only permitted for locally licensed investors.

This implies that the opposite alphas will not flip the performance. Check alpha example with same simulation setting below:

> [IMAGE] {"title": "CHN_1", "width": 1077, "height": 506, "fileSize": 78053, "url": "https://api.worldquantbrain.com/content/images/IXeVl9KLtAFM9AT65sjUe4oLiOs=/267/original/chn1.png"}

> [HEADING] {"level": "1", "content": "CHN region simulation settings"}

China [alphas](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=A-,Alpha,-An) are created and simulated on the “Simulate” page. To run your first [simulation](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=definition.-,Simulation,-Simulation) on China region:

- Click on the gear icon under your simulation tab to open the settings panel.
- Select “CHN” in Region drop down menu and click “Apply”

> [IMAGE] {"title": "chn_2", "width": 676, "height": 518, "fileSize": 36818, "url": "https://api.worldquantbrain.com/content/images/IBHSkkqEnyn4nXE2OxHtEDpWanc=/268/original/chn2.png"}

> [HEADING] {"level": "1", "content": "The replicate of China Stock Index 1000 on BRAIN"}

The CSI 1000 Index is composed of 1,000 small-scale and well-liquid stocks after excluding the constituent stocks of the CSI 800 Index from all A-shares. It comprehensively reflects the stock price performance of small and medium-sized companies in China's A-share market. You can replicate this index on BRAIN platform:

> [IMAGE] {"title": "chn3.png", "width": 731, "height": 736, "fileSize": 57846, "url": "https://api.worldquantbrain.com/content/images/c9dffleUllw_8WD5KN787R1UCvE=/269/original/chn3.png"}

> [HEADING] {"level": "2", "content": "Formula"}

> [SIMULATION_EXAMPLE] {"settings": {"instrumentType": "EQUITY", "region": "CHN", "universe": "TOP2000", "delay": 0, "decay": 0, "neutralization": "NONE", "truncation": 0.0, "pasteurization": "ON", "unitHandling": "VERIFY", "nanHandling": "OFF", "language": "FASTEXPR", "maxTrade": "OFF", "maxPosition": "OFF"}, "type": "REGULAR", "regular": "rank(cap) < 0.6 ? rank(cap) > 0.1 ? cap : 0 : 0"}

Since CSI1000 takes 1000 stocks based on the market cap ranking from 800 to 1800, so we have this range from 0.1 to 0.6.

> [HEADING] {"level": "1", "content": "Getting started"}

- Although China’s stock market has several unique characteristics, many common research ideas can be good starting point for your China research before you explore specific ideas:Technical indicators (Stochastic Oscillator, Relative Strength Index, MACD etc.)
- Fundamental ratios
- The China market has a high cost of trading, thus requiring higher returns than other regions.
- Apart from usual robustness tests such as sub universes, turnover, fitness and weight, there is an additional test exclusive to the China research region: Robust universe test performance: Alphas are considered good if the robust universe component retains at least 40% of the returns and Sharpe of the submission version.

Since China market has a price limit system, (i.e., the Shenzhen Stock Exchange and Shanghai Stock Exchange resumed the 10% symmetric price limit system on 26 December 1996 [1] ), you will observe the fact that the opposite alphas will not flip the performance (i.e., Sharpe, Returns, Fitness) of alphas.

Please see the following example:

Alpha= ts_returns(close,5) with Delay=”1”, Neutralization = “industry”, Decay = “0”, Truncation = “0.01”, Universe=”TOP3000” will have the performance as follows:

Sharpe=-6.10, Turnover=63.34%, Returns=-72.73%, Margin=-229.10

However, the oppsite alpha:

Alpha= -ts_returns(close,5) with Delay=”1”, Neutralization = “industry”, Decay = “0”, Truncation = “0.01”, Universe=”TOP3000”

will have the performance as follows:

Sharpe=1.86, Turnover=62.69%, Returns=22.99%, Margin=73.30

Don’t miss your chance to make unique alphas for the China region and boost performance of your BRAIN alpha portfolio!

[1]: [The impact of price limit system on the comprehensive quality of the stock market: Research on long-term and short-term effects based on submarkets](https://www.tandfonline.com/doi/full/10.1080/23322039.2022.2106635)
