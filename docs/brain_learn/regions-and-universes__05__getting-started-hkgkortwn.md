# Getting started on HKG/KOR/TWN [Grand Master]

- 튜토리얼: `regions-and-universes` (Regions and Universes)
- 페이지 ID: `getting-started-hkgkortwn`
- 최종수정: 2026-02-13T09:57:25.680648-05:00
- 분량: PT1M

---

- In Korea and Taiwan regions, “(-1) * Alpha” is not the opposite of the alpha due to the way we a trade is simulated and the more common presence of circuit breakers in these regions.
- Due to the generally high trading cost, it is better to have higher margin that is at least 10 bps for Taiwan region.
- In Korea and Hong Kong regions, it is better to control the turnover to be less than 30%. If your alphas do have higher turnover, then the alphas should have higher Sharpe and Returns to cover those higher trading costs.
- You can try alpha ideas that work well in ASI. In addition to your ideas that work in ASI, you can also try other specific ideas or specific datasets which are only available or have higher coverage in a certain country.
- When simulating or submitting Alphas in HKG, KOR or TWN regions, you have to turn on the 'Max Trade' option.
- To get started, besides price volume, we recommend to try out some of the below specific datasets:HKG: [model25](https://platform.worldquantbrain.com/data/data-sets/model25?delay=1&instrumentType=EQUITY&limit=20&offset=0&region=HKG&universe=TOP800), [other85](https://platform.worldquantbrain.com/data/data-sets/other85?delay=1&instrumentType=EQUITY&limit=20&offset=0&region=HKG&universe=TOP800)
- KOR: [analyst25](https://platform.worldquantbrain.com/data/data-sets/analyst25?delay=1&instrumentType=EQUITY&limit=20&offset=0&region=KOR&universe=TOP600)
