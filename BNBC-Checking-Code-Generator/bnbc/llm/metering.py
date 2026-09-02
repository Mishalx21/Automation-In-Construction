"""Token accounting and the per-rule budget.

A :class:`TokenMeter` accumulates every call's usage under the node that made
it and raises :class:`BudgetExceeded` past ``TOKEN_BUDGET_PER_RULE``. The
meter round-trips through graph state as a plain dict (:meth:`to_state` /
:meth:`from_state`) so each node can reconstruct it.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from bnbc import config as cfg
from bnbc.llm.base import BudgetExceeded

logger = logging.getLogger("bnbc.llm.metering")


# ---------------------------------------------------------------------------
# Model pricing (bnbc/llm/model_prices.json)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _price_table() -> dict[str, dict[str, float]]:
    try:
        payload = json.loads(cfg.MODEL_PRICES_FILE.read_text(encoding="utf-8"))
        return dict(payload.get("prices", {}))
    except Exception as exc:
        logger.warning("model prices unavailable (%s); costs will be 0", exc)
        return {}


def call_cost_usd(model_name: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD cost of one call, or **None** when the model has no listed price.

    None, not 0.0: an unpriced model that silently costs nothing produces a
    provenance record claiming "$0.0000" for a run that really spent money,
    and that number is the kind of thing that ends up in a paper. Callers
    must decide what to do with an unknown; :class:`TokenMeter` records the
    model name so the gap is visible.
    """
    price = _price_table().get(model_name)
    if price is None:
        return None
    return (
        input_tokens * float(price.get("input_per_mtok", 0.0))
        + output_tokens * float(price.get("output_per_mtok", 0.0))
    ) / 1_000_000.0


# ---------------------------------------------------------------------------
# Meter
# ---------------------------------------------------------------------------

class TokenMeter:
    """Per-rule token accounting with a hard ceiling and a soft alert."""

    def __init__(
        self,
        budget: int | None = None,
        alert: int | None = None,
        per_node: dict[str, dict[str, Any]] | None = None,
    ):
        self.budget = int(budget) if budget is not None else cfg.TOKEN_BUDGET_PER_RULE
        self.alert = int(alert) if alert is not None else cfg.TOKEN_ALERT_PER_RULE
        self.per_node: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in (per_node or {}).items()
        }
        #: Models that answered but carry no listed price — the reported cost
        #: is a floor, not a total, and provenance must say so.
        self.unpriced_models: set[str] = set()
        self._alerted = self.total >= self.alert

    @classmethod
    def from_state(
        cls, token_usage: dict | None, budget: int | None = None, alert: int | None = None
    ) -> "TokenMeter":
        token_usage = token_usage or {}
        return cls(budget=budget, alert=alert, per_node=token_usage.get("per_node", {}))

    @property
    def total(self) -> int:
        return sum(
            int(n.get("input_tokens", 0)) + int(n.get("output_tokens", 0))
            for n in self.per_node.values()
        )

    @property
    def total_cost_usd(self) -> float:
        return round(sum(float(n.get("cost_usd", 0.0)) for n in self.per_node.values()), 6)

    def add(
        self,
        node: str,
        usage: dict | None,
        *,
        model_name: str = "",
        latency_ms: int = 0,
    ) -> None:
        """Record one call's usage under ``node``; raise past the hard budget.

        The usage is recorded *before* raising so nothing is lost on abort.
        """
        usage = usage or {}
        bucket = self.per_node.setdefault(
            node,
            {"input_tokens": 0, "output_tokens": 0, "calls": 0,
             "latency_ms": 0, "cost_usd": 0.0},
        )
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        bucket["input_tokens"] += in_tok
        bucket["output_tokens"] += out_tok
        bucket["calls"] = int(bucket.get("calls", 0)) + 1
        bucket["latency_ms"] = int(bucket.get("latency_ms", 0)) + int(latency_ms)
        cost = call_cost_usd(model_name, in_tok, out_tok)
        if cost is None:
            if model_name and model_name not in self.unpriced_models:
                self.unpriced_models.add(model_name)
                logger.warning(
                    "no listed price for %s — reported cost excludes it "
                    "(add it to bnbc/llm/model_prices.json)", model_name,
                )
        else:
            bucket["cost_usd"] = round(float(bucket.get("cost_usd", 0.0)) + cost, 6)

        total = self.total
        if total >= self.alert and not self._alerted:
            self._alerted = True
            logger.warning(
                "Token usage %d passed alert threshold %d (budget %d)",
                total, self.alert, self.budget,
            )
        if total > self.budget:
            raise BudgetExceeded(
                f"Token budget exceeded: {total} > {self.budget} (last node: {node})",
                usage=self.to_state(),
            )

    def to_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "per_node": {k: dict(v) for k, v in self.per_node.items()},
            "total": self.total,
            "total_cost_usd": self.total_cost_usd,
            "budget": self.budget,
        }
        if self.unpriced_models:
            # Never let a run report a cost that looks complete when it is not.
            state["cost_is_partial"] = True
            state["unpriced_models"] = sorted(self.unpriced_models)
        return state
