# How to translate Fast Expression Alpha into Python Alpha

- 튜토리얼: `consultant-information` (Consultant Information)
- 페이지 ID: `how-translate-fast-expression-alpha-python-alpha`
- 최종수정: 2026-06-19T23:48:43.726944-04:00
- 분량: PT12M

---

Translating an existing Fast Expression Alpha into Python is a great way to write your first Python Alpha. It is not only easier than starting from scratch, but also lets you directly compare performance between the Python version and the original — giving you a concrete baseline to build on as you think about ways to improve it by leveraging the full flexibility of the Python language.



This article has two parts:





- How to prompt an LLM to translate a Fast Expression Alpha into a Python Alpha

- Tips and techniques for making Python Alphas better than the underlying expression





## Part 1 — Prompting an LLM to Translate a Fast Expression Alpha


We encourage to prompt the LLM with at least following five sections for converting fast expression into python properly





- Original Fast Expression

- Operator description

- Data field description

- Python Alpha syntax

- Simple example



### 1.1 — The Original Fast Expression


For example, your original Alpha expression:



`ts_rank(rank(returns))`


### 1.2 — Description of the Operators Used


You can find descriptions of all operators on the [operator page](https://platform.worldquantbrain.com/learn/operators).



**ts_rank** — The `ts_rank` operator evaluates how the current value of a variable compares to its values over a defined lookback period (d days) for each instrument. It returns a normalized rank (between 0 and 1) of the current value within that window, optionally shifted by a constant. This is helpful for identifying trends, momentum, or reversals in time-series data.



**rank** — The `rank(x)` operator assigns a rank to each value in the input `x` across all instruments for a given date, mapping the lowest value to 0.0 and the highest to 1.0, with all other values evenly distributed in between. This helps normalize data, limit extreme values, and can improve the stability of your Alpha by reducing outliers and drawdown. The optional `rate` parameter controls the precision of sorting (default is 2; set to 0 for exact sorting).




### 1.3 — Description of the Data Fields Used


You can find data field descriptions on the data explorer page.



**returns** — Daily returns




### 1.4 — The Python Alpha Syntax



  **The `@alpha` contract**



  Every Python Alpha is one self-contained module. It cannot import your local
  files, so every helper function must be contained in the file itself.



`from brain.alphas import alpha
import numpy as np
import numpy.typing as npt

@alpha(
    data=["field_a", "field_b"],   # MATRIX fields to load; do NOT list "universe"
    store=[],                       # state persisted across days (often empty)
)
def my_alpha(data, store) -> npt.NDArray[np.float32]:
    ...
    return signal.astype(np.float32)`

  **Rules that bite:**


  

- The engine calls the function **once per time step**. Each `data.<field>` is a
      2-D array `[rows, n_instruments]`, dtype **float32**, where `rows = lookback+1`
      when the window is full and **today is the last row** (`field[-1]`).

- During warm-up the window has fewer rows (can be 1). Reductions over `[-d:]`
      are safe; explicit back-indexing (`x[-1-d]`) must guard `x.shape[0]`.

- `data.universe` is always present (int 1/0). **Never** put `"universe"` in
      `data`. Use `data.universe[-1]` for today's in-universe mask.

- **Input data field arrays are read-only.** `.copy()` before mutating, or you get
      *"assignment destination is read-only."*

- **Return a 1-D float32 array** of shape `(n_instruments,)`. numpy silently
      promotes to float64 (`np.nanmean`, multiplying by a Python float, …), so
      **always** finish with `.astype(np.float32)` — otherwise *"Alpha vector is
      not float32."*

- Only **MATRIX** fields load. VECTOR fields, the GLB region, and multi-sim are
      not supported.

- Set the simulation `lookback` ≥ the largest `ts_*` window used. Sparse
      fundamentals want `lookback ≈ 250` so backfill has room.
  

  **`store` — state across days.** Most ports need none. Use it only for
  path-dependent operators (`trade_when`, `keep`, `hump`,
  `days_from_last_change`, running means, buffers). A typed entry
  auto-extends along the instrument axis as the universe grows:



`store=[{"name": "buf", "dims": "i", "extend": np.float64(0)}]   # "i"=1-D vector, "xi"=2-D [any, n_insts]`

  Detect the first call with `store.<name> is None`.



  **Full `store` + `extend` example — rolling window buffer with `"dims": "xi"`**



  This example keeps a rolling 10-day window of raw returns per instrument in a
  2-D store `[window, n_instruments]` and outputs the mean-reversion of that
  window. `"dims": "xi"` means the **x** axis is free (the 10-day window) and
  the **i** axis is along instruments — the simulator auto-extends new instrument
  columns with the `extend` fill value.



`from brain.alphas import alpha
import numpy as np
import numpy.typing as npt

WINDOW = 10

@alpha(
    data=["returns"],
    store=[{"name": "buf", "dims": "xi", "extend": np.float64(0)}],
)
def rolling_reversion(data, store) -> npt.NDArray[np.float32]:
    raw = -np.nanmean(data.returns, axis=0)          # float64, shape [n_instruments]

    if store.buf is None:
        # First call: initialise buffer with this single row
        store.buf = raw[np.newaxis, :]               # shape [1, n_instruments]
    else:
        # Append today's row and keep only the last WINDOW rows
        store.buf = np.vstack([store.buf, raw])[-WINDOW:]   # shape [≤WINDOW, n_instruments]

    signal = np.nanmean(store.buf, axis=0)           # float64, shape [n_instruments]
    return signal.astype(np.float32)`

  Key points:


  

- `"dims": "xi"` — the **x** axis (rows = window length) is free/fixed by your
      logic; the **i** axis is instruments. When a new instrument enters the
      universe the simulator appends a new **column** filled with `extend` (`0.0`).

- `"dims": "i"` would be for a plain 1-D vector; use `"xi"` whenever you need
      a 2-D buffer shaped `[any_rows, n_instruments]`.

- `"extend": np.float64(0)` must match the array's dtype exactly — `np.float32(0)`
      for float32 stores, `np.int64(0)` for int64, etc.

- `store.buf is None` is **only true on the very first call**; after that it is
      always a numpy array.

- Assign back to `store.buf` every step — the simulator persists whatever you
      leave in `store` between calls.
  



### 1.5 — A Python Alpha Example


`from brain.alphas import alpha
import numpy as np
import numpy.typing as npt


def pasteurize(a, u):
    a = a.copy()
    a[~u.astype(bool)] = np.nan
    return a

def neutralize(a):
    a0 = np.nan_to_num(a, nan=0, posinf=0, neginf=0)
    return a - np.mean(a0)

def scale(a):
    a0 = np.nan_to_num(a, nan=0, posinf=0, neginf=0)
    norm = np.linalg.norm(a0, ord=1)
    return a / norm if norm > 0 else a


@alpha(
    data=["returns"],
    store=[],
)
def mean_reversion(data, store) -> npt.NDArray[np.float32]:
    a = -np.nanmean(data.returns, axis=0).astype(np.float32)
    a = pasteurize(a, data.universe[-1])
    a = scale(neutralize(a))
    return a.astype(np.float32)`

You can also, for easier reuse, turn the data fields and operator descriptions into a lookup via MCP using the BRAIN API, and then save the entire prompt construction as a skill. That way, next time the LLM agent can directly use that skill to convert a Fast Expression Alpha into a Python Alpha.



For tracking the record, please tag translated Alphas with original fast expression Alpha ID. There is an ID column in [Alphas page](https://platform.worldquantbrain.com/alphas/submitted).






## Part 2 — Tips, Tricks, and Techniques for Better Python Alphas




- **Better data preprocessing** — clean and normalize inputs more carefully than the Fast Expression engine does by default.

- **Use machine learning algorithms** — capture relationships between instruments reflected in the data itself. For example, use various clustering algorithms to create customized groupings to enhance Alpha performance.

- **Use advanced mathematical models** — apply techniques like Fourier Transformation to find the essential signal driving Alpha performance and decouple it from noise.

- **Use heuristic optimization** — apply methods such as ant-colony optimization (ACO) to search for better Alpha parameters in a time-efficient way.



  **Research note:** While Python Alphas make it easy to increase model complexity, the fundamental principles of Alpha research still apply. Keep the core idea simple, avoid overfitting, and do not add complexity unless it delivers a meaningful improvement. If a technique as involved as deep learning only moves Sharpe from 2.0 to 2.05, the marginal gain does not justify the added overfitting risk — and the Alpha is likely worse off for it.
