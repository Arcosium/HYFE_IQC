#!/usr/bin/env python3
"""Reproducible analysis for the single-axis mutation report.

The script reads a read-only GenomicWQB SQLite snapshot and writes only
aggregate results and public-benchmark outputs to an explicitly supplied
directory. Raw rows never leave the vault-backed analysis workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from scipy.stats import norm, wilcoxon
from statsmodels.stats.proportion import proportion_confint


SEOUL = ZoneInfo("Asia/Seoul")
BOOTSTRAP_SEED = 20260821
BENCHMARK_SEED = 20260822


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--external", action="store_true")
    parser.add_argument("--reuse-external", action="store_true")
    parser.add_argument("--benchmark-reps", type=int, default=20)
    parser.add_argument("--benchmark-budget-multiplier", type=int, default=20)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_genes(raw: str | None) -> list[str] | None:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


def wilson(successes: int, n: int) -> tuple[float, float]:
    low, high = proportion_confint(successes, n, method="wilson")
    return float(low), float(high)


def percentile_ci(values: np.ndarray, level: float = 0.95) -> tuple[float, float]:
    alpha = (1.0 - level) / 2.0
    return (
        float(np.quantile(values, alpha)),
        float(np.quantile(values, 1.0 - alpha)),
    )


def parent_cluster_bootstrap(
    df: pd.DataFrame,
    value: str,
    statistic: str = "mean",
    iterations: int = 2000,
) -> tuple[float, float, np.ndarray]:
    """Bootstrap the single-minus-multi contrast by resampling parents."""
    if df.empty or df["single"].nunique() < 2:
        return float("nan"), float("nan"), np.asarray([], dtype=float)
    parent_ids = df["parent_alpha_id"].drop_duplicates().to_numpy()
    parent_position = {parent_id: idx for idx, parent_id in enumerate(parent_ids)}
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    sampled_positions = rng.integers(
        0, len(parent_ids), size=(iterations, len(parent_ids))
    )
    if statistic == "mean":
        grouped = (
            df.groupby(["parent_alpha_id", "single"])[value]
            .agg(["sum", "count"])
            .unstack(fill_value=0)
            .reindex(parent_ids, fill_value=0)
        )
        sums_true = grouped[("sum", True)].to_numpy(float)
        sums_false = grouped[("sum", False)].to_numpy(float)
        counts_true = grouped[("count", True)].to_numpy(float)
        counts_false = grouped[("count", False)].to_numpy(float)
        values = np.empty(iterations)
        for idx, sampled in enumerate(sampled_positions):
            values[idx] = (
                sums_true[sampled].sum() / counts_true[sampled].sum()
                - sums_false[sampled].sum() / counts_false[sampled].sum()
            )
    else:
        arm_data = {}
        for arm in (False, True):
            part = df[df["single"] == arm][["parent_alpha_id", value]].sort_values(value)
            arm_data[arm] = (
                part[value].to_numpy(float),
                part["parent_alpha_id"].map(parent_position).to_numpy(int),
            )

        def weighted_median(sorted_values: np.ndarray, row_parents: np.ndarray, multiplicity: np.ndarray) -> float:
            weights = multiplicity[row_parents]
            threshold = weights.sum() / 2.0
            return float(sorted_values[np.searchsorted(np.cumsum(weights), threshold, side="left")])

        values = np.empty(iterations)
        for idx, sampled in enumerate(sampled_positions):
            multiplicity = np.bincount(sampled, minlength=len(parent_ids))
            medians = {
                arm: weighted_median(*arm_data[arm], multiplicity)
                for arm in (False, True)
            }
            values[idx] = medians[True] - medians[False]
    low, high = percentile_ci(values)
    return low, high, values


def sibling_parent_bootstrap(
    parent_contrasts: pd.Series, iterations: int = 5000
) -> tuple[float, float]:
    rng = np.random.default_rng(BOOTSTRAP_SEED + 1)
    values = parent_contrasts.dropna().to_numpy(float)
    draws = np.empty(iterations)
    for idx in range(iterations):
        draws[idx] = rng.choice(values, size=len(values), replace=True).mean()
    return percentile_ci(draws)


def load_lineages(connection: sqlite3.Connection) -> pd.DataFrame:
    query = """
        SELECT
            c.id,
            c.parent_alpha_id,
            c.user_id,
            c.round_num,
            c.sharpe,
            c.fitness,
            c.self_corr,
            c.metrics,
            c.submitted,
            c.fail_count,
            c.error_count,
            c.cached,
            c.code_hash,
            c.settings_fp,
            c.genes_changed,
            c.origin,
            c.directive,
            c.ts,
            c.region,
            c.universe,
            c.delay,
            c.generation,
            p.sharpe AS parent_sharpe,
            p.fitness AS parent_fitness,
            p.self_corr AS parent_self_corr,
            p.code_hash AS parent_code_hash,
            p.settings_fp AS parent_settings_fp,
            p.ts AS parent_ts
        FROM alphas c
        JOIN alphas p ON p.id = c.parent_alpha_id
        WHERE c.parent_alpha_id IS NOT NULL
          AND c.sharpe IS NOT NULL
          AND p.sharpe IS NOT NULL
    """
    frame = pd.read_sql_query(query, connection)
    frame["genes"] = frame["genes_changed"].map(parse_genes)
    def metric_self_corr(raw: str | None) -> float:
        try:
            metrics = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return np.nan
        for key in ("self_correlation", "selfCorrelation", "self_corr"):
            value = metrics.get(key) if isinstance(metrics, dict) else None
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return np.nan
        return np.nan

    frame["self_corr"] = frame["self_corr"].fillna(
        frame["metrics"].map(metric_self_corr)
    )
    frame["k"] = frame["genes"].map(
        lambda value: len(value) if isinstance(value, list) else np.nan
    )
    frame["exact_config"] = (
        frame["code_hash"].fillna("") == frame["parent_code_hash"].fillna("")
    ) & (
        frame["settings_fp"].fillna("")
        == frame["parent_settings_fp"].fillna("")
    )
    frame["time_order_valid"] = frame["ts"] >= frame["parent_ts"]
    frame["delta_sharpe"] = frame["sharpe"] - frame["parent_sharpe"]
    frame["improved"] = (frame["delta_sharpe"] > 0).astype(int)
    frame["crossed_158"] = (
        (frame["parent_sharpe"] < 1.58) & (frame["sharpe"] >= 1.58)
    ).astype(int)
    frame["single"] = frame["k"].eq(1)
    frame["week"] = pd.to_datetime(frame["ts"], unit="s", utc=True).dt.strftime(
        "%G-W%V"
    )
    for column in ("origin", "region", "universe"):
        frame[column] = frame[column].fillna("missing").replace("", "missing")
    frame["delay"] = frame["delay"].fillna(-1).astype(int).astype(str)
    frame["generation"] = frame["generation"].fillna(-1)
    return frame


def cohort_flow(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    flow: dict[str, int] = {"joined_parent_child_with_sharpe": int(len(raw))}
    eligible = raw[raw["k"].notna()].copy()
    flow["parsed_genes_changed"] = int(len(eligible))
    flow["excluded_zero_changed"] = int((eligible["k"] == 0).sum())
    eligible = eligible[eligible["k"] >= 1].copy()
    flow["excluded_cached"] = int((eligible["cached"] == 1).sum())
    eligible = eligible[eligible["cached"] == 0].copy()
    flow["excluded_exact_config"] = int(eligible["exact_config"].sum())
    eligible = eligible[~eligible["exact_config"]].copy()
    flow["excluded_invalid_time_order"] = int((~eligible["time_order_valid"]).sum())
    primary = eligible[eligible["time_order_valid"]].copy()
    flow["primary_evaluated_lineage_pairs"] = int(len(primary))
    flow["primary_single"] = int(primary["single"].sum())
    flow["primary_multi"] = int((~primary["single"]).sum())
    return primary, flow


def describe_snapshot(connection: sqlite3.Connection, db_path: Path) -> dict:
    def scalar(sql: str):
        return connection.execute(sql).fetchone()[0]

    min_ts, max_ts = connection.execute(
        "SELECT MIN(ts), MAX(ts) FROM alphas"
    ).fetchone()
    return {
        "snapshot_sha256": sha256(db_path),
        "alphas": int(scalar("SELECT COUNT(*) FROM alphas")),
        "rounds": int(scalar("SELECT COUNT(*) FROM rounds")),
        "unique_codes": int(scalar("SELECT COUNT(DISTINCT code) FROM alphas")),
        "unique_settings": int(
            scalar(
                "SELECT COUNT(DISTINCT settings_fp) FROM alphas "
                "WHERE settings_fp IS NOT NULL AND settings_fp <> ''"
            )
        ),
        "successful_submit_attempts": int(
            scalar("SELECT COUNT(*) FROM submit_attempts WHERE submitted=1")
        ),
        "min_ts": float(min_ts),
        "max_ts": float(max_ts),
        "min_kst": datetime.fromtimestamp(min_ts, SEOUL).isoformat(timespec="seconds"),
        "max_kst": datetime.fromtimestamp(max_ts, SEOUL).isoformat(timespec="seconds"),
    }


def descriptive_by_k(primary: pd.DataFrame) -> pd.DataFrame:
    display = primary.copy()
    display["k_group"] = display["k"].map(
        lambda value: "6+" if value >= 6 else str(int(value))
    )
    rows = []
    for group in ["1", "2", "3", "4", "5", "6+"]:
        part = display[display["k_group"] == group]
        successes = int(part["improved"].sum())
        low, high = wilson(successes, len(part))
        rows.append(
            {
                "k_group": group,
                "n": int(len(part)),
                "improved_n": successes,
                "improve_rate": float(part["improved"].mean()),
                "improve_ci_low": low,
                "improve_ci_high": high,
                "delta_mean": float(part["delta_sharpe"].mean()),
                "delta_median": float(part["delta_sharpe"].median()),
                "self_corr_n": int(part["self_corr"].notna().sum()),
                "self_corr_median": (
                    float(part["self_corr"].median())
                    if part["self_corr"].notna().any()
                    else None
                ),
                "crossed_158_rate": float(part["crossed_158"].mean()),
            }
        )
    return pd.DataFrame(rows)


def clustered_ols_summary(model, groups: pd.Series, coefficient: str) -> dict:
    """OLS coefficient with a parent-cluster sandwich covariance.

    statsmodels' generic cluster path is unstable for the deliberately
    redundant fixed-effect matrices used here. A symmetric Moore-Penrose
    bread matrix keeps the estimand while producing the usual CR1 variance.
    """
    design = np.asarray(model.model.exog, dtype=float)
    residual = np.asarray(model.resid, dtype=float)
    group_values = np.asarray(groups)
    unique_groups = np.unique(group_values)
    bread = np.linalg.pinv(design.T @ design)
    meat = np.zeros((design.shape[1], design.shape[1]), dtype=float)
    for group in unique_groups:
        mask = group_values == group
        score = design[mask].T @ residual[mask]
        meat += np.outer(score, score)
    covariance = bread @ meat @ bread
    covariance = (covariance + covariance.T) / 2.0
    n, p = design.shape
    correction = (len(unique_groups) / (len(unique_groups) - 1)) * ((n - 1) / (n - p))
    covariance *= correction
    position = model.model.exog_names.index(coefficient)
    estimate = float(model.params.iloc[position])
    se = float(np.sqrt(max(covariance[position, position], 0.0)))
    z_score = estimate / se if se > 0 else math.inf
    return {
        "estimate": estimate,
        "se": se,
        "ci_low": estimate - 1.96 * se,
        "ci_high": estimate + 1.96 * se,
        "p_value": float(2.0 * norm.sf(abs(z_score))),
        "n": int(model.nobs),
    }


def regression_results(primary: pd.DataFrame) -> dict:
    formula_controls = (
        "single + parent_sharpe + I(parent_sharpe ** 2) + generation "
        "+ C(origin) + C(region) + C(universe) + C(delay) + C(week)"
    )
    result: dict[str, dict] = {}
    for outcome in ("improved", "delta_sharpe", "crossed_158"):
        model = smf.ols(
            f"{outcome} ~ {formula_controls}", data=primary
        ).fit()
        summary = clustered_ols_summary(model, primary["parent_alpha_id"], "single[T.True]")
        summary["r_squared"] = float(model.rsquared)
        result[f"adjusted_{outcome}"] = summary

    arm_presence = primary.groupby("parent_alpha_id")["single"].agg(["min", "max"])
    mixed_parent_ids = arm_presence.index[
        (arm_presence["min"] == False) & (arm_presence["max"] == True)  # noqa: E712
    ]
    siblings = primary[primary["parent_alpha_id"].isin(mixed_parent_ids)].copy()
    result["mixed_parent_count"] = int(len(mixed_parent_ids))
    result["mixed_parent_rows"] = int(len(siblings))

    for outcome in ("improved", "delta_sharpe", "crossed_158"):
        formula = (
            f"{outcome} ~ single + C(parent_alpha_id) + C(origin) + C(week)"
        )
        model = smf.ols(formula, data=siblings).fit()
        result[f"sibling_fe_{outcome}"] = clustered_ols_summary(
            model, siblings["parent_alpha_id"], "single[T.True]"
        )

        means = (
            siblings.groupby(["parent_alpha_id", "single"])[outcome]
            .mean()
            .unstack()
        )
        parent_contrast = means[True] - means[False]
        low, high = sibling_parent_bootstrap(parent_contrast)
        result[f"sibling_equal_parent_{outcome}"] = {
            "estimate": float(parent_contrast.mean()),
            "median": float(parent_contrast.median()),
            "ci_low": low,
            "ci_high": high,
            "positive_parent_fraction": float((parent_contrast > 0).mean()),
            "parent_n": int(parent_contrast.notna().sum()),
        }
    return result


def main_effects(primary: pd.DataFrame) -> dict:
    result: dict[str, dict] = {}
    for outcome, statistic in (
        ("improved", "mean"),
        ("delta_sharpe", "mean"),
        ("crossed_158", "mean"),
        ("self_corr", "median"),
    ):
        part = primary.dropna(subset=[outcome]).copy()
        if part.empty or part["single"].nunique() < 2:
            result[outcome] = {
                "single": None,
                "multi": None,
                "difference": None,
                "ci_low": None,
                "ci_high": None,
                "single_n": int((part["single"] == True).sum()),  # noqa: E712
                "multi_n": int((part["single"] == False).sum()),  # noqa: E712
            }
            continue
        grouped = part.groupby("single")[outcome]
        arm_stat = grouped.median() if statistic == "median" else grouped.mean()
        low, high, _ = parent_cluster_bootstrap(
            part, outcome, statistic=statistic
        )
        result[outcome] = {
            "single": float(arm_stat.loc[True]),
            "multi": float(arm_stat.loc[False]),
            "difference": float(arm_stat.loc[True] - arm_stat.loc[False]),
            "ci_low": low,
            "ci_high": high,
            "single_n": int((part["single"] == True).sum()),  # noqa: E712
            "multi_n": int((part["single"] == False).sum()),  # noqa: E712
        }
    return result


def heterogeneity(primary: pd.DataFrame) -> pd.DataFrame:
    frames: list[dict] = []
    temp = primary.copy()
    temp["parent_quartile"] = pd.qcut(
        temp["parent_sharpe"], 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop"
    )
    for dimension, values in (
        ("origin", sorted(temp["origin"].unique())),
        ("parent_quartile", ["Q1", "Q2", "Q3", "Q4"]),
    ):
        for value in values:
            part = temp[temp[dimension].astype(str) == str(value)]
            if part["single"].nunique() < 2 or len(part) < 50:
                continue
            single = part.loc[part["single"], "improved"]
            multi = part.loc[~part["single"], "improved"]
            low, high, _ = parent_cluster_bootstrap(part, "improved", iterations=1000)
            frames.append(
                {
                    "dimension": dimension,
                    "group": str(value),
                    "n": int(len(part)),
                    "single_n": int(len(single)),
                    "multi_n": int(len(multi)),
                    "single_rate": float(single.mean()),
                    "multi_rate": float(multi.mean()),
                    "difference": float(single.mean() - multi.mean()),
                    "ci_low": low,
                    "ci_high": high,
                }
            )
    return pd.DataFrame(frames)


def single_gene_results(primary: pd.DataFrame) -> pd.DataFrame:
    single = primary[primary["single"]].copy()
    single["gene"] = single["genes"].map(lambda values: values[0])
    rows = []
    for gene, part in single.groupby("gene"):
        if len(part) < 30:
            continue
        successes = int(part["improved"].sum())
        low, high = wilson(successes, len(part))
        rows.append(
            {
                "gene": gene,
                "n": int(len(part)),
                "improve_rate": float(part["improved"].mean()),
                "ci_low": low,
                "ci_high": high,
                "delta_median": float(part["delta_sharpe"].median()),
            }
        )
    return pd.DataFrame(rows).sort_values(["improve_rate", "n"], ascending=False)


def multi_strength_distribution(primary: pd.DataFrame) -> dict[int, float]:
    counts = Counter(int(value) for value in primary.loc[~primary["single"], "k"])
    total = sum(counts.values())
    return {key: value / total for key, value in sorted(counts.items())}


def run_external_benchmark(
    out_dir: Path,
    strength_distribution: dict[int, float],
    reps: int,
    budget_multiplier: int,
) -> pd.DataFrame:
    import ioh

    rng_master = np.random.default_rng(BENCHMARK_SEED)
    strengths = np.array(list(strength_distribution), dtype=int)
    probabilities = np.array(list(strength_distribution.values()), dtype=float)
    probabilities /= probabilities.sum()
    rows: list[dict] = []

    for function_id in range(1, 26):
        for dimension in (16, 100):
            for instance in (1, 2, 3):
                problem = ioh.get_problem(
                    function_id, instance, dimension, ioh.ProblemClass.PBO
                )
                function_name = problem.meta_data.name
                optimum = float(problem.optimum.y)
                for rep in range(reps):
                    pair_seed = int(rng_master.integers(0, np.iinfo(np.int32).max))
                    start_rng = np.random.default_rng(pair_seed)
                    start = start_rng.integers(0, 2, size=dimension, dtype=np.int8)
                    for arm_idx, arm in enumerate(("single", "empirical_multi")):
                        rng = np.random.default_rng(pair_seed + 1000003 * (arm_idx + 1))
                        problem.reset()
                        current = start.copy()
                        current_value = float(problem(current.tolist()))
                        start_value = current_value
                        best_value = current_value
                        first_hit = 1 if math.isclose(best_value, optimum) else None
                        accepted = 0
                        improving = 0
                        budget = budget_multiplier * dimension
                        for evaluation in range(2, budget + 1):
                            if arm == "single":
                                k = 1
                            else:
                                k = int(rng.choice(strengths, p=probabilities))
                                k = min(k, dimension)
                            indexes = rng.choice(dimension, size=k, replace=False)
                            candidate = current.copy()
                            candidate[indexes] ^= 1
                            value = float(problem(candidate.tolist()))
                            if value >= current_value:
                                accepted += 1
                                if value > current_value:
                                    improving += 1
                                current = candidate
                                current_value = value
                            if value > best_value:
                                best_value = value
                            if first_hit is None and math.isclose(best_value, optimum):
                                first_hit = evaluation
                        denominator = optimum - start_value
                        normalized_progress = (
                            (best_value - start_value) / denominator
                            if denominator > 0
                            else 1.0
                        )
                        rows.append(
                            {
                                "function_id": function_id,
                                "function_name": function_name,
                                "dimension": dimension,
                                "instance": instance,
                                "rep": rep,
                                "pair_seed": pair_seed,
                                "arm": arm,
                                "budget": budget,
                                "start_value": start_value,
                                "best_value": best_value,
                                "optimum": optimum,
                                "normalized_progress": float(normalized_progress),
                                "hit_optimum": int(first_hit is not None),
                                "first_hit": first_hit,
                                "accepted": accepted,
                                "improving": improving,
                            }
                        )
    frame = pd.DataFrame(rows)
    frame.to_csv(out_dir / "external_benchmark_runs.csv", index=False)
    return frame


def summarize_external(frame: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    keys = ["function_id", "function_name", "dimension", "instance", "rep", "pair_seed"]
    pivot = frame.pivot(index=keys, columns="arm", values=["normalized_progress", "hit_optimum"])
    pivot.columns = [f"{metric}_{arm}" for metric, arm in pivot.columns]
    pivot = pivot.reset_index()
    pivot["progress_difference"] = (
        pivot["normalized_progress_single"]
        - pivot["normalized_progress_empirical_multi"]
    )
    pivot["hit_difference"] = (
        pivot["hit_optimum_single"] - pivot["hit_optimum_empirical_multi"]
    )
    statistic, p_value = wilcoxon(
        pivot["normalized_progress_single"],
        pivot["normalized_progress_empirical_multi"],
        zero_method="zsplit",
    )
    rng = np.random.default_rng(BENCHMARK_SEED + 1)
    cell_means = (
        pivot.groupby(["function_id", "dimension"])[["progress_difference", "hit_difference"]]
        .mean()
        .reset_index()
    )
    sampled = rng.integers(0, len(cell_means), size=(5000, len(cell_means)))
    progress_values = cell_means["progress_difference"].to_numpy()
    hit_values = cell_means["hit_difference"].to_numpy()
    boot = progress_values[sampled].mean(axis=1)
    boot_hit = hit_values[sampled].mean(axis=1)
    low, high = percentile_ci(boot)
    hit_low, hit_high = percentile_ci(boot_hit)
    cells = (
        pivot.groupby(["function_id", "function_name", "dimension"])
        .agg(
            n=("progress_difference", "size"),
            progress_single=("normalized_progress_single", "mean"),
            progress_multi=("normalized_progress_empirical_multi", "mean"),
            progress_difference=("progress_difference", "mean"),
            hit_single=("hit_optimum_single", "mean"),
            hit_multi=("hit_optimum_empirical_multi", "mean"),
        )
        .reset_index()
    )
    result = {
        "paired_runs": int(len(pivot)),
        "problem_dimension_cells": int(len(cells)),
        "progress_single": float(pivot["normalized_progress_single"].mean()),
        "progress_multi": float(pivot["normalized_progress_empirical_multi"].mean()),
        "progress_difference": float(pivot["progress_difference"].mean()),
        "progress_ci_low": low,
        "progress_ci_high": high,
        "wilcoxon_statistic": float(statistic),
        "wilcoxon_p_value": float(p_value),
        "hit_single": float(pivot["hit_optimum_single"].mean()),
        "hit_multi": float(pivot["hit_optimum_empirical_multi"].mean()),
        "hit_difference": float(pivot["hit_difference"].mean()),
        "hit_ci_low": hit_low,
        "hit_ci_high": hit_high,
        "cells_single_better": int((cells["progress_difference"] > 1e-12).sum()),
        "cells_tied": int((cells["progress_difference"].abs() <= 1e-12).sum()),
        "cells_multi_better": int((cells["progress_difference"] < -1e-12).sum()),
    }
    return result, cells


def set_plot_style() -> None:
    candidates = [
        "/home/arcosium/.local/share/fonts/NotoSansKR-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            from matplotlib import font_manager

            font_manager.fontManager.addfont(candidate)
            prop = font_manager.FontProperties(fname=candidate)
            plt.rcParams["font.family"] = prop.get_name()
            break
    plt.rcParams.update(
        {
            "axes.unicode_minus": False,
            "figure.dpi": 150,
            "savefig.dpi": 220,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#B7C0CC",
            "axes.labelcolor": "#26364A",
            "xtick.color": "#4E5D6C",
            "ytick.color": "#4E5D6C",
        }
    )


def plot_dose_response(table: pd.DataFrame, out_dir: Path) -> None:
    set_plot_style()
    x = np.arange(len(table))
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))
    rate = table["improve_rate"].to_numpy()
    axes[0].errorbar(
        x,
        rate,
        yerr=[rate - table["improve_ci_low"], table["improve_ci_high"] - rate],
        fmt="o-",
        color="#245C88",
        ecolor="#7D9CB7",
        capsize=4,
        lw=2,
    )
    axes[0].set_xticks(x, table["k_group"])
    axes[0].set_xlabel("동시에 바꾼 유전자 수")
    axes[0].set_ylabel("부모 대비 Sharpe 개선률")
    axes[0].yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
    axes[0].grid(axis="y", color="#E7ECF1")
    for idx, row in table.iterrows():
        axes[0].annotate(f"n={int(row['n']):,}", (idx, row["improve_rate"]), xytext=(0, 10), textcoords="offset points", ha="center", fontsize=8)

    colors = ["#D17A22"] + ["#AAB5C2"] * (len(table) - 1)
    axes[1].bar(x, table["delta_median"], color=colors, width=0.66)
    axes[1].axhline(0, color="#26364A", lw=0.8)
    axes[1].set_xticks(x, table["k_group"])
    axes[1].set_xlabel("동시에 바꾼 유전자 수")
    axes[1].set_ylabel("Sharpe 변화량 중앙값")
    axes[1].grid(axis="y", color="#E7ECF1")
    fig.tight_layout()
    fig.savefig(out_dir / "fig1_internal_dose_response.png", bbox_inches="tight")
    plt.close(fig)


def plot_effects(effects: dict, regressions: dict, out_dir: Path) -> None:
    set_plot_style()
    entries = [
        ("무보정", effects["improved"]),
        ("공변량 조정", regressions["adjusted_improved"]),
        ("동일 부모 고정효과", regressions["sibling_fe_improved"]),
    ]
    estimates = np.array([item[1]["difference"] if "difference" in item[1] else item[1]["estimate"] for item in entries])
    lows = np.array([item[1]["ci_low"] for item in entries])
    highs = np.array([item[1]["ci_high"] for item in entries])
    y = np.arange(len(entries))[::-1]
    fig, ax = plt.subplots(figsize=(8.2, 3.5))
    ax.errorbar(
        estimates,
        y,
        xerr=[estimates - lows, highs - estimates],
        fmt="o",
        color="#245C88",
        ecolor="#7D9CB7",
        capsize=4,
        markersize=7,
    )
    ax.axvline(0, color="#7A8793", lw=1)
    ax.set_yticks(y, [item[0] for item in entries])
    ax.set_xlabel("1축 변이의 개선률 차이(퍼센트포인트)")
    ax.xaxis.set_major_formatter(lambda value, _: f"{value * 100:.1f}")
    ax.grid(axis="x", color="#E7ECF1")
    fig.tight_layout()
    fig.savefig(out_dir / "fig2_internal_effect_estimates.png", bbox_inches="tight")
    plt.close(fig)


def plot_heterogeneity(table: pd.DataFrame, out_dir: Path) -> None:
    set_plot_style()
    shown = table.copy()
    origin_names = {"crossover": "교차", "mutate": "일반 변이", "sweep": "스윕"}
    shown["label"] = shown.apply(
        lambda row: (
            f"생성 경로 · {origin_names.get(row['group'], row['group'])} (n={int(row['n']):,})"
            if row["dimension"] == "origin"
            else f"부모 Sharpe · {row['group']} (n={int(row['n']):,})"
        ),
        axis=1,
    )
    shown = shown.sort_values(["dimension", "difference"])
    y = np.arange(len(shown))
    fig, ax = plt.subplots(figsize=(9.3, max(4.2, 0.48 * len(shown))))
    ax.errorbar(
        shown["difference"],
        y,
        xerr=[shown["difference"] - shown["ci_low"], shown["ci_high"] - shown["difference"]],
        fmt="o",
        color="#D17A22",
        ecolor="#C5A174",
        capsize=3,
    )
    ax.axvline(0, color="#7A8793", lw=1)
    ax.set_yticks(y, shown["label"])
    ax.set_xlabel("1축 변이의 개선률 차이(퍼센트포인트)")
    ax.xaxis.set_major_formatter(lambda value, _: f"{value * 100:.1f}")
    ax.grid(axis="x", color="#E7ECF1")
    fig.tight_layout()
    fig.savefig(out_dir / "fig3_internal_heterogeneity.png", bbox_inches="tight")
    plt.close(fig)


def plot_external(cells: pd.DataFrame, out_dir: Path) -> None:
    set_plot_style()
    matrix = cells.pivot(index=["function_id", "function_name"], columns="dimension", values="progress_difference")
    fig, ax = plt.subplots(figsize=(8.2, 10.5))
    values = matrix.to_numpy()
    bound = max(0.05, float(np.nanmax(np.abs(values))))
    image = ax.imshow(values, cmap="RdBu", vmin=-bound, vmax=bound, aspect="auto")
    ax.set_xticks(np.arange(len(matrix.columns)), [f"d={value}" for value in matrix.columns])
    ax.set_yticks(np.arange(len(matrix.index)), [f"F{idx} {name}" for idx, name in matrix.index], fontsize=8)
    for row in range(values.shape[0]):
        for col in range(values.shape[1]):
            ax.text(col, row, f"{values[row, col]:+.2f}", ha="center", va="center", fontsize=7, color="#16202A")
    colorbar = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.03)
    colorbar.set_label("정규화 진척도 차이(1축 − 다축)")
    ax.set_xlabel("문제 차원")
    fig.tight_layout()
    fig.savefig(out_dir / "fig4_external_pbo_heatmap.png", bbox_inches="tight")
    plt.close(fig)


def to_jsonable(value):
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
        snapshot = describe_snapshot(connection, args.db)
        raw = load_lineages(connection)
    primary, flow = cohort_flow(raw)
    by_k = descriptive_by_k(primary)
    effects = main_effects(primary)
    regressions = regression_results(primary)
    hetero = heterogeneity(primary)
    genes = single_gene_results(primary)
    strength_distribution = multi_strength_distribution(primary)

    by_k.to_csv(args.out / "internal_by_mutation_width.csv", index=False)
    hetero.to_csv(args.out / "internal_heterogeneity.csv", index=False)
    genes.to_csv(args.out / "internal_single_gene_results.csv", index=False)
    plot_dose_response(by_k, args.out)
    plot_effects(effects, regressions, args.out)
    plot_heterogeneity(hetero, args.out)

    summary = {
        "analysis_version": "2026-08-21.1",
        "bootstrap_seed": BOOTSTRAP_SEED,
        "benchmark_seed": BENCHMARK_SEED,
        "snapshot": snapshot,
        "cohort_flow": flow,
        "effects": effects,
        "regressions": regressions,
        "multi_strength_distribution": strength_distribution,
        "external": None,
    }

    if args.external:
        external_runs = args.out / "external_benchmark_runs.csv"
        if args.reuse_external and external_runs.exists():
            benchmark = pd.read_csv(external_runs)
        else:
            benchmark = run_external_benchmark(
                args.out,
                strength_distribution,
                args.benchmark_reps,
                args.benchmark_budget_multiplier,
            )
        external, cells = summarize_external(benchmark)
        cells.to_csv(args.out / "external_benchmark_cells.csv", index=False)
        plot_external(cells, args.out)
        summary["external"] = external

    with (args.out / "analysis_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(to_jsonable(summary), handle, ensure_ascii=False, indent=2)

    print(json.dumps(to_jsonable(summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
