# Crowding Risk-Neutralized Alphas

- 튜토리얼: `advanced-topics` (Getting Started)
- 페이지 ID: `getting-started-crowding-risk-neutralized-alphas`
- 최종수정: 2025-11-04T02:32:58.440036-05:00
- 분량: PT2M

---

> [HEADING] {"level": "2", "content": "What is Crowding Risk?"}

Crowding risk occurs when many investors hold similar positions and trades. This similarity can create crowded positions and trades.

The main problems with crowded trades include:

- Unraveling of Crowded Positions: When too many investors try to sell the same positions simultaneously, prices can drop quickly, increasing losses.
- Reduced Profitability: Crowded trades can become less profitable as more investors enter the same positions. Initially, prices may rise, but they may become vulnerable to steep declines.

> [HEADING] {"level": "2", "content": "Crowding Risk Factors"}

For [long-short neutralized Alphas](https://platform.worldquantbrain.com/learn/documentation/advanced-topics/getting-started-risk-neutralized-alphas), several style risk factors can indicate crowding risk. For example, the momentum factor identifies high exposure to instruments with favorable medium-term price movements. Instruments with strong momentum can attract many investors, potentially leading to crowded trades.

> [HEADING] {"level": "2", "content": "How to Simulate Crowding Risk-Neutralized Alphas"}

To control for crowding risk in Alphas, BRAIN has developed a risk model with various risk factors. By monitoring and managing these factors, BRAIN consultants can potentially reduce the negative impacts of crowded trades and improve the robustness of Alphas. Consultants can neutralize Alphas using crowding risk factors by adjusting the settings to 'CROWDING':

> [IMAGE] {"title": "crowding_risk_neu.png", "width": 624, "height": 362, "fileSize": 50882, "url": "https://api.worldquantbrain.com/content/images/cPHU6I2_nsgdPuZATjrV1FBSSmc=/318/original/crowding_risk_neu.png"}

settings_dict = {
 "instrumentType": "EQUITY",
 "region": "USA",
 "universe": "TOP3000",
 "delay": 1,
 "decay": 0,
 "neutralization": "CROWDING",
 "truncation": 0.1,
 "pasteurization": "ON",
 "unitHandling": "VERIFY",
 "nanHandling": "ON",
 "language": "FASTEXPR",
 "visualization": False
}
