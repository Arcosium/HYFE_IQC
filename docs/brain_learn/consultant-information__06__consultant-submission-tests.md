# Consultant Submission Tests

- 튜토리얼: `consultant-information` (Consultant Information)
- 페이지 ID: `consultant-submission-tests`
- 최종수정: 2026-07-20T00:34:49.558579-04:00
- 분량: PT7M

---

> [HEADING] {"level": "1", "content": "Alphas excluding CHN region"}

The following tests are run till the end of In Sample period.

> [TABLE] {"data": [["SUBMISSION CRITERIA", "THRESHOLDS FOR CONSULTANTS"], ["Fitness", "Greater than 1.5 for Delay 0 alphas and Greater than 1 for Delay 1 alphas"], ["Sharpe", "Greater than 2.69 for Delay 0 and Greater than 1.58 for Delay 1 alphas"], ["Turnover", "Greater than 1% and less than 70%"], ["Weight", "Max weight in any stock < 10%. This measures if sufficient number of stocks are assigned weight for sufficient days in a year. Number varies with simulation universe (Top 3000, Top 2000 etc.)"], ["Sub universe test", "The Sharpe in the sub universe must be higher than at least one threshold. These thresholds scale down sharpe with sub universe size. You can find detailed example below."], ["Self-correlation", "Less than 0.7 PNL series correlation with user's alphas, or a sharpe at least 10% greater than other correlated alphas submitted by user"], ["Prod-correlation", "Same as the Self-correlation criteria but applied to all submitted alphas in BRAIN, not just your own"], ["Check-IS-Sharpe or IS-Ladder test", "In Sample Sharpe for the recent 2, 3, 4...10 years should be above the Sharpe thresholds set up for D1 and D0. Please find detailed information below about the D0 and D1 thresholds and the ladder test"], ["Bias test", "This measures any forward bias in the alpha. This test should not fail for expression alphas. In case any of your expressions fail Bias test, please contact BRAIN Support."]], "firstRowIsTableHeader": true, "firstColIsHeader": true}

> [HEADING] {"level": "1", "content": "Alphas in CHN region"}

- The China market has a high cost of trading, thus requiring higher returns than other regions. Thus the submission criteria are **Sharpe >= 2.08, Returns >= 8% and Fitness >= 1.0 for D1; Sharpe >= 3.5, Returns >= 12% and Fitness >= 1.5 for D0**
- Apart from usual robustness tests such as sub universes, turnover, fitness and weight, there is an additional test exclusive to the China research region: **Robust universe test performance**: Alphas are considered good if the robust universe component retains at least 40% of the returns and Sharpe of the submission version.

> [HEADING] {"level": "1", "content": "ASI Alphas: Robust Universe Performance Test"}

**What is Robust Universe Test Criteria?**

The ASI research region introduces a new and exclusive robustness assessment: the **Robust Universe Test**. This test is designed to ensure that Alphas maintain strong, reliable performance even when a more scalable universe is adjusted to better reflect real-world scalability constraints. An Alpha passes the Robust Universe Test if its performance on the adjusted universe retains **at least 90%** of the returns and Sharpe ratio compared to the original submission version.

**Actionable tips to potentially pass Robust Universe Test for ASI Alphas**
 Build Alphas that perform well across a wide, diverse universe, rather than relying on signals from a limited group of stocks. Also, prioritize signals with strong, intuitive economic rationale and broad applicability, rather than those tailored to specific stocks or historical quirks. Before submitting, test your Alpha on various universe configurations to ensure robust performance retention. You can also use visualization function to see how your Alpha performs in different countries/regions; try to submit Alphas that can have performance in various countries/regions rather than just a few countries/regions.

> [IMAGE] {"title": "ASI Alphas.png", "width": 617, "height": 263, "fileSize": 81957, "url": "https://api.worldquantbrain.com/content/images/VyUyu7a3WYpKGfw7nwcf1nVP5Zg=/423/original/ASI_Alphas.png"}

And try to aim for higher returns and greater margin, as Alphas with stronger performance and wider profit margins are more resilient to real-world investment costs and constraints and are more likely to be selected for deployment.

> [HEADING] {"level": "1", "content": "ASI, JPN, HKG, TWN, KOR Alphas: Investability Sharpe test"}

