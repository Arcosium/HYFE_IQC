"""Typed genome generators for HYFE IQC alpha search — genetic-algorithm core.

The split is by account type:
- StandardGenomeModel: non-RC accounts. It shares datafields but targets the
  Playwright/browser path and relies on browser self-correlation gating.
- ResearchConsultantGenomeModel: RC accounts. It renders stricter API-safe
  FASTEXPR and assumes the API backend will submit-attempt every completed alpha.

Population composition (진짜 GA):
- 탐색 라운드: seed(엘리트) 유전체가 있으면 교차(crossover)/변이(mutate) 자식 절반
  + 무작위 탐색 절반. seed 가 없으면 전량 무작위.
- focus 라운드: 부모 유전체를 fail 사유(정향, directed mutation)에 따라 변이.
  seed 가 있으면 마지막 2 슬롯은 부모×seed 교차.
- 밴딧 arm(slot_settings)은 '무작위 탐색' 슬롯의 settings 유전자에만 주입한다 —
  GA 자식은 부모에게서 settings 를 유전받는 것이 목적이므로 덮어쓰지 않는다.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import random
import re
import zlib
from typing import Iterable

SHARED_DATASETS = {
    "pv": ("close", "open", "high", "low", "vwap", "volume", "returns", "adv20", "cap"),
    "fundamental": ("net_income_adjusted", "assets", "equity", "debt", "liabilities", "cashflow_op", "cashflow", "cash", "dividend"),
    "analyst": ("anl4_bvps_mean", "anl4_netdebt_mean", "anl4_adjusted_netincome_ft", "anl4_afv4_eps_mean", "anl4_afv4_eps_high", "anl4_afv4_eps_low", "anl4_afv4_eps_number"),
    "option": ("implied_volatility_call_120", "implied_volatility_put_120", "implied_volatility_mean_150", "implied_volatility_mean_skew_150", "historical_volatility_120", "mdl77_voldiff_pc"),
    "news": ("nws12_afterhsz_result2", "nws12_afterhsz_vol_ratio", "nws12_allz_result2", "news_dividend_yield"),
}
VECTOR_FIELDS = frozenset({"nws12_afterhsz_result2", "nws12_afterhsz_vol_ratio", "nws12_allz_result2"})
UNIVERSES = ("TOP3000", "TOP1000", "TOP500", "TOP200")
NEUTRALIZATIONS = ("INDUSTRY", "SECTOR", "SUBINDUSTRY", "MARKET", "NONE")
GROUPS = {"INDUSTRY": "industry", "SECTOR": "sector", "SUBINDUSTRY": "subindustry"}

_FAMILY_OF_FIELD = {f: fam for fam, fs in SHARED_DATASETS.items() for f in fs}
_ALL_FIELDS_RX = re.compile(
    r"\b(" + "|".join(sorted(_FAMILY_OF_FIELD, key=len, reverse=True)) + r")\b")
_TRANSFORM_RX = re.compile(r"\b(ts_rank|ts_zscore|ts_delta|ts_mean|rank)\s*\(")
_TS_WINDOW_RX = re.compile(r"\bts_\w+\([^()]*?,\s*(\d+)\s*\)")


@dataclass(frozen=True)
class Genome:
    model: str
    family: str
    fields: tuple[str, str, str]
    transform_a: str
    transform_b: str
    combine: str
    sign: int
    lookback_a: int
    lookback_b: int
    universe: str
    neutralization: str
    decay: int
    truncation: float
    nan_handling: str = "OFF"
    generation: int = 0


_GENE_NAMES = tuple(f.name for f in dataclasses.fields(Genome))


def _coerce_genome(obj) -> Genome | None:
    """dict/Genome → Genome. 알 수 없는 키는 버리고 누락 키는 기본값으로 채운다."""
    if obj is None:
        return None
    if isinstance(obj, Genome):
        return obj
    if not isinstance(obj, dict):
        return None
    try:
        d = {k: obj[k] for k in _GENE_NAMES if k in obj}
        d.setdefault("model", "seed")
        d.setdefault("family", "pv")
        flds = tuple(d.get("fields") or ())
        if len(flds) != 3:
            flds = tuple(SHARED_DATASETS["pv"][:3])
        d["fields"] = tuple(str(f) for f in flds)
        d.setdefault("transform_a", "rank")
        d.setdefault("transform_b", "ts_zscore")
        d.setdefault("combine", "sum")
        d["sign"] = -1 if int(d.get("sign") or 1) < 0 else 1
        d["lookback_a"] = max(1, min(252, int(d.get("lookback_a") or 20)))
        d["lookback_b"] = max(1, min(252, int(d.get("lookback_b") or 60)))
        d.setdefault("universe", "TOP3000")
        d.setdefault("neutralization", "INDUSTRY")
        d["decay"] = max(0, min(30, int(d.get("decay") or 0)))
        d["truncation"] = max(0.01, min(0.15, float(d.get("truncation") or 0.08)))
        d.setdefault("nan_handling", "OFF")
        d["generation"] = int(d.get("generation") or 0)
        return Genome(**d)
    except Exception:
        return None


def genome_from_alpha(code: str, settings: dict | None = None) -> dict:
    """저장된 알파(code+settings)에서 유전체를 역추출한다 (best-effort).

    renderer 산출물이면 거의 정확하고, 레거시(Gemini) 알파도 필드/연산자만
    건져 교차 재료로 쓸 수 있다. 결정론(zlib.crc32 시드)이라 재시작에도 안전.
    """
    code = str(code or "")
    st = dict(settings or {})
    rng = random.Random(zlib.crc32(code.encode("utf-8")))
    found = list(dict.fromkeys(_ALL_FIELDS_RX.findall(code)))
    family = _FAMILY_OF_FIELD.get(found[0], "pv") if found else "pv"
    pool = [f for f in SHARED_DATASETS.get(family, SHARED_DATASETS["pv"]) if f not in found]
    rng.shuffle(pool)
    while len(found) < 3:
        found.append(pool.pop() if pool else rng.choice(SHARED_DATASETS["pv"]))
    trs = _TRANSFORM_RX.findall(code)
    # render() 는 마지막에 rank() 로 감싸므로 첫 토큰이 rank 인 것은 래퍼일 확률이 높다.
    inner = [t for t in trs[1:]] or trs
    ta = inner[0] if inner else "rank"
    tb = inner[1] if len(inner) > 1 else ta
    if "ts_corr(" in code:
        combine = "corr"
    elif "/(" in code.replace(" ", ""):
        combine = "ratio"
    elif re.search(r"\)\s*\*\s*", code) and "-1*(" not in code.replace(" ", ""):
        combine = "product"
    elif re.search(r"\)\s*-\s*", code):
        combine = "spread"
    else:
        combine = "sum"
    windows = [int(w) for w in _TS_WINDOW_RX.findall(code)[:2]]
    la = windows[0] if windows else 20
    lb = windows[1] if len(windows) > 1 else max(la, 60)
    try:
        decay = int(float(st.get("decay") or 0))
    except (TypeError, ValueError):
        decay = 0
    try:
        trunc = float(st.get("truncation") or 0.08)
    except (TypeError, ValueError):
        trunc = 0.08
    g = _coerce_genome({
        "model": "seed",
        "family": family,
        "fields": tuple(found[:3]),
        "transform_a": ta,
        "transform_b": tb,
        "combine": combine,
        "sign": -1 if "-1*" in code.replace(" ", "") else 1,
        "lookback_a": la,
        "lookback_b": lb,
        "universe": str(st.get("universe") or "TOP3000"),
        "neutralization": str(st.get("neutralization") or "INDUSTRY"),
        "decay": decay,
        "truncation": trunc,
        "nan_handling": str(st.get("nan_handling") or "OFF"),
        "generation": 0,
    })
    return dict(g.__dict__)


def _field_expr(field: str) -> str:
    return f"vec_avg({field})" if field in VECTOR_FIELDS else field


def _transform(expr: str, kind: str, window: int) -> str:
    if kind == "rank":
        return f"rank({expr})"
    if kind == "ts_rank":
        return f"ts_rank({expr},{window})"
    if kind == "ts_zscore":
        return f"ts_zscore({expr},{window})"
    if kind == "ts_delta":
        return f"ts_delta({expr},{max(1, min(window, 20))})"
    if kind == "ts_mean":
        return f"ts_mean({expr},{window})"
    return f"rank({expr})"


def _combine(a: str, b: str, c: str, kind: str, window: int) -> str:
    if kind == "spread":
        return f"({a}-{b})"
    if kind == "sum":
        return f"add({a},{b})"
    if kind == "triple":
        return f"add(add({a},{b}),{c})"
    if kind == "product":
        return f"({a}*{b})"
    if kind == "ratio":
        return f"({a}/(abs({b})+0.000001))"
    if kind == "corr":
        return f"ts_corr({a},{b},{window})"
    return f"add({a},{b})"


def render(genome: Genome) -> str:
    f1, f2, f3 = (_field_expr(f) for f in genome.fields)
    a = _transform(f1, genome.transform_a, genome.lookback_a)
    b = _transform(f2, genome.transform_b, genome.lookback_b)
    c = _transform(f3, "ts_zscore", max(20, genome.lookback_b))
    core = _combine(a, b, c, genome.combine, genome.lookback_b)
    if genome.sign < 0:
        core = f"-1*({core})"
    group = GROUPS.get(genome.neutralization)
    if group and genome.combine != "corr":
        core = f"group_neutralize({core},{group})"
    if genome.decay >= 8:
        core = f"ts_mean({core},{min(genome.decay, 30)})"
    return f"rank({core})"


def settings(genome: Genome, forced_delay=None) -> dict:
    out = {
        "universe": genome.universe,
        "neutralization": genome.neutralization,
        "decay": str(genome.decay),
        "truncation": str(genome.truncation),
        "nan_handling": genome.nan_handling,
    }
    if forced_delay is not None:
        out["delay"] = str(forced_delay)
    return out


def _error_tokens(errors: Iterable[dict] | None) -> set[str]:
    toks: set[str] = set()
    for e in errors or []:
        for ident in e.get("identifiers") or []:
            toks.add(str(ident))
        pat = str(e.get("pattern") or "")
        for fam in SHARED_DATASETS.values():
            for f in fam:
                if f in pat:
                    toks.add(f)
    return toks


def _pick_fields(rng: random.Random, family: str, forbidden: set[str], delay) -> tuple[str, str, str]:
    if str(delay) == "0":
        pool = list(SHARED_DATASETS["pv"])
    else:
        pool = list(SHARED_DATASETS.get(family) or SHARED_DATASETS["pv"])
        if len(pool) < 3:
            pool += list(SHARED_DATASETS["pv"])
    pool = [f for f in pool if f not in forbidden]
    if len(pool) < 3:
        pool = list(SHARED_DATASETS["pv"])
    rng.shuffle(pool)
    return (pool[0], pool[1], pool[2])


# ── directed mutation: fail 사유 → 어느 유전자 축을 움직일지 ─────────────────

def _directives(fail_items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for it in fail_items or []:
        s = str(it).lower()
        # 순서 중요: 'LOW_SUB_UNIVERSE_SHARPE' 는 sharpe 이전에 sub-universe 로 잡아야 한다.
        if "sub" in s and ("universe" in s or "sharpe" in s):
            out.append("universe")
        elif "correlation" in s or "self-corr" in s or "self corr" in s:
            out.append("decorrelate")
        elif "turnover" in s:
            out.append("sharpen" if ("<" in s or "low_turnover" in s) else "smooth")
        elif "weight" in s or "concentr" in s:
            out.append("concentration")
        elif any(k in s for k in ("sharpe", "fitness", "returns", "margin", "drawdown")):
            out.append("signal")
    return out


class BaseGenomeModel:
    name = "base"
    transforms = ("rank", "ts_rank", "ts_zscore", "ts_delta", "ts_mean")
    combines = ("spread", "sum", "product", "ratio", "corr", "triple")
    families = ("pv", "fundamental", "analyst", "option", "news")
    decays = (0, 2, 4, 6, 8, 12, 20)
    truncations = (0.08, 0.1, 0.12)

    def __init__(self, *, round_num: int, forced_delay=None, errors=None, feedback=None,
                 parent_genome=None, fail_items=None, seed_genomes=None,
                 slot_settings=None, salt: int = 0):
        self.round_num = int(round_num or 0)
        self.forced_delay = forced_delay
        self.feedback = feedback or []
        self.forbidden = _error_tokens(errors)
        self.parent = _coerce_genome(parent_genome)
        self.fail_items = [str(x) for x in (fail_items or []) if str(x).strip()]
        self.seeds = [g for g in (_coerce_genome(s) for s in (seed_genomes or [])) if g is not None]
        self.slot_settings = list(slot_settings or [])
        self.salt = int(salt or 0)

    def _rng(self, nonce: int) -> random.Random:
        seed = ((self.round_num + 1) * 1009 + nonce * 9173
                + self.salt * 7919 + sum(ord(c) for c in self.name))
        return random.Random(seed)

    # ── population plan ──────────────────────────────────────
    def _plan(self, n: int) -> list[tuple[str, Genome | None, Genome | None]]:
        if self.parent is not None:
            plan: list[tuple[str, Genome | None, Genome | None]] = [
                ("mutate", self.parent, None)] * n
            # 마지막 2 슬롯은 부모×엘리트 교차 — 새 유전자 유입 통로.
            for j in range(min(2, len(self.seeds), max(0, n - 2))):
                plan[n - 1 - j] = ("crossover", self.parent, self.seeds[j])
            return plan
        plan = []
        if self.seeds:
            k = min(n // 2, max(2, len(self.seeds)))
            for j in range(k):
                a = self.seeds[j % len(self.seeds)]
                b = self.seeds[(j + 1) % len(self.seeds)]
                if len(self.seeds) >= 2 and j % 2 == 0:
                    plan.append(("crossover", a, b))
                else:
                    plan.append(("mutate", a, None))
        while len(plan) < n:
            plan.append(("random", None, None))
        return plan

    def generate(self, n: int = 8) -> list[dict]:
        plan = self._plan(n)
        # 밴딧 arm 은 무작위 탐색 슬롯에만 순서대로 배정 (GA 자식은 settings 를 유전).
        arms_by_slot: dict[int, dict] = {}
        ai = 0
        for si, (op, _, _) in enumerate(plan):
            if op == "random" and ai < len(self.slot_settings):
                arms_by_slot[si] = self.slot_settings[ai]
                ai += 1

        out: list[dict] = []
        seen: set[str] = set()
        if self.parent is not None:
            # 부모 그대로(무변이) 재출현 방지 — 이미 시뮬한 조합.
            seen.add(self._dedup_key(self.parent))
        i = 0
        while len(out) < n and i < n * 10:
            i += 1
            rng = self._rng(i)
            slot = len(out)
            op, a, b = plan[slot]
            if op == "mutate":
                # 정향변이의 유전자 공간이 작아 중복이 계속되면(시도 예산 절반 소진)
                # 지시 없는 일반 변이로 강등해 슬롯을 채운다 — 세대가 8개 미만이면
                # 시뮬 예산이 놀게 되기 때문.
                g = self._mutate(a, rng, directed=(i <= n * 5))
            elif op == "crossover":
                g = self._crossover(a, b, rng)
            else:
                g = self._genome(i, rng)
                arm = arms_by_slot.get(slot)
                if arm:
                    g = self._apply_arm(g, arm)
            g = self._constrain(g, rng)
            key = self._dedup_key(g)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "idx": len(out) + 1,
                "code": render(g),
                "desc": self._desc(g, op),
                "settings": settings(g, self.forced_delay),
                "genome": dict(g.__dict__),
                "generation": g.generation,
                "origin": op,
            })
        return out

    @staticmethod
    def _dedup_key(g: Genome) -> str:
        # 코드가 같아도 settings 유전자가 다르면 다른 후보다 (settings 스윕 자식 보존).
        return (f"{render(g)}|{g.universe}|{g.neutralization}|{g.decay}"
                f"|{g.truncation}|{g.nan_handling}")

    # ── GA operators ─────────────────────────────────────────
    def _mutate(self, parent: Genome, rng: random.Random, directed: bool = True) -> Genome:
        d = dict(parent.__dict__)
        dirs = _directives(self.fail_items) if directed else []
        directive = rng.choice(dirs) if dirs else None
        if directive == "smooth":       # turnover 과다 → 스무딩 유전자 강화
            d["decay"] = max(int(d["decay"]), rng.choice((8, 12, 20)))
            d["lookback_a"] = min(252, int(d["lookback_a"]) * rng.choice((2, 3)))
            d["lookback_b"] = min(252, int(d["lookback_b"]) * rng.choice((2, 3)))
            if rng.random() < 0.5 and "ts_mean" in self.transforms:
                d["transform_b"] = "ts_mean"
        elif directive == "sharpen":    # turnover 과소 → 신호 민감도 강화
            d["decay"] = rng.choice((0, 2))
            d["lookback_a"] = rng.choice((5, 10, 20))
            if rng.random() < 0.5 and "ts_delta" in self.transforms:
                d["transform_a"] = "ts_delta"
        elif directive == "concentration":  # weight 집중 → 분산 유전자
            d["neutralization"] = rng.choice(("SUBINDUSTRY", "INDUSTRY"))
            d["universe"] = "TOP3000"
            d["truncation"] = 0.08
        elif directive == "universe":   # sub-universe sharpe → 큰 유니버스
            d["universe"] = "TOP3000"
            if d["neutralization"] in ("NONE", "MARKET"):
                d["neutralization"] = rng.choice(("SECTOR", "INDUSTRY"))
        elif directive == "decorrelate":  # self-corr → 다른 패밀리/조합으로 탈상관
            fam_pool = [f for f in self.families if f != d.get("family")] or list(self.families)
            fam = rng.choice(fam_pool)
            d["family"] = fam
            d["fields"] = _pick_fields(rng, fam, self.forbidden, self.forced_delay)
            d["combine"] = rng.choice(self.combines)
        else:                           # signal 미달 or 사유 불명 → 신호 유전자 무작위 변이
            for gene in rng.sample(
                    ("fields", "transform_a", "transform_b", "combine",
                     "sign", "lookback_a", "lookback_b"),
                    k=rng.choice((1, 2))):
                if gene == "fields":
                    d["fields"] = _pick_fields(rng, d.get("family") or "pv",
                                               self.forbidden, self.forced_delay)
                elif gene == "transform_a":
                    d["transform_a"] = rng.choice(self.transforms)
                elif gene == "transform_b":
                    d["transform_b"] = rng.choice(self.transforms)
                elif gene == "combine":
                    d["combine"] = rng.choice(self.combines)
                elif gene == "sign":
                    d["sign"] = -d["sign"]
                elif gene == "lookback_a":
                    d["lookback_a"] = rng.choice((5, 10, 20, 40, 60, 120))
                else:
                    d["lookback_b"] = rng.choice((10, 20, 40, 60, 120, 252))
        d["model"] = self.name
        d["generation"] = int(parent.generation or 0) + 1
        return Genome(**d)

    def _crossover(self, a: Genome, b: Genome, rng: random.Random) -> Genome:
        d = {}
        for gene in _GENE_NAMES:
            d[gene] = getattr(a if rng.random() < 0.5 else b, gene)
        fa, fb = list(a.fields), list(b.fields)
        mixed = tuple((fa[i] if rng.random() < 0.5 else fb[i]) for i in range(3))
        if len(set(mixed)) < 3:
            fam = d.get("family") or a.family
            mixed = _pick_fields(rng, fam, self.forbidden, self.forced_delay)
        d["fields"] = mixed
        d["family"] = _FAMILY_OF_FIELD.get(mixed[0], a.family)
        d["model"] = self.name
        d["generation"] = max(int(a.generation or 0), int(b.generation or 0)) + 1
        return Genome(**d)

    def _apply_arm(self, g: Genome, arm: dict) -> Genome:
        d = dict(g.__dict__)
        if arm.get("universe"):
            d["universe"] = str(arm["universe"])
        if arm.get("neutralization"):
            d["neutralization"] = str(arm["neutralization"])
        if arm.get("decay") is not None:
            try:
                d["decay"] = max(0, min(30, int(arm["decay"])))
            except (TypeError, ValueError):
                pass
        return Genome(**d)

    def _constrain(self, g: Genome, rng: random.Random) -> Genome:
        """모델 불변식 재적용 — mutate/crossover/seed 유입 후에도 항상 성립해야 한다."""
        d = dict(g.__dict__)
        if d["transform_a"] not in self.transforms:
            d["transform_a"] = rng.choice(self.transforms)
        if d["transform_b"] not in self.transforms:
            d["transform_b"] = rng.choice(self.transforms)
        if d["combine"] not in self.combines:
            d["combine"] = rng.choice(self.combines)
        if d["universe"] not in UNIVERSES:
            d["universe"] = "TOP3000"
        if d["neutralization"] not in NEUTRALIZATIONS:
            d["neutralization"] = "INDUSTRY"
        pv = set(SHARED_DATASETS["pv"])
        if str(self.forced_delay) == "0" and not all(f in pv for f in d["fields"]):
            d["fields"] = _pick_fields(rng, "pv", self.forbidden, "0")
            d["family"] = "pv"
        if self.forbidden and any(f in self.forbidden for f in d["fields"]):
            d["fields"] = _pick_fields(rng, d.get("family") or "pv",
                                       self.forbidden, self.forced_delay)
        d["model"] = self.name
        return Genome(**d)

    def _genome(self, slot: int, rng: random.Random) -> Genome:
        family = self.families[(slot - 1) % len(self.families)]
        fields = _pick_fields(rng, family, self.forbidden, self.forced_delay)
        return Genome(
            model=self.name,
            family=family,
            fields=fields,
            transform_a=rng.choice(self.transforms),
            transform_b=rng.choice(self.transforms),
            combine=rng.choice(self.combines),
            sign=-1 if rng.random() < 0.55 else 1,
            lookback_a=rng.choice((5, 10, 20, 40, 60, 120)),
            lookback_b=rng.choice((10, 20, 40, 60, 120, 252)),
            universe=rng.choice(UNIVERSES),
            neutralization=rng.choice(NEUTRALIZATIONS),
            decay=rng.choice(self.decays),
            truncation=rng.choice(self.truncations),
            nan_handling="ON" if family in ("fundamental", "analyst", "option", "news") else "OFF",
            generation=0,
        )

    def _desc(self, g: Genome, origin: str = "random") -> str:
        tag = {"mutate": "mut", "crossover": "xo", "random": "rand"}.get(origin, origin)
        gen = f" g{g.generation}" if g.generation else ""
        return (f"{self.name} {g.family}: {g.combine}/{g.transform_a}+{g.transform_b} "
                f"{g.universe}x{g.neutralization} [{tag}{gen}]")


class StandardGenomeModel(BaseGenomeModel):
    name = "standard-playwright-genome"

    def _constrain(self, g: Genome, rng: random.Random) -> Genome:
        g = super()._constrain(g, rng)
        return Genome(**{**g.__dict__,
                         "decay": max(g.decay, 4),
                         "truncation": max(g.truncation, 0.08)})


class ResearchConsultantGenomeModel(BaseGenomeModel):
    name = "rc-api-genome"
    transforms = ("rank", "ts_rank", "ts_zscore", "ts_delta")
    combines = ("spread", "sum", "product", "ratio", "corr")
    decays = (0, 2, 4, 6, 8)
    truncations = (0.08, 0.1)

    def _constrain(self, g: Genome, rng: random.Random) -> Genome:
        g = super()._constrain(g, rng)
        d = dict(g.__dict__)
        if d["neutralization"] == "NONE":
            d["neutralization"] = "MARKET"
        d["decay"] = min(int(d["decay"]), 8)
        d["truncation"] = min(max(float(d["truncation"]), 0.08), 0.1)
        d["nan_handling"] = "OFF"
        return Genome(**d)


def generate_population(*, account_type: str, round_num: int, forced_delay=None,
                        errors=None, feedback=None, n: int = 8,
                        parent_genome=None, fail_items=None, seed_genomes=None,
                        slot_settings=None, salt: int = 0) -> list[dict]:
    cls = (ResearchConsultantGenomeModel
           if account_type == "research_consultant" else StandardGenomeModel)
    return cls(round_num=round_num, forced_delay=forced_delay, errors=errors,
               feedback=feedback, parent_genome=parent_genome, fail_items=fail_items,
               seed_genomes=seed_genomes, slot_settings=slot_settings,
               salt=salt).generate(n=n)
