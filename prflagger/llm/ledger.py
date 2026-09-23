"""What every model call cost, and the caps that stop the next one.

Three caps from `[budget]`: per run, per repository per UTC day, and in total.
Before a call, its worst case — every input token at full price plus the whole
`max_tokens` of output — is reserved against all three; a call that could breach
any of them is refused with `BudgetExceeded` and never sent. After it, the
provider's own usage is priced and recorded, and the reservation released.

Reservations are what make the caps hold under concurrency: two runs checking
at the same moment each see the other's call in flight, so neither can spend
the same remaining dollar.

Every call is a row in `llm_spend`, including cache hits at $0, so what the
disk cache saved is visible rather than assumed.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass
from typing import Any

from prflagger.core.config import BudgetConfig, ModelConfig
from prflagger.llm.provider import Completion, Request
from prflagger.storage.db import Database

__all__ = ["BudgetExceeded", "Ledger", "Reservation", "UnpricedModel"]

#: Multipliers on the input price. A 1-hour cache write costs twice the input
#: price, a 5-minute one 1.25x; reading from the cache costs a tenth.
_CACHE_WRITE = {"1h": 2.0, "5m": 1.25}
_CACHE_READ = 0.1


class BudgetExceeded(RuntimeError):
    """A call was refused before it was sent, because it could breach a cap."""

    def __init__(self, scope: str, cap: float, committed: float, needed: float) -> None:
        self.scope, self.cap, self.committed, self.needed = scope, cap, committed, needed
        super().__init__(
            f"{scope} budget: ${committed:.4f} of ${cap:.2f} already committed, and this "
            f"call could cost up to ${needed:.4f}"
        )


class UnpricedModel(BudgetExceeded):
    """A model with no configured price: a cap cannot be enforced on it."""

    def __init__(self, model: str) -> None:
        RuntimeError.__init__(
            self,
            f"no price is configured for {model!r}, so no budget can be enforced on it; "
            "add it under [models.prices] in config.toml",
        )
        self.scope, self.cap, self.committed, self.needed = "price", 0.0, 0.0, 0.0


@dataclass(frozen=True)
class Reservation:
    id: int
    usd: float
    run_id: str | None
    repo: str | None


class Ledger:
    def __init__(self, db: Database, budget: BudgetConfig, models: ModelConfig) -> None:
        self._db = db
        self._budget = budget
        self._prices = {model: (inp, out) for model, inp, out in models.prices}
        self._ttl = models.cache_ttl
        self._lock = threading.Lock()
        self._held: dict[int, Reservation] = {}
        self._next = 0

    # -- prices ---------------------------------------------------------------

    def price(self, model: str) -> tuple[float, float] | None:
        """USD per million (input, output) tokens, or None for an unpriced model."""
        return self._prices.get(model)

    def cost(self, completion: Completion, *, model: str | None = None) -> float:
        name = model or completion.model
        price = self.price(name) or self.price(completion.model)
        if price is None:
            return 0.0
        inp, out = price
        write = _CACHE_WRITE.get(self._ttl, 1.25)
        usd = (
            completion.input_tokens * inp
            + completion.cache_write_tokens * inp * write
            + completion.cache_read_tokens * inp * _CACHE_READ
            + completion.output_tokens * out
        ) / 1_000_000
        return round(usd, 6)

    def worst_case(self, request: Request) -> float:
        """The most `request` can cost: all input uncached-and-written, all output used.

        Input tokens are estimated at three characters each, which overcounts
        English and code alike — the right direction for a cap.
        """
        price = self.price(request.model)
        if price is None:
            raise UnpricedModel(request.model)
        inp, out = price
        chars = len(request.prompt) + sum(len(text) for text, _ in request.system)
        tokens = chars // 3 + 64
        write = _CACHE_WRITE.get(self._ttl, 1.25)
        return round((tokens * inp * max(1.0, write) + request.max_tokens * out) / 1_000_000, 6)

    # -- what has been spent ---------------------------------------------------

    def spent(self, *, run_id: str | None = None, repo: str | None = None,
              since: float | None = None) -> float:
        sql = "SELECT COALESCE(SUM(usd), 0) FROM llm_spend WHERE 1 = 1"
        params: list[Any] = []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if repo is not None:
            sql += " AND repo = ?"
            params.append(repo)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        return float(self._db.scalar(sql, params, default=0.0) or 0.0)

    @staticmethod
    def day_start(now: float | None = None) -> float:
        moment = dt.datetime.fromtimestamp(now or time.time(), tz=dt.UTC)
        return dt.datetime(moment.year, moment.month, moment.day, tzinfo=dt.UTC).timestamp()

    # -- reserve, record, release ---------------------------------------------

    def reserve(self, request: Request, *, run_id: str | None, repo: str | None
                ) -> Reservation:
        needed = self.worst_case(request)
        with self._lock:
            held = list(self._held.values())
            checks: list[tuple[str, float, float]] = [
                ("total", self._budget.total_usd,
                 self.spent() + sum(r.usd for r in held)),
            ]
            if repo is not None:
                checks.append((
                    f"daily {repo}", self._budget.per_repo_daily_usd,
                    self.spent(repo=repo, since=self.day_start())
                    + sum(r.usd for r in held if r.repo == repo),
                ))
            if run_id is not None:
                checks.append((
                    f"run {run_id}", self._budget.per_run_usd,
                    self.spent(run_id=run_id) + sum(r.usd for r in held if r.run_id == run_id),
                ))
            for scope, cap, committed in checks:
                if committed + needed > cap:
                    raise BudgetExceeded(scope, cap, committed, needed)
            self._next += 1
            reservation = Reservation(self._next, needed, run_id, repo)
            self._held[reservation.id] = reservation
            return reservation

    def release(self, reservation: Reservation) -> None:
        with self._lock:
            self._held.pop(reservation.id, None)

    def record(
        self,
        completion: Completion,
        *,
        model: str,
        stage: str,
        run_id: str | None,
        repo: str | None,
        cached: bool,
        reservation: Reservation | None = None,
    ) -> float:
        usd = 0.0 if cached else self.cost(completion, model=model)
        self._db.execute(
            """
            INSERT INTO llm_spend (ts, run_id, repo, model, stage, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens, usd, cached)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(), run_id, repo, model, stage, completion.input_tokens,
                completion.output_tokens, completion.cache_read_tokens,
                completion.cache_write_tokens, usd, 1 if cached else 0,
            ),
        )
        if reservation is not None:
            self.release(reservation)
        return usd

    def summary(self) -> dict[str, Any]:
        row = self._db.one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(cached), 0) AS cached,"
            " COALESCE(SUM(input_tokens), 0) AS input,"
            " COALESCE(SUM(output_tokens), 0) AS output,"
            " COALESCE(SUM(cache_read_tokens), 0) AS cache_read,"
            " COALESCE(SUM(cache_write_tokens), 0) AS cache_write FROM llm_spend"
        )
        return dict(row) if row else {}
