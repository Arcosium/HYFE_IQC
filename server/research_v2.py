"""GenomicWQB 2.1 policy primitives.

This module is intentionally small and mostly pure.  It provides the common
canonical identity, lineage profile, two-speed search decision, concentration
guard, and submit-response evidence promotion used by the worker and DB layer.
Keeping these decisions in one module prevents generator-specific exceptions
from silently creating a second policy.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import hashlib
import json
import math
import re
from typing import Any, Iterable

from . import alpha_ast
from . import settings_fp


POLICY_VERSION = "genomicwqb-2.1.1"
MAX_DATASET_SHARE = 0.25
MAX_EXPRESSION_SHARE = 0.30
MAX_QUARANTINED_SHARE = 0.10

_SPACE = re.compile(r"\s+")
_CORR_PATTERNS = {
    "PROD_CORRELATION": re.compile(
        r"(?:PROD_CORRELATION\s*\(\s*|prod_corr\s*\(\s*)([0-9.]+)", re.I),
    "SELF_CORRELATION": re.compile(
        r"(?:SELF_CORRELATION\s*\(\s*|self_corr(?:elation)?\s*\(\s*)([0-9.]+)", re.I),
}


def canonicalize(code: str, settings: dict | None = None, delay=None) -> dict[str, str]:
    """Return the one identity used by preflight, cache, submit, and evidence."""
    hygienic = alpha_ast.apply_field_hygiene(str(code or "")).strip()
    normalized = _SPACE.sub(" ", hygienic).strip().lower()
    effective = settings_fp.effective_settings(settings or {}, delay)
    fp = settings_fp.settings_fingerprint(effective)
    payload = json.dumps(
        {"code": normalized, "settings_fp": fp},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {"code": hygienic, "normalized_code": normalized,
            "settings_fp": fp, "canonical_key": key}


@lru_cache(maxsize=1)
def _field_dataset_map() -> dict[str, str]:
    try:
        from . import datafield_palette
        return {str(k): str(v).lower() for k, v in datafield_palette.field_dataset_map().items()}
    except Exception:
        return {}


def lineage_profile(code: str, genome: dict | None = None) -> dict[str, Any]:
    """Stable structural family: datasets + fields + operators + expression archetype."""
    if not isinstance(genome, dict):
        try:
            parsed = json.loads(str(genome or "{}"))
            genome = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            genome = {}
    fields = sorted(str(x) for x in alpha_ast.fields_used(str(code or "")) if x)
    operators = sorted(str(x).lower() for x in alpha_ast.operators_used(str(code or "")) if x)
    mapping = _field_dataset_map()
    datasets = sorted({mapping[f] for f in fields if f in mapping and mapping[f]})
    family = str((genome or {}).get("family") or "unknown").lower()
    dataset_key = "+".join(datasets) if datasets else family
    if "vector_neut" in operators:
        expression = "vector_neut"
    elif "regression_neut" in operators:
        expression = "regression_neut"
    elif "group_neutralize" in operators:
        expression = "group_neutralize"
    else:
        expression = "+".join(operators[:5]) or "raw"
    lineage_payload = json.dumps(
        {"datasets": datasets or [family], "fields": fields, "operators": operators},
        sort_keys=True, separators=(",", ":"),
    )
    lineage_key = hashlib.sha256(lineage_payload.encode("utf-8")).hexdigest()[:24]
    return {"lineage_key": lineage_key, "dataset_key": dataset_key,
            "expression_key": expression, "fields": fields,
            "operators": operators, "family": family}


def dataset_tokens(code: str, genome: dict | None = None) -> set[str]:
    """Datasets used by one candidate, including every member of mixed keys."""
    key = lineage_profile(code, genome)["dataset_key"]
    return {x for x in key.split("+") if x} or {key}


def _number(value: Any) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        return float(str(value).rstrip("%")) / (100.0 if str(value).endswith("%") else 1.0)
    except (TypeError, ValueError):
        return None


def dataset_key(row: dict) -> str:
    """Return the exact dataset combination used for policy attribution."""
    return str(lineage_profile(
        str(row.get("code") or row.get("parent_code") or ""),
        row.get("genome") or row.get("parent_genome") or {})["dataset_key"])


def _prod_corr(row: dict) -> float | None:
    value = _number((row.get("metrics") or {}).get("prod_correlation"))
    if value is not None:
        return value
    match = _CORR_PATTERNS["PROD_CORRELATION"].search(
        str(row.get("submit_status") or ""))
    return float(match.group(1)) if match else None


def quality_misses(row: dict) -> list[str]:
    """Stable D1/GLB quality misses used to identify actionable near passes."""
    metrics = row.get("metrics") or {}
    misses: list[str] = []
    checks = (
        ("sharpe", 1.58), ("fitness", 1.0),
        ("glb_amer_sharpe", 1.0), ("glb_emea_sharpe", 1.0),
        ("glb_apac_sharpe", 1.0),
    )
    for key, floor in checks:
        value = _number(metrics.get(key))
        if value is not None and value < floor:
            misses.append(key)
    return misses


def _is_near_miss(row: dict) -> bool:
    metrics = row.get("metrics") or {}
    sharpe = _number(metrics.get("sharpe"))
    fitness = _number(metrics.get("fitness"))
    return (sharpe is not None and fitness is not None
            and sharpe >= 1.40 and fitness >= 0.65
            and len(quality_misses(row)) <= 2)


def build_lineage_policy(recent: Iterable[dict] | None = None) -> dict[str, Any]:
    """Learn exact-dataset conversion and quarantine state from recent evidence.

    PROD correlation is deliberately attributed to an exact dataset combination.
    A bad ``risk70`` lineage therefore cannot force unrelated ``pv1`` or mixed
    lineages into structural escape.  Quarantine is reversible: one observation
    below the PROD threshold immediately prevents the lineage from being marked.
    """
    stats: dict[str, dict[str, Any]] = {}
    for row in list(recent or []):
        key = dataset_key(row)
        st = stats.setdefault(key, {
            "simulated": 0, "strict_is": 0, "near_miss": 0,
            "prod_observations": 0, "prod_rejects": 0, "prod_passes": 0,
        })
        st["simulated"] += 1
        misses = quality_misses(row)
        if not misses and _number((row.get("metrics") or {}).get("sharpe")) is not None:
            st["strict_is"] += 1
        if _is_near_miss(row):
            st["near_miss"] += 1
        status = str(row.get("submit_status") or "")
        corr = _prod_corr(row)
        if corr is not None or status == "submitted" or bool(row.get("submitted")):
            st["prod_observations"] += 1
            if status.upper().startswith("REJECTED:PROD_CORRELATION") or (
                    corr is not None and corr >= 0.7):
                st["prod_rejects"] += 1
            elif status == "submitted" or bool(row.get("submitted")) or (
                    corr is not None and corr < 0.7):
                st["prod_passes"] += 1

    quarantined: set[str] = set()
    for key, st in stats.items():
        observed = int(st["prod_observations"])
        rejected = int(st["prod_rejects"])
        st["strict_rate"] = st["strict_is"] / max(1, st["simulated"])
        st["near_rate"] = st["near_miss"] / max(1, st["simulated"])
        st["prod_pass_rate"] = st["prod_passes"] / max(1, observed)
        if (rejected >= 2 and st["prod_passes"] == 0
                and rejected / max(1, observed) >= 0.75):
            quarantined.add(key)

    near_keys = {key for key, st in stats.items()
                 if st["near_miss"] and key not in quarantined}
    return {
        "version": POLICY_VERSION,
        "dataset_stats": stats,
        "quarantined": sorted(quarantined),
        "near_miss_keys": sorted(near_keys),
        "near_miss_count": sum(stats[k]["near_miss"] for k in near_keys),
        "allocation": {"near_miss": 0.45, "exploration": 0.30,
                       "correlation_escape": 0.15, "hypothesis": 0.10},
    }


def policy_log_config(policy: dict | None) -> dict[str, Any]:
    """Compact, replayable policy snapshot without storing the entire row window."""
    policy = policy or {}
    return {
        "dataset_share": MAX_DATASET_SHARE,
        "expression_share": MAX_EXPRESSION_SHARE,
        "quarantine_share": MAX_QUARANTINED_SHARE,
        "quarantined": list(policy.get("quarantined") or []),
        "near_miss_keys": list(policy.get("near_miss_keys") or []),
        "allocation": dict(policy.get("allocation") or {}),
        "submit_policy": "probe_first_wqb_ground_truth",
        "post_submit_observation_s": 900.0,
    }


def candidate_priority(row: dict, policy: dict | None = None) -> float:
    """Rank seed/focus candidates by conversion potential, not raw Sharpe alone."""
    policy = policy or {}
    key = dataset_key(row)
    if key in set(policy.get("quarantined") or []):
        return -100.0
    metrics = row.get("metrics") or {}
    sharpe = _number(metrics.get("sharpe")) or 0.0
    fitness = _number(metrics.get("fitness")) or 0.0
    regions = [_number(metrics.get(k)) for k in (
        "glb_amer_sharpe", "glb_emea_sharpe", "glb_apac_sharpe")]
    regions = [v for v in regions if v is not None]
    regional = min(regions) if regions else 0.0
    near_bonus = 3.0 if _is_near_miss(row) else 0.0
    if key in set(policy.get("near_miss_keys") or []):
        near_bonus += 1.0
    lineage = (policy.get("dataset_stats") or {}).get(key) or {}
    conversion = 2.0 * float(lineage.get("strict_rate") or 0.0)
    return near_bonus + conversion + sharpe + 0.8 * fitness + 0.35 * regional


def select_seed_rows(rows: Iterable[dict], policy: dict | None = None,
                     top_n: int = 5) -> list[dict]:
    """Select diverse, high-conversion seeds with at most one quarantine probe."""
    policy = policy or {}
    quarantined = set(policy.get("quarantined") or [])
    unique: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row.get("genome"), dict):
            continue
        key = str(row.get("code_hash") or row.get("code") or row.get("id") or "")
        previous = unique.get(key)
        if previous is None or candidate_priority(row, policy) > candidate_priority(previous, policy):
            unique[key] = row
    ordered = sorted(unique.values(), key=lambda row: (
        candidate_priority(row, policy), int(row.get("id") or 0)), reverse=True)
    selected: list[dict] = []
    dataset_counts: Counter[str] = Counter()
    quarantine_count = 0
    for row in ordered:
        key = dataset_key(row)
        if key in quarantined:
            if quarantine_count >= 1:
                continue
            quarantine_count += 1
        elif dataset_counts[key] >= 1:
            continue
        selected.append(row)
        dataset_counts[key] += 1
        if len(selected) >= max(0, int(top_n)):
            break
    # A committee-provided pool can contain fewer than ``top_n`` distinct
    # dataset keys.  Keep the first pass maximally diverse, then fill only with
    # a second member of a non-quarantined key so simulation capacity is not lost.
    selected_ids = {id(row) for row in selected}
    if len(selected) < max(0, int(top_n)):
        for row in ordered:
            if id(row) in selected_ids:
                continue
            key = dataset_key(row)
            if key in quarantined or dataset_counts[key] >= 2:
                continue
            selected.append(row)
            selected_ids.add(id(row))
            dataset_counts[key] += 1
            if len(selected) >= int(top_n):
                break
    return selected


def focus_allowed(row: dict, policy: dict | None = None) -> tuple[bool, str]:
    key = dataset_key(row)
    if key in set((policy or {}).get("quarantined") or []):
        return False, f"quarantined PROD-correlation lineage {key}"
    return True, "actionable non-quarantined lineage"


def choose_search_mode(round_num: int, recent: Iterable[dict] | None = None,
                       has_specs: bool = False,
                       focus_code: str = "",
                       policy: dict | None = None) -> tuple[str, str]:
    """Choose hypothesis, local 1–2 axis exploitation, or structural escape."""
    if has_specs:
        return "hypothesis", "pending evidence-backed strategy specs"
    rows = list(recent or [])[:120]
    policy = policy or build_lineage_policy(rows)
    quarantined = set(policy.get("quarantined") or [])

    # A near-pass focus parent can look attractive globally while its dataset
    # family repeatedly hits the same PROD wall.  Local 1–2 axis refinement
    # preserves that correlated signal, so make a structural jump instead.
    if focus_code:
        focus_key = lineage_profile(focus_code)["dataset_key"]
        if focus_key in quarantined:
            st = (policy.get("dataset_stats") or {}).get(focus_key) or {}
            return "escape", (f"focus exact-lineage quarantine {focus_key} "
                              f"{int(st.get('prod_rejects') or 0)}/"
                              f"{int(st.get('prod_observations') or 0)}")

    # Actionable near passes receive most rounds.  A correlation wall in one
    # lineage never flips the whole system into escape mode.
    if int(policy.get("near_miss_count") or 0) > 0:
        if int(round_num or 0) % 4 == 0:
            return "escape", "scheduled 25% dataset/structure exploration"
        return "exploit", "non-quarantined near-miss conversion"

    sharpes: list[float] = []
    for row in [r for r in rows if dataset_key(r) not in quarantined][:40]:
        try:
            sharpes.append(float((row.get("metrics") or {}).get("sharpe")))
        except (TypeError, ValueError):
            sharpes.append(float("nan"))
    if len(sharpes) >= 24:
        new = [x for x in sharpes[:12] if not math.isnan(x)]
        old = [x for x in sharpes[12:24] if not math.isnan(x)]
        if new and old and max(new) <= max(old) + 0.02:
            return "escape", "12-candidate Sharpe plateau"

    if int(round_num or 0) % 4 == 0:
        return "escape", "scheduled 25% dataset/structure exploration"
    return "exploit", "1–2 axis local refinement"


def concentration_filter(strategies: list[dict], *, min_keep: int = 8,
                         dataset_share: float = MAX_DATASET_SHARE,
                         expression_share: float = MAX_EXPRESSION_SHARE,
                         recent: Iterable[dict] | None = None,
                         policy: dict | None = None,
                         ) -> tuple[list[dict], list[dict]]:
    """Maximize dataset coverage, then enforce dataset/expression concentration.

    Candidates are greedily reordered so uncovered single datasets come first,
    with datasets absent from the recent window preferred on ties.  The cap is
    then enforced on newly simulated candidates, not cached rows.  If the cap
    would leave fewer than ``min_keep`` candidates, the least-repeated dropped
    lineages are restored; keeping WQB slots fed remains the final fallback.
    """
    if not strategies:
        return [], []
    cap_ds = max(1, math.floor(len(strategies) * float(dataset_share)))
    cap_expr = max(1, math.floor(len(strategies) * float(expression_share)))
    quarantined = set((policy or {}).get("quarantined") or [])
    # A normal round ultimately simulates roughly 8–12 candidates after all
    # gates, regardless of the much larger pre-filter pool.  One probe keeps
    # quarantine reversible while staying at about ten percent of real spend.
    cap_quarantined = 1
    recent_count: Counter[str] = Counter()
    for row in recent or []:
        for dataset in dataset_tokens(
                str(row.get("code") or ""), row.get("genome") or {}):
            recent_count[dataset] += 1

    pool: list[tuple[int, dict, list[str], str]] = []
    pool_count: Counter[str] = Counter()
    for pos, strategy in enumerate(strategies):
        p = lineage_profile(strategy.get("code", ""), strategy.get("genome") or {})
        strategy["_v2_lineage"] = p
        datasets = [x for x in p["dataset_key"].split("+") if x] or [p["dataset_key"]]
        pool.append((pos, strategy, datasets, p["expression_key"]))
        pool_count.update(datasets)

    # Round-robin set cover: a clean single-dataset candidate is easiest to
    # attribute and counts toward dataset/pyramid breadth, so it wins before a
    # mixed candidate.  Within the same class, recent under-use breaks ties.
    ordered: list[tuple[int, dict, list[str], str]] = []
    covered_ds: set[str] = set()
    covered_expr: set[str] = set()
    remaining = list(pool)
    while remaining:
        def coverage_key(item):
            pos, _strategy, datasets, expression = item
            new_ds = set(datasets) - covered_ds
            recent_load = sum(recent_count[d] for d in datasets)
            rarity = sum(1.0 / max(1, pool_count[d]) for d in datasets)
            return (bool(new_ds), bool(new_ds) and len(datasets) == 1,
                    len(new_ds), -recent_load, expression not in covered_expr,
                    rarity, -pos)

        picked = max(remaining, key=coverage_key)
        remaining.remove(picked)
        ordered.append(picked)
        covered_ds.update(picked[2])
        covered_expr.add(picked[3])

    ds_count: Counter[str] = Counter()
    ex_count: Counter[str] = Counter()
    kept: list[dict] = []
    dropped: list[dict] = []
    quarantined_count = 0
    for _pos, strategy, datasets, ex in ordered:
        ds = (strategy.get("_v2_lineage") or {}).get("dataset_key", "")
        blocked_ds = [x for x in datasets if ds_count[x] >= cap_ds]
        blocked_quarantine = ds in quarantined and quarantined_count >= cap_quarantined
        if blocked_ds or ex_count[ex] >= cap_expr or blocked_quarantine:
            strategy["_v2_drop_reason"] = (
                f"{'quarantine' if blocked_quarantine else 'concentration'}("
                f"dataset={'+'.join(blocked_ds) or ds}:"
                f"{max((ds_count[x] for x in datasets), default=0)}/{cap_ds},"
                f" expression={ex}:{ex_count[ex]}/{cap_expr})")
            dropped.append(strategy)
            continue
        kept.append(strategy)
        if ds in quarantined:
            quarantined_count += 1
        for dataset in datasets:
            ds_count[dataset] += 1
        ex_count[ex] += 1
    if len(kept) < min(min_keep, len(strategies)):
        need = min(min_keep, len(strategies)) - len(kept)
        dropped.sort(key=lambda s: (
            (s.get("_v2_lineage") or {}).get("dataset_key", "") in quarantined,
            sum(recent_count[x] for x in
                ((s.get("_v2_lineage") or {}).get("dataset_key", "").split("+") or [""])),
            sum(ds_count[x] for x in
                ((s.get("_v2_lineage") or {}).get("dataset_key", "").split("+") or [""]))
            + ex_count[(s.get("_v2_lineage") or {}).get("expression_key", "")],
        ))
        restorable = [s for s in dropped
                      if (s.get("_v2_lineage") or {}).get("dataset_key", "") not in quarantined]
        if len(restorable) < need:
            restorable.extend(s for s in dropped if s not in restorable)
        restored = restorable[:need]
        restored_ids = {id(s) for s in restored}
        dropped = [s for s in dropped if id(s) not in restored_ids]
        for s in restored:
            s.pop("_v2_drop_reason", None)
            kept.append(s)
    return kept, dropped


def absolute_quality_observations(metrics: dict | None) -> list[str]:
    """Return absolute-quality misses for ranking and diagnosis only.

    Consultant/HT classifications can temporarily downgrade the standard
    Sharpe and Fitness checks to warnings.  This signal helps choose seeds and
    mutation axes, but must never prevent a real submit probe: rejection is
    free and the WQB response is the ground truth.  Missing metrics are left
    unknown rather than guessed.
    """
    from . import criteria

    m = metrics or {}

    def number(*keys: str) -> float | None:
        for key in keys:
            try:
                raw = m.get(key)
                if raw not in (None, ""):
                    return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    delay = str(m.get("_delay") or m.get("delay") or "1")
    region = str(m.get("region") or "").upper()
    floors = criteria.cutoffs(delay, region)
    checks = [
        ("LOW_SHARPE", number("sharpe_check", "sharpe"),
         number("sharpe_check_cutoff") or float(floors["sharpe"])),
        ("LOW_FITNESS", number("fitness_check", "fitness"),
         number("fitness_check_cutoff") or float(floors["fitness"])),
    ]
    if region == "GLB":
        checks.extend([
            ("LOW_GLB_AMER_SHARPE", number("glb_amer_sharpe"),
             number("glb_amer_sharpe_cutoff") or 1.0),
            ("LOW_GLB_EMEA_SHARPE", number("glb_emea_sharpe"),
             number("glb_emea_sharpe_cutoff") or 1.0),
            ("LOW_GLB_APAC_SHARPE", number("glb_apac_sharpe"),
             number("glb_apac_sharpe_cutoff") or 1.0),
        ])
    return [name for name, value, floor in checks
            if value is not None and floor is not None and value < floor]


def prune_focus_queue(queue: Iterable[dict] | None,
                      last_main_round: int) -> tuple[list[dict], int]:
    """Drop legacy multi-phase focus debt when architecture v2 takes over.

    V2 spends at most one refinement round on a parent.  Entries from an older
    main round are stale search debt, while phase 2/3 entries belong to the old
    three-pass policy.  Keeping either lets historical parents consume the new
    policy's simulation budget before fresh escape candidates can run.
    """
    kept: list[dict] = []
    seen: set[tuple[int, int]] = set()
    floor = int(last_main_round or 0)
    rows = list(queue or [])
    for entry in rows:
        parent_round = int(entry.get("parent_round_num") or 0)
        phase = int(entry.get("phase") or 1)
        parent_id = int(entry.get("parent_alpha_id") or 0)
        parent_idx = int(entry.get("parent_idx") or 0)
        key = (parent_round, parent_id or parent_idx)
        if parent_round < floor or phase != 1 or key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    return kept, len(rows) - len(kept)


def promote_submit_evidence(result: dict) -> dict:
    """Promote correlation values in a submit response into metrics/check buckets."""
    status = str(result.get("submit_status") or "")
    if not status:
        return result
    metrics = dict(result.get("metrics") or {})
    is_status = dict(result.get("is_status") or {})
    is_status.setdefault("pass", [])
    is_status.setdefault("fail", [])
    is_status.setdefault("error", [])
    is_status.setdefault("pending", [])
    is_status.setdefault("warning", [])
    for name, rx in _CORR_PATTERNS.items():
        match = rx.search(status)
        if not match:
            continue
        value = float(match.group(1))
        key = "prod_correlation" if name == "PROD_CORRELATION" else "self_correlation"
        metrics[key] = str(value)
        if status.lower().startswith("rejected:") and not any(
                str(x.get("name") if isinstance(x, dict) else x).upper() == name
                for x in is_status["fail"]):
            is_status["fail"].append({"name": name, "value": str(value),
                                      "cutoff": "0.7", "result": "FAIL",
                                      "desc": f"{name} of {value} is above cutoff of 0.7 (FAIL)"})
    result["metrics"] = metrics
    result["is_status"] = is_status
    return result


def portfolio_dimensions(metrics: dict | None) -> dict[str, float | None]:
    m = metrics or {}

    def f(key: str) -> float | None:
        try:
            return float(m.get(key))
        except (TypeError, ValueError):
            return None

    regions = [v for v in (f("glb_amer_sharpe"), f("glb_emea_sharpe"),
                            f("glb_apac_sharpe")) if v is not None]
    return {"min_region_sharpe": min(regions) if regions else None,
            "prod_correlation": f("prod_correlation")}
