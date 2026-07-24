# How can you avoid duplicate simulations?

- 튜토리얼: `brain-api` (Consultant Information)
- 페이지 ID: `how-can-you-avoid-duplicate-simulations`
- 최종수정: 2026-01-21T01:03:07.613232-05:00
- 분량: PT4M

---

When you simulate Alphas automatically or with an LLM, you can easily end up simulating the same Alpha more than once without noticing. A simulation hash cache can help you detect these exact duplicates and reuse the existing alpha_id instead of running the simulation again, so you do not waste your simulation quota on repetitive simulations.

In practice, it is a simple table (for example, a parquet file) with columns such as alpha_hashed, alpha_id, and date_created, where each row represents a previously simulated Alpha. An Alpha configuration dictionary is the JSON-like object you send to the /simulations endpoint, containing the simulation type, settings, and expression, for example:

`simulation_data = {
    "type": "REGULAR",
    "settings": {
        "instrumentType": "EQUITY",
        "region": "USA",
        "universe": "TOP3000",
        "delay": 1,
        "decay": 15,
        "neutralization": "SUBINDUSTRY",
        "truncation": 0.08,
        "pasteurization": "ON",
        "testPeriod": "P1Y6M",
        "unitHandling": "VERIFY",
        "nanHandling": "OFF",
        "language": "FASTEXPR",
        "visualization": False,
    },
    "regular": "close",
}`

You can avoid duplicates by adding a pre-check step before each simulation:

- Build the Alpha configuration dictionary (alpha_dict) you plan to send to the API.
- Compute a hash of this dictionary.
- Look up this hash in your local cache:If found -> retrieve the alpha_id and skip the simulation
- If not found -> run the simulation, then add the new hash + alpha_id to the cache.
- This way your machine won’t send an identical Alpha twic

> [HEADING] {"level": "1", "content": "Potential steps to implement a local hash-based simulation cache"}

Below is a minimal Python example.

> [HEADING] {"level": "2", "content": "Hash your Alpha configuration"}

`from datetime import datetime, timezone

def add_to_cache(alpha_dict: dict, alpha_id: str):
    # Ensure cache exists
    if not os.path.exists(CACHE_PATH):
        create_simulation_cache()

    alpha_hashed = hash_alpha(alpha_dict)

    # Load existing cache
    t = pd.read_parquet(CACHE_PATH)

    # Append new record
    new_row = {
        "alpha_hashed": alpha_hashed,
        "alpha_id": alpha_id,
        "date_created": datetime.now(timezone.utc),
    }

    t = pd.concat([t, pd.DataFrame([new_row])], ignore_index=True)
    t.to_parquet(CACHE_PATH, index=False)`

import hashlib
import json

def hash_alpha(alpha_dict: dict) -> str:
 alpha_str = json.dumps(alpha_dict, sort_keys=True)
 return hashlib.sha256(alpha_str.encode("utf-8")).hexdigest()

`import hashlib
import json

def hash_alpha(alpha_dict: dict) -> str:
    alpha_str = json.dumps(alpha_dict, sort_keys=True)
    return hashlib.sha256(alpha_str.encode("utf-8")).hexdigest()`

> [HEADING] {"level": "2", "content": "Set up cache file"}

import os
import pandas as pd

CACHE_PATH = "simulation_cache.parquet"

def create_simulation_cache():
 if not os.path.exists(CACHE_PATH):
 df = pd.DataFrame(columns=["alpha_hashed", "alpha_id", "date_created"])
 df.to_parquet(CACHE_PATH, index=False)

`import os
import pandas as pd

CACHE_PATH = "simulation_cache.parquet"

def create_simulation_cache():
    if not os.path.exists(CACHE_PATH):
        df = pd.DataFrame(columns=["alpha_hashed", "alpha_id", "date_created"])
        df.to_parquet(CACHE_PATH, index=False)`

> [HEADING] {"level": "2", "content": "Add simulation to the cache"}

`from datetime import datetime, timezone

def add_to_cache(alpha_dict: dict, alpha_id: str):
    # Ensure cache exists
    if not os.path.exists(CACHE_PATH):
        create_simulation_cache()

    alpha_hashed = hash_alpha(alpha_dict)

    # Load existing cache
    t = pd.read_parquet(CACHE_PATH)

    # Append new record
    new_row = {
        "alpha_hashed": alpha_hashed,
        "alpha_id": alpha_id,
        "date_created": datetime.now(timezone.utc),
    }

    t = pd.concat([t, pd.DataFrame([new_row])], ignore_index=True)
    t.to_parquet(CACHE_PATH, index=False)`

> [HEADING] {"level": "2", "content": "Check if an Alpha was already simulated"}

from typing import Optional

def check_if_alpha_already_simulated(alpha_dict: dict) -> Optional[str]:
 if not os.path.exists(CACHE_PATH):
 return None

alpha_hashed = hash_alpha(alpha_dict)
 t = pd.read_parquet(CACHE_PATH)

matches = t.loc[t["alpha_hashed"] == alpha_hashed]
 if len(matches) == 0:
 return None

return matches.iloc[0]["alpha_id"]

`from typing import Optional

def check_if_alpha_already_simulated(alpha_dict: dict) -> Optional[str]:
    if not os.path.exists(CACHE_PATH):
        return None

    alpha_hashed = hash_alpha(alpha_dict)
    t = pd.read_parquet(CACHE_PATH)

    matches = t.loc[t["alpha_hashed"] == alpha_hashed]
    if len(matches) == 0:
        return None

    return matches.iloc[0]["alpha_id"]`

> [HEADING] {"level": "1", "content": "Examples"}

Integrate this into your existing BRAIN API workflow

> [HEADING] {"level": "2", "content": "Build alpha configuration"}

`alpha_list = [
    ace.generate_alpha(
        regular=x,
        region="USA",
        universe="TOP1000",
        delay=1,
        truncation=0.08,
    )
    for x in expression_list
]`

> [HEADING] {"level": "2", "content": "Filter out alphas that were already simulated (using the cache)"}

`new_alpha_list = []
reused_alpha_ids = []

for alpha_dict in alpha_list:
    existing_alpha_id = check_if_alpha_already_simulated(alpha_dict)
    if existing_alpha_id is not None:
        reused_alpha_ids.append(
            {"alpha_id": existing_alpha_id, "simulate_data": alpha_dict}
        )
    else:
        new_alpha_list.append(alpha_dict)

print(f"Found {len(reused_alpha_ids)} duplicates, {len(new_alpha_list)} new alphas to simulate.")

# If everything is a duplicate, you can stop here
if not new_alpha_list:
    stats_list_result = []  # Optionally fetch stats for reused_alpha_ids here`

> [HEADING] {"level": "2", "content": "Run multi-simulation only for new alphas"}

`else:
    multisim_results = ace.simulate_alpha_list_multi(
        ace_session,
        new_alpha_list,
        limit_of_concurrent_simulations=1,
        limit_of_multi_simulations=8,
    )`

> [HEADING] {"level": "1", "content": "Tips for success"}

- Hash the full configuration:Include expression(s) and all relevant settings (region, universe, delay, truncation, etc.).
- If you use generate_alpha function from ace_lib, hashing its output dictionary is enough.
- Consider cleanup:If the file grows large, you can delete older entries
