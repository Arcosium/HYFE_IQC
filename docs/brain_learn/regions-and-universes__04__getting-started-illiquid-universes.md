# Getting started on Illiquid Universes [Gold]

- 튜토리얼: `regions-and-universes` (Regions and Universes)
- 페이지 ID: `getting-started-illiquid-universes`
- 최종수정: 2025-01-08T07:58:11.376704-05:00
- 분량: PT1M

---

> [HEADING] {"level": "2", "content": "Introductions"}

In this documentation, we will introduce a new Universe for Alpha on USA, EUR, ASI regions called ILLIQUID_MINVOL1M. It’s a unique universe consisting of illiquid equities, defined by our liquidity metrics: minimum volume of $1M.

The illiquid universe offers potential opportunities to capitalize on short term and long term price discrepancies due to its market inefficiency. However, it also implies higher trading costs, as short selling becomes more difficult and obtaining optimal order pricing is challenging due to slippage. For this reason, a new submission test for Alpha builds has been introduced in the ILLIQUID_MINVOL1M universe.

> [IMAGE] {"title": "getting-started-illiquid-universes.png", "width": 468, "height": 277, "fileSize": 29292, "url": "https://api.worldquantbrain.com/content/images/Zl8MNnxUpepDN6tandfC5X9fPYQ=/283/original/getting-started-illiquid-universes.png"}

> [HEADING] {"level": "2", "content": "New Submission test"}

Most Illiquid instruments after cost Sharpe test measures the proportion of after cost performance in an illiquid universe with reference to the original universe. This test ensures that the most illiquid half of the illiquid universe has a minimum required Sharpe after considering the various costs of trading these instruments
