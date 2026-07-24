# Getting Started with vector_neut Operator

- 튜토리얼: `discover-brain` (Getting Started)
- 페이지 ID: `getting-started-vector_neut-operator`
- 최종수정: 2025-06-03T00:04:25.167537-04:00
- 분량: PT2M

---

**Getting Started with vector_neut Operator**

The **vector_neut** operator orthogonalizes one vector with respect to another. When vector x is "orthogonal" to vector y, they have zero dot product—they are perpendicular in vector space. The **vector_neut** operator transforms a vector x into a new vector x* such that x* is orthogonal to a specified vector y while preserving as much of the original information in x as possible.

**Important Mathematical Properties**

- **Orthogonality**: x* · y = 0 (zero dot product)

- **Variance decomposition**: Var(x) = Var(x*) + Var(projection)

- **Minimal norm**: x* has the minimum Euclidean distance to x among all vectors orthogonal to yWith these orthogonalization properties, **vector_neut** is useful for removing or controlling factor exposures in your Alpha, which can help reduce unwanted volatility and improve Alpha's Sharpe ratio

**Example Usage**

- Let's start with the most common risk factor – market beta.

- Step 1: Approximate market returns by taking the average of individual stock returns.market_returns = group_mean(returns, 1, market);

- Step 2a: Calculate Beta using time series regression between stock and market returns. The parameter rettype=2 specifies that we want the slope coefficient (β).beta = ts_regression(returns, market_returns, 252, rettype=2);

- Step 2b: An alternative Beta calculation using covariance and variance.beta = ts_covariance(returns, market_returns, 252) / power(ts_std_dev(market_returns, 252), 2);

- Step 3: Neutralize your Alpha against Beta.alpha* = vector_neut(alpha, beta);

- The resulting Alpha* is orthogonal to Beta, meaning dot(alpha*, beta) = 0, which implies the neutralized alpha has no linear relationship with market beta.
