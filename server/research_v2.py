"""GenomicWQB 2.0 policy primitives.

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


POLICY_VERSION = "genomicwqb-2.0.0"
MAX_DATASET_SHARE = 0.25
MAX_EXPRESSION_SHARE = 0.30

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


def choose_search_mode(round_num: int, recent: Iterable[dict] | None = None,
                       has_specs: bool = False,
                       focus_code: str = "") -> tuple[str, str]:
    """Choose hypothesis, local 1–2 axis exploitation, or structural escape."""
    if has_specs:
        return "hypothesis", "pending evidence-backed strategy specs"
    rows = list(recent or [])[:60]

    # A near-pass focus parent can look attractive globally while its dataset
    # family repeatedly hits the same PROD wall.  Local 1–2 axis refinement
    # preserves that correlated signal, so make a structural jump instead.
    if focus_code:
        focus_datasets = set(lineage_profile(focus_code)["dataset_key"].split("+"))
        family_corr: list[float] = []
        for row in rows:
            code = str(row.get("code") or "")
            if not code:
                continue
            row_datasets = set(lineage_profile(code, row.get("genome") or {})[
                "dataset_key"].split("+"))
            if not focus_datasets.intersection(row_datasets):
                continue
            value = (row.get("metrics") or {}).get("prod_correlation")
            try:
                family_corr.append(float(value))
                continue
            except (TypeError, ValueError):
                pass
            match = _CORR_PATTERNS["PROD_CORRELATION"].search(
                str(row.get("submit_status") or ""))
            if match:
                family_corr.append(float(match.group(1)))
        failures = sum(v >= 0.7 for v in family_corr)
        if len(family_corr) >= 2 and failures / len(family_corr) >= 0.5:
            ds = "+".join(sorted(x for x in focus_datasets if x)) or "unknown"
            return "escape", (
                f"focus dataset correlation wall {ds} "
                f"{failures}/{len(family_corr)}")

    corr_rejects = sum(
        1 for r in rows
        if "PROD_CORRELATION" in str(r.get("submit_status") or "").upper()
    )
    if rows and corr_rejects / len(rows) >= 0.35:
        return "escape", f"PROD correlation wall {corr_rejects}/{len(rows)}"

    sharpes: list[float] = []
    for row in rows[:40]:
        try:
            sharpes.append(float((row.get("metrics") or {}).get("sharpe")))
        except (TypeError, ValueError):
            sharpes.append(float("nan"))
    if len(sharpes) >= 24:
        new = [x for x in sharpes[:12] if not math.isnan(x)]
        old = [x for x in sharpes[12:24] if not math.isnan(x)]
        if new and old and max(new) <= max(old) + 0.02:
            return "escape", "12-candidate Sharpe plateau"

    # Stable 70/30 background cadence.  Hypothesis rounds are scheduled separately.
    if int(round_num or 0) % 10 in (2, 5, 8):
        return "escape", "scheduled structural exploration"
    return "exploit", "1–2 axis local refinement"


def concentration_filter(strategies: list[dict], *, min_keep: int = 8,
                         dataset_share: float = MAX_DATASET_SHARE,
                         expression_share: float = MAX_EXPRESSION_SHARE,
                         recent: Iterable[dict] | None = None,
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
    for _pos, strategy, datasets, ex in ordered:
        ds = (strategy.get("_v2_lineage") or {}).get("dataset_key", "")
        blocked_ds = [x for x in datasets if ds_count[x] >= cap_ds]
        if blocked_ds or ex_count[ex] >= cap_expr:
            strategy["_v2_drop_reason"] = (
                f"concentration(dataset={'+'.join(blocked_ds) or ds}:"
                f"{max((ds_count[x] for x in datasets), default=0)}/{cap_ds},"
                f" expression={ex}:{ex_count[ex]}/{cap_expr})")
            dropped.append(strategy)
            continue
        kept.append(strategy)
        for dataset in datasets:
            ds_count[dataset] += 1
        ex_count[ex] += 1
    if len(kept) < min(min_keep, len(strategies)):
        need = min(min_keep, len(strategies)) - len(kept)
        dropped.sort(key=lambda s: (
            sum(recent_count[x] for x in
                ((s.get("_v2_lineage") or {}).get("dataset_key", "").split("+") or [""])),
            sum(ds_count[x] for x in
                ((s.get("_v2_lineage") or {}).get("dataset_key", "").split("+") or [""]))
            + ex_count[(s.get("_v2_lineage") or {}).get("expression_key", "")],
        ))
        restored, dropped = dropped[:need], dropped[need:]
        for s in restored:
            s.pop("_v2_drop_reason", None)
            kept.append(s)
    return kept, dropped


def submit_quality_reasons(metrics: dict | None) -> list[str]:
    """Return v2 absolute-quality misses even when WQB labels them WARNING.

    Consultant/HT classifications can temporarily downgrade the standard
    Sharpe and Fitness checks to warnings.  Architecture v2 optimizes the
    quality of a scarce daily submission, so a warning is not enough to waive
    the documented D0/D1 floor.  GLB candidates also have to clear each
    reported regional Sharpe floor; missing metrics are left to WQB rather
    than guessed.
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