Please refer to [Investability Constrained Metrics | WorldQuant BRAIN](https://platform.worldquantbrain.com/learn/documentation/advanced-topics/getting-started-investability-constrained-metrics) for details about investability concept and max-trade.

For ASI, JPN, HKG, TWN, KOR Alphas, they can pass the investability Sharpe test, in either way:

- Set maxtrade=ON
- Set maxtrade=OFF, and ensure "investability constraint Sharpe" > "original Sharpe" * 0.7

> [HEADING] {"level": "1", "content": "Superalphas"}

Same submission criteria apply for superalphas as alphas, except turnover. **2% <= turnover < 40%**

> [HEADING] {"level": "1", "content": "Alphas in ILLIQUID_MINVOL1M Universes"}

The ILLIQUID_MINVOL1M universe contains a basket of illiquid instruments that have a minimum volume of 1 million USD. As illiquid instruments have high slippages, wider bid ask spreads and low volume which is not found in the liquid universes, there is a larger difference in returns and wider price impact when traded compared to liquid universes.

Thus, the **after cost sharpe** scores the returns taking into account the above costs of trading.

An additional condition that the After cost sharpe for most illiquid 50% instruments should be greater than fraction (~52.5%) of original universe after cost sharpe apply to this universe.

> [HEADING] {"level": "1", "content": "Self-Correlation"}

Self correlation tests checks if the submitted alpha is highly correlated to previous submissions made by the user.

The threshold for correlation is 0.7. In case correlation between submitted alpha and previous submissions is greater than 0.7, below conditions apply:

> [HEADING] {"level": "1", "content": "Weight test"}

Alphas are also tested on the distribution of Alpha [weights](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=W-,Weight,-BRAIN) across stocks. Alphas can fail this test if:

- [Too few stocks are assigned](https://support.worldquantbrain.com/hc/en-us/community/posts/8394917303575-Why-fundamental-alphas-always-show-Weight-is-too-strongly-concentrated-or-too-few-instruments-are-assigned-weight-) weight for significant number of days in a year. Note that assigning zero weights to all stocks at the start of the [simulation](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=definition.-,Simulation,-Simulation) does not fail this condition, it only applies after the Alpha starts assigning weights. The exact number of minimum stocks varies with the simulation [universe](https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions#:~:text=U-,Universe,-Universe).
- Alpha weight is too concentrated in any one stock. For example, if one stock has 30 percent of all Alpha weight, it will fail this test.

> [HEADING] {"level": "1", "content": "Sub universe"}

For TOPXXX universes:

subuniverse_sharpe >= 0.75 * sqrt(subuniverse_size / alpha_universe_size) * alpha_sharpe

For non TOPXXX universes:

subuniverse_sharpe >= subuniverse_ratio * alpha_sharpe

where subuniverse_ratio:

ASI MINVOL1M: 0.295

USA ILLIQUID_MINVOL1M: 0.41

EUR ILLIQUID_MINVOL1M: 0.355

> [HEADING] {"level": "1", "content": "Check-IS-Sharpe or IS-Ladder test"}

> [EQUATION] $$𝑆ℎ𝑎𝑟𝑝𝑒= \sqrt{250}*𝐼𝑅≈15.8*𝐼𝑅 $$

> [EQUATION] $$ 𝐼𝑅=\frac{𝑎𝑣𝑔(𝑑𝑎𝑖𝑙𝑦 𝑃𝑁𝐿)}{Std𝐷𝑒𝑣(𝑑𝑎𝑖𝑙𝑦 𝑃𝑁𝐿)}$$

The Check-IS-Sharpe or IS-Ladder test is an iterative test which compares the average IS performance to a series of benchmarks. Initially, the test examines only the most recent two years of IS data available. With each iteration, an additional year is added to the IS period. For example, in the fourth iteration, the IS ladder test examines the most recent 5 years of IS data and compares the Sharpe from that period to the benchmarks described below.

Alpha undergoes the IS Sharpe ladder test according to the following logic:

- if Sharpe for the whole history is less than FAIL_THRESHOLD, the test is failed
- Else:
- Start with N_YEARS = 2
- if Sharpe[N_YEARS] < FAIL_THRESHOLD: test FAILED
- else if Sharpe[N_YEARS] > PASS_THRESHOLDS[N_YEARS]: test PASSED
- else if (Sharpe[N_YEARS] > FAIL_THRESHOLD) and (Sharpe[N_YEARS ] < PASS_THRESHOLDS[N_YEARS]): N_YEARS += 1
- Go to step 4 with updated N_YEARS value.

**Thresholds for D1 and D0:**

> [TABLE] {"data": [["No. YEARS", "D1 THRESHOLD", "D0 THRESHOLD"], ["FAIL_THRESHOLD", "1.59", "2.69"], ["2 YEARS", "2.38", "3.96"], ["3 YEARS", "2.38", "3.96"], ["4 YEARS", "2.38", "3.96"], ["5 YEARS", "2.38", "3.96"], ["6 YEARS", "2.22", "3.64"], ["7 YEARS", "2.06", "3.33"], ["8 YEARS", "1.90", "3.17"], ["9 YEARS", "1.74", "2.85"], ["10 YEARS", "1.59", "2.69"]], "firstRowIsTableHeader": true, "firstColIsHeader": true}

**Note:**

- Sharpe[N_YEARS] is calculated for most recent N_YEARS years of the IS period
- If the turnover of Alpha is less than 30%, the IS Sharpe Ladder PASS_THRESHOLDS are multiplied by a factor of 0.85. FAIL_THRESHOLD is not multiplied by this factor.

> [HEADING] {"level": "1", "content": "Cluster Test"}

An Alpha is classified as a Cluster Alpha if its aggregated Cluster Sharpe exceeds the required threshold.

- **Threshold**: Cluster Sharpe >= **1.58**
- **Effect**: Classification only — submission conditions remains unchanged

> [HEADING] {"level": "2", "content": "Cluster Alphas"}

Cluster Alphas are evaluated using an additional Cluster Test. A cluster refers to a group of related instruments considered together. The Cluster Test evaluates the Alpha at the cluster level using a single aggregated cluster-level performance measure across these groups. These groups are intended to capture broader common structure in the market, such as related business activity, shared economic behavior, or similar market characteristics.

> [HEADING] {"level": "2", "content": "Classification Logic"}

- If Cluster Sharpe >= 1.58, the Alpha is tagged as **Cluster Alpha**
- Otherwise, the Alpha is not classified as a Cluster Alpha Failing the Cluster Test does not block submission and does not affect eligibility for other classifications. An Alpha may be classified as a Cluster Alpha and also receive other classifications at the same time.

> [HEADING] {"level": "2", "content": "Interpretation"}

The Cluster Test seeks to identify Alphas that exhibit broader and more consistent patterns across related groups of instruments. As a result, Cluster Alphas may be useful for theme-based research and for identifying signals with stronger group-level structure.

> [HEADING] {"level": "1", "content": "Interpreting Status Messages in Simulation Results"}

When “Check Submission” or “Submit Alpha” button is pressed, tests are performed in the order described below. In case Alpha fails any of the tests, respective Test Message is displayed.

> [TABLE] {"data": [["#", "TEST RESULT", "TEST MESSAGE"], ["1", "Alpha fails checkWeight test", "Maximum weight on an instrument is greater than 10% OR Weight is too strongly concentrated or too few instruments are assigned weight."], ["2", "Alpha fails CheckCorr test", "Reduce max correlation"], ["3", "Alpha fails 0.75*ISLadder", "Improve Sharpe"], ["4", "Alpha clears 0.75*ISLadder , fails 0.85*ISLadder and turnover > 10%", "Improve Sharpe or reduce turnover"], ["5", "Alpha clears 0.85*ISLadder, fails 1.0*ISLadder, turnover > 30% and correlation > 0.3", "Improve Sharpe or reduce turnover or reduce correlation"], ["6", "Alpha fails fitness test", "Improve fitness"], ["7", "Delay 0 Alpha fails checkDelay1Sharpe", "Alpha better suited for Delay 1"], ["8", "Alpha fails SubUniverse test", "Improve Sharpe in SubUniverse"], ["9", "Most illiquid 50% instruments after cost Sharpe of x is below cutoff of y (z original universe after cost Sharpe)", "Improve alpha performance in top illiquid quantile"]], "firstRowIsTableHeader": true, "firstColIsHeader": true}

- Example: if an Alpha clears criteria 1, 2 and 3 but fails at criteria 4, recommendation would be “Improve Sharpe or reduce turnover”.
