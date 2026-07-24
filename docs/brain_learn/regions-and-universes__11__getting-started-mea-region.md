# Getting Started with the MEA Region

- 튜토리얼: `regions-and-universes` (Regions and Universes)
- 페이지 ID: `getting-started-mea-region`
- 최종수정: 2026-04-30T12:27:46.633059-04:00
- 분량: PT2M

---

> [HEADING] {"level": "1", "content": "Characteristics of the MEA Region"}

The MEA (Middle East and Africa) region groups selected Middle Eastern and North African markets into one research region. It offers access to a distinct set of country exposures that differ from other regions.

You can verify the included countries by simulating an Alpha with Visualization = ON and reviewing the country breakdown on BRAIN.

The main MEA equity universe, TOP300, contains approximately 300 stocks, making it a relatively small and capacity-sensitive region.

> [HEADING] {"level": "1", "content": "Tips for Success"}

- Focus on higher-turnover datasets that update frequently. A turnover of around 20% can be reasonable for this region. Data categories to start:Price Volume
- Analyst
- Higher update frequency datasets (check with BRAIN Labs)
- Liquidity and capacity are important in MEA because it is relatively small region. Make sure your Alpha’s margins are high enough to justify the trading costs from higher turnover. Check performance with:Max Trade = ON
- Max Pos = ON
- Avoid over-concentration in less liquid space, check Average Size By Capitalization by simulating an Alpha with Visualization = ON
- Use neutralization. A good first step is Market neutralization. You can also apply double neutralization such as sector + country.
- Diversity in dataset usage is important in MEA. Do not rely only on price-volume patterns. Where possible, combine them with other signals such as fundamental or analyst data to improve robustness.
- Test robustness across different time periods. In particular, compare behavior before and after 2018 using the test period feature.This can help you better understand what is driving your signal and build more robust MEA Alphas.
