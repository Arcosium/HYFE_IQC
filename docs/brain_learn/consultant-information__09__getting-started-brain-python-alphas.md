# Brain Python Alphas

- 튜토리얼: `consultant-information` (Consultant Information)
- 페이지 ID: `getting-started-brain-python-alphas`
- 최종수정: 2026-06-17T02:50:22.177780-04:00
- 분량: PT12M

---

# Getting Started with BRAIN Python Alphas

**Python Alpha** is a new feature that enables you to write Alphas in Python. By using Python’s rich open-source ecosystem, you can build Alphas that are more granular, flexible, and diverse than ever before.


You can access this feature through the Platform UI by [selecting Python as the language](#option-d-platform-uiapi-simulation), or conduct research in the [BrainLabs environment](https://platform.worldquantbrain.com/profile/account/brainlabs).



## Table of Contents



- [Setup](#setup)
[Handling integer fields](#handling-integer-fields)



- [The `@alpha` Decorator](#the-alpha-decorator)
[`data` — Declaring Input Fields](#data--declaring-input-fields)

- [`store` — Persisting State Across Time Steps](#store--persisting-state-across-time-steps)

- [Return Value](#return-value)

- [Rules Summary](#rules-summary)



- [Adjusting Prices for Corporate Actions](#adjusting-prices-for-corporate-actions)
[Example: stock split on 2014-12-22](#example-stock-split-on-2014-12-22)



- [Two Simulation Paths](#two-simulation-paths)
[Path 1: BrainLabs Simulation (fast, local)](#path-1-brainlabs-simulation-fast-local)

- [Path 2: Actual Simulation (accurate, remote)](#path-2-actual-simulation-accurate-remote)

- [When to Use Which](#when-to-use-which)



- [Simulating an Alpha](#simulating-an-alpha)
[Option A: Send the decorated function directly](#option-a-send-the-decorated-function-directly)

- [Option B: Read the code file and send it](#option-b-read-the-code-file-and-send-it)

- [Option C: Async simulation](#option-c-async-simulation)

- [Option D: Platform UI/API Simulation](#option-d-platform-uiapi-simulation)



- [SimulationSettings Reference](#simulationsettings-reference)

- [Complete Example](#complete-example)



## Setup

`from brain import Brain, BrainCache
from brain.models import SimulationSettings
from brain.alphas import alpha
import numpy as np
import numpy.typing as npt
`

`brain = Brain(raw=True)
cache = BrainCache(brain)
`

`Brain()` defaults to `instrument_type='EQUITY'`, `region='USA'`, `delay=1`, `universe='TOP3000'`. Pass `raw=True` to receive data in its native dtype (e.g., `float32`, `int32`) — the same types used in the actual simulation environment. Without `raw=True`, all data is converted to `float64`.


`BrainCache` wraps Brain and caches data field lookups — pass it to every simulation call to avoid redundant downloads. Create a new `BrainCache` if you change the simulation region, delay, or date range, since cached data is tied to those parameters. `BrainCache` is reserved for simulation only — to explore or inspect data, use `brain.get_data_frame()` directly:


`# Exploring data — use brain.get_data_frame(), NOT cache
df = brain.get_data_frame("returns")
print(df.shape)    # (n_dates, n_instruments)
print(df.dtypes)
`


### Handling integer fields

Some data fields are integer-typed (e.g., `int32`). Integer arrays cannot hold `NaN` — missing values are represented by the type's minimum value (`np.iinfo(np.int32).min`, i.e., `-2147483648`). To work with these fields, cast to `float32` and replace the sentinel with `NaN`:


`from brain import get_missing_value

data = brain.get_data_frame('pv96_acquisition_c_flag').values
print(data.dtype)   # int32

missing = get_missing_value(data.dtype)   # np.iinfo(np.int32).min
data_float = data.astype(np.float32)
data_float[data == missing] = np.nan
`



## The `@alpha` Decorator

Every Alpha is a Python function decorated with `@alpha`. The decorator declares which data fields to load and which store variables to persist across time steps.


`@alpha(
    data=["returns"],
    store=["my_state"],
)
def my_alpha(data, store) -> npt.NDArray[np.float32]:
    # data.returns  — shape [lookback_window, n_instruments], float32
    # data.universe — shape [lookback_window, n_instruments] (always available, int: 1 = in, 0 = out)
    #
    # store.my_state — initialized to None, persists across time steps

    signal = -np.nanmean(data.returns, axis=0)
    return signal.astype(np.float32)
`


### `data` — Declaring Input Fields

The `data` parameter is a list of data field names. Each name becomes an attribute on the `data` namespace as a 2-D numpy array shaped `[lookback_window, n_instruments]`:


`@alpha(data=["returns", "close"], store=[])
def my_alpha(data, store) -> npt.NDArray[np.float32]:
    # data.returns           — [lookback_window, n_instruments], float32
    # data.close  — [lookback_window, n_instruments], float32
    # data.universe          — [lookback_window, n_instruments] (always available, do NOT list it)
    ...
`

The simulation engine calls your Alpha once per time step. At each step, each data field contains `lookback + 1` rows of history (set via `SimulationSettings.lookback`). A `lookback` of 0 gives 1 row (today only), a `lookback` of 5 gives 6 rows. The most recent data is the last row:


`yesterday = data.returns[-1]          # shape: [n_instruments]
two_days_ago = data.returns[-2]       # shape: [n_instruments]
full_window = data.returns            # shape: [lookback_window, n_instruments]
`

During the first few time steps the window is still growing (fewer than `lookback + 1` rows). Once the window reaches full size it slides forward — one new row enters at the bottom, the oldest drops off the top.


The special field `universe` is always available as an integer array (1 = in-universe, 0 = out). Do not include "universe" in your data list.


**Data arrays are read-only.** The engine marks them as non-writable — attempting to modify them in-place will raise a `ValueError`. Always `.copy()` before modifying:


`# WRONG — raises ValueError: assignment destination is read-only
a = data.returns[-1]
a[a < 0] = 0

# RIGHT — copy first
a = data.returns[-1].copy()
a[a < 0] = 0
`


### `store` — Persisting State Across Time Steps

The `store` parameter is a list of store declarations. Each is either a plain **string** (untyped, pre-initialized to `None`) or a **typed dict** that declares the variable's shape and how it grows with the universe:


`{"name": "my_arr", "dims": "i", "extend": np.float64(0)}
`



- "name": variable name (required)

- "dims": string of axis characters — "i" = instruments axis (auto-extended when universe grows), "x" = free axis. "xi" → 2D `[any, n_instruments]`.

- "extend": fill value for new instruments — **must match the array's dtype exactly**. Always specify it explicitly, even when the default may suffice — it makes intent clear and avoids surprises. Use explicit NumPy scalar constructors: `np.float64(0)`, `np.float32(0)`, `np.float64(np.nan)` for float64, `np.float32(np.nan)` for float32. Never use bare `np.nan` — it is Python's `float`, not `numpy.float64`, and will fail the type check. Never use bare Python literals like `0` or `0.0` — they may not match the array's dtype and will raise a `ValueError`:
  `Store item 'name' extend value '0' has wrong type: <class 'int'> expected: <class 'numpy.float32'>`
  If omitted, the fill is type-determined (NaN for float arrays, the type's minimum value for integers), but omitting is discouraged.

Use `store.var is None` to detect the first call. For typed entries the simulator auto-extends "i" axes when the universe grows — no manual padding needed.


`@alpha(data=["returns"], store=[{"name": "running_mean", "dims": "i", "extend": np.float64(0)}])
def smooth_alpha(data, store) -> npt.NDArray[np.float32]:
    if store.running_mean is None:
        store.running_mean = np.zeros(data.returns.shape[1], dtype=np.float64)

    raw = -np.nanmean(data.returns, axis=0)   # float64
    store.running_mean = 0.9 * store.running_mean + 0.1 * raw
    return store.running_mean.astype(np.float32)
`

Use "dims": "xi" for a 2D cache where axis 0 is a free (time) axis and axis 1 is the instruments axis. A practical use case is caching cross-sectional ranks across more days than the `lookback` window provides. Use `"extend": np.float64(np.nan)` so historical entries for newly added instruments are NaN — `np.nanmean` then naturally excludes them until the instrument accumulates history:


`RANK_DAYS = 20

@alpha(
    data=["returns"],
    store=[
        {"name": "rank_cache", "dims": "xi", "extend": np.float64(np.nan)},  # [n_days_cached, n_instruments]
    ],
)
def mean_of_rank_alpha(data, store) -> npt.NDArray[np.float32]:
    today = data.returns[-1]

    # Cross-sectional rank of today's return
    finite = np.where(np.isnan(today), -np.inf, today)
    today_rank = np.argsort(np.argsort(finite)).astype(np.float64)
    today_rank[np.isnan(today)] = np.nan

    if store.rank_cache is None:
        store.rank_cache = today_rank[np.newaxis, :]  # shape: [1, n_instruments]
    else:
        new_cache = np.vstack([store.rank_cache, today_rank[np.newaxis, :]])
        store.rank_cache = new_cache[-RANK_DAYS:]

    mean_rank = np.nanmean(store.rank_cache, axis=0)
    return (-mean_rank).astype(np.float32)
`

When the universe grows, the simulator appends a new NaN column to `rank_cache` — no manual padding needed.


Valid values for untyped string entries:
- `None`, scalars (`int`, `float`, `bool`, `str`), NumPy scalars
- `numpy.ndarray`
- Lists and dicts containing any of the above, including nested arrays, lists, and dicts
- Dict keys must be strings


**Fallback — `_pad_store` for untyped entries**: if you use a plain string entry that stores an instrument-sized array, extend it manually before reading otherwise it will throw error in mid-simulation:


`def _pad_store(arr, n_insts, fill_value):
    """Pad along the instruments axis when the universe grows."""
    if arr.shape[-1] >= n_insts:
        return arr
    n_new = n_insts - arr.shape[-1]
    if arr.ndim == 1:
        return np.pad(arr, (0, n_new), mode='constant', constant_values=fill_value)
    return np.pad(arr, ((0, 0), (0, n_new)), mode='constant', constant_values=fill_value)

n_insts = data.returns.shape[1]
store.running_sum = _pad_store(store.running_sum, n_insts, fill_value=0.0)
`

But even 2D cases like a correlation matrix (diagonal=1, off-diagonal=0) work with typed dicts: use `"dims": "ii"` with `"extend": np.float64(0)`. The simulator fills new rows and columns with 0; fix the diagonal for newly added instruments in the Alpha body using a plain string `"prev_n"` to track the previous universe size.



### Return Value

The Alpha must return a 1-D `float32` numpy array of shape `[n_instruments]`. Data fields from BRAIN are typically `float32`, but many NumPy operations silently promote to `float64` — `np.nanmean`, `np.nanstd`, `np.nansum`, and even arithmetic with Python floats like `0.9 * array`. Always add `.astype(np.float32)` at the return as a safety measure (it is a no-op if the array is already `float32`):


`def my_alpha(data, store) -> npt.NDArray[np.float32]:
    # np.nanmean promotes float32 input to float64 output
    signal = -np.nanmean(data.returns, axis=0)
    return signal.astype(np.float32)
`

If the return value is not `float32`, the simulation will raise: `Alpha vector is not float32`.



### Rules Summary



- Exactly one `@alpha` decorator per function

- Function must accept exactly 2 parameters (`data`, `store`)

- Return type must be `float32` numpy array of shape `[n_instruments]`

- Do not include `"universe"` in the `data` list — it is always available

- Do not mutate `data` arrays in-place — they are read-only, copy first



## Adjusting Prices for Corporate Actions

Any per-share field contains discontinuities from corporate actions such as stock splits. This includes prices (`close`, `open`, `high`, `low`) as well as per-share fundamentals like `eps`, `book_value_ps`, and similar. For example, a 1:5 reverse split causes the reported price to jump overnight — not a real market move. If you use these fields directly in Alpha logic, the jumps will produce false signals.


Brain provides an `adjfactor` field that records cumulative adjustment factors for each instrument. Apply it to convert raw prices into a continuous, split-adjusted series:


`import matplotlib.pyplot as plt

close = brain.get_data_frame('close')
adjfactor = brain.get_data_frame('adjfactor')

# Compute split-adjusted close prices
adjusted_close = close / ((adjfactor - 1.0).cumsum() + 1.0)
`


### Example: stock split on 2014-12-22

Instrument `EQ0000000000041095` had a 1:5 reverse split on 2014-12-22. Without adjustment, the price appears to jump from ~0.5 to ~3.5:


`instrument = 'EQ0000000000041095'
date_range = slice('2014-12-01', '2015-01-31')

fig, axes = plt.subplots(1, 2, figsize=(14, 5))

close.loc[date_range, instrument].plot(ax=axes[0], title='Raw close price')
axes[0].set_ylabel('Price')

adjusted_close.loc[date_range, instrument].plot(ax=axes[1], title='Split-adjusted close price')
axes[1].set_ylabel('Price')

plt.tight_layout()
plt.show()
`

The raw chart shows a sharp discontinuity at the split date. The adjusted chart shows a smooth, continuous price series — which is what your Alpha should operate on.



**Tip**: The `returns` field is already split-adjusted. This adjustment step is needed for any per-share field — prices (`close`, `open`, `high`, `low`), per-share fundamentals (`eps`, `book_value_ps`), and similar.





## Two Simulation Paths


### Path 1: BrainLabs Simulation (fast, local)

BrainLabs simulation runs your Alpha function locally on cached data. It executes row-by-row through a three-step pipeline. This is fast and ideal for development, debugging, and parameter sweeps — but the PnL may differ slightly from the actual simulation environment.


`simulation_settings = SimulationSettings(
    instrument_type='EQUITY',
    region='USA',
    delay=1,
    universe='TOP3000',
    lookback=5,
    visualization=True,
)

# Step 1: Generate alpha matrix (calls your alpha function row-by-row)
alpha_matrix = brain.generate_alpha_matrix(mean_reversion, simulation_settings, cache)

# Step 2: Generate positions (applies delay, pasteurization, neutralization, scaling)
positions = brain.generate_alpha_positions(alpha_matrix, simulation_settings, cache)

# Step 3: Generate stats (computes daily PnL, prints Sharpe/returns/drawdown/turnover)
stats = brain.generate_alpha_stats(positions, simulation_settings, cache)
`


### Path 2: Actual Simulation (accurate, remote)

Actual simulation submits your Alpha to the BRAIN backend. This produces accurate results but is slower (remote API call with polling). The backend applies the full pipeline including decay, neutralization, pasteurization, truncation, and position/per-step position change limits.


`simulation_settings = SimulationSettings(
    instrument_type='EQUITY',
    region='USA',
    delay=1,
    universe='TOP3000',
    lookback=5,
    decay=10,
    neutralization='MARKET',
    pasteurization='ON',
    truncation=0.8,
    language='PYTHON',
    visualization=False,
    max_position='OFF',
    max_trade='OFF',
)

# Submit the @alpha-decorated function
alpha_id = brain.simulate(mean_reversion, simulation_settings)
print(alpha_id)

# Retrieve the result
alpha_result = brain.get_alpha(alpha_id)
print(alpha_result)
`


### When to Use Which




| 
 | BrainLabs Simulation | Actual Simulation
 |



| 
**Speed** | Fast (local, cached data) | Slower (remote API)
 |

| 
**Accuracy** | Approximate | Accurate
 |

| 
**Use for** | Development, debugging, parameter sweeps | Final validation, submission
 |

| 
**Pipeline** | `generate_alpha_matrix` -> `generate_alpha_positions` -> `generate_alpha_stats` | `brain.simulate(alpha_fn, settings)`
 |


**Recommended workflow**: Develop and iterate using BrainLabs simulation (fast feedback), then validate your best Alpha with actual simulation before submission.




## Simulating an Alpha


### Option A: Send the decorated function directly

Pass your `@alpha`-decorated function to `brain.simulate()`:


`alpha_id = brain.simulate(mean_reversion, simulation_settings)
`

The function source code is extracted automatically and sent to the backend. All helper functions and imports used inside the Alpha must be visible in the same notebook cell.



### Option B: Read the code file and send it

Save your Alpha and all its dependencies into a single `.py` file, then read and submit it as a string:


`# alpha_example.py
from brain.alphas import alpha
import numpy as np
import numpy.typing as npt


def pasteurize(a: npt.NDArray[np.float32], u: npt.NDArray) -> npt.NDArray[np.float32]:
    # return pasteurized position, removing out-of-universe position

def neutralize_scale(a: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    # return neutralized and scaled position

@alpha(
    data=["returns"],
    store=[],
)
def mean_reversion(data, store) -> npt.NDArray[np.float32]:
    a = -np.nanmean(data.returns, axis=0).astype(np.float32)
    a = pasteurize(a, data.universe[-1])
    a = neutralize_scale(a)
    return a.astype(np.float32)
`

Then submit from your notebook:


`brain = Brain(raw=True)

simulation_settings = SimulationSettings(
    instrument_type='EQUITY', region='USA', delay=1,
    universe='TOP3000', lookback=5, decay=10,
    neutralization='MARKET', pasteurization='ON',
    truncation=0.8, language='PYTHON',
    visualization=False, max_position='OFF', max_trade='OFF',
)

with open('./alpha_example.py', 'r') as f:
    code = f.read()

alpha_id = brain.simulate(code, simulation_settings)
print(alpha_id)
`

This is useful when your Alpha has helper functions or operators that need to travel with the submission as a single unit.



### Option C: Async simulation

For non-blocking submission — useful when running multiple simulations concurrently or from an async context — use the async API:


`import asyncio

simulation_id = await brain.simulate_async(code, simulation_settings)
print('started', simulation_id)
simulation_response, retry_after, alpha_id = await brain.get_simulation_async(simulation_id)
while retry_after:
    print('sleeping', retry_after)
    await asyncio.sleep(retry_after)
    simulation_response, retry_after, alpha_id = await brain.get_simulation_async(simulation_id)

print('completed', simulation_id, alpha_id, simulation_response)
`

`simulate_async` accepts either a `@alpha`-decorated function or a code string, same as `brain.simulate`. `get_simulation_async` returns a `(response, retry_after, alpha_id)` tuple — `retry_after` is non-zero while the simulation is still running, and `None` once complete.



### Option D: Platform UI/API Simulation

Python Alpha can be simulated and submitted on BRAIN just like Fast Expression Alpha. 
Simply open the LANGUAGE dropdown in Settings, select Python, and enter your Python code in the code editor below. You may notice that the parameters in Settings are slightly different from those used for Fast Expression.



![이미지](https://api.worldquantbrain.com/content/images/9GuOQvRQ-G87pkYQd_mPHoMmu1U=/460/original/python_platform_simulaiton.png)


Once the Python code is ready, simply click the Simulate button to run the simulation. You will see a progress bar during execution, and the results will be displayed when the simulation is complete. 
If you encounter a failure message, consider first running your Python Alpha through the Path 1 BrainLabs environment to further debug your code.


Remember not to import packages that are not used directly in the Alpha function. For example, you should remove `from brain import Brain, BrainCache` and `from brain.models import SimulationSettings` when simulating through UI.




## SimulationSettings Reference




| 
Parameter | Description | Typical Values
 |



| 
`instrument_type` | Market type | `'EQUITY'`
 |

| 
`region` | Simulation region | `'USA'`, `'EUR'`, `'ASI'`
 |

| 
`delay` | Delay | `0`, `1`
 |

| 
`universe` | Instrument universe | `'TOP3000'`, `'TOP2000'`, `'TOP500'`
 |

| 
`lookback` | Extra history rows (window = lookback + 1) | `0`, `5`, `21`, `63`, etc.
 |

| 
`start_date` | Simulation start date (BrainLabs sim) | `'2020-01-01'`
 |

| 
`end_date` | Simulation end date (BrainLabs sim) | `'2021-12-31'`
 |

| 
`neutralization` | Position neutralization | `'NONE'`, `'MARKET'`, `'SECTOR'`
 |

| 
`truncation` | Position limit factor | `0.01`, `0.05`
 |

| 
`decay` | Alpha decay parameter | `0`, `3`, `6`, `10`
 |

| 
`pasteurization` | Mask to universe | `'ON'`, `'OFF'`
 |

| 
`language` | Simulation language (actual sim) | `'PYTHON'`
 |

| 
`visualization` | Show PnL chart (BrainLabs sim) | `True`, `False`
 |

| 
`max_position` | Limit position sizes (actual sim) | `'ON'`, `'OFF'`
 |

| 
`max_trade` | Limit per-step position changes (actual sim) | `'ON'`, `'OFF'`
 |




## Complete Example

The example below is meant to be run in a notebook in **BrainLabs**. Each fenced
block is a separate cell — run them from top-to-bottom.



### BrainLabs simulation (fast iteration)

`from brain import Brain, BrainCache
from brain.models import SimulationSettings
from brain.alphas import alpha
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
    return a.astype(np.float32)


brain = Brain(raw=True)
cache = BrainCache(brain)

settings = SimulationSettings(
    instrument_type='EQUITY', region='USA', delay=1,
    universe='TOP3000', lookback=5, visualization=True,
)

alpha_matrix = brain.generate_alpha_matrix(mean_reversion, settings, cache)
positions = brain.generate_alpha_positions(alpha_matrix, settings, cache)
stats = brain.generate_alpha_stats(positions, settings, cache)
`


### Actual simulation (final validation + submission)


**Note**: `brain.simulate(...)` reads the **entire source cell** that defines
the Alpha function and ships it to the simulation server. Only importing alpha module
from brain.alphas is allowed in the **same cell** as
the `@alpha`-decorated function. Importing other brain modules such as `from brain import Brain, BrainCache` 
or `from brain.models import SimulationSettings`, the simulation will fail. Keep those
imports out of the Alpha cell. The same import restriction applies when simulating Python Alpha
from BRAIN.



**Cell 1 — Alpha definition** (only `from brain.alphas import alpha` is put here)


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
    return a.astype(np.float32)
`

**Cell 2 — submit and fetch the result** (`Brain` / `SimulationSettings` imports go here)


`from brain import Brain
from brain.models import SimulationSettings

brain = Brain(raw=True)

submit_settings = SimulationSettings(
    instrument_type='EQUITY', region='USA', delay=1,
    universe='TOP3000', lookback=5, decay=10,
    neutralization='MARKET', pasteurization='ON',
    truncation=0.8, language='PYTHON',
    visualization=False, max_position='OFF', max_trade='OFF',
)

alpha_id = brain.simulate(mean_reversion, submit_settings)
alpha_result = brain.get_alpha(alpha_id)
print(alpha_result)
`
