"""Import-safe exchange-scoped single-flight coordinator for FC-Next C-A jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
import threading


@dataclass
class _ActiveJob:
    chat_ids: set[int] = field(default_factory=set)
    started_at_utc: str = ""


class ExchangeSingleFlight:
    """Run at most one FindCoin job per exchange and fan out its delivery.

    The coordinator has no Telegram, CandleView session, LLM, exchange, or
    persistence dependency.  A process restart naturally loses in-flight jobs;
    that C-A limitation is explicit and cannot mutate PHASE state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, _ActiveJob] = {}

    def start(
        self,
        *,
        chat_id: int,
        exchange_id: str,
        run_job: Callable[[], Any],
        deliver_result: Callable[[list[int], Any], None],
        deliver_failure: Callable[[list[int], Exception], None],
    ) -> str:
        exchange_id = str(exchange_id).lower().strip()
        with self._lock:
            existing = self._jobs.get(exchange_id)
            if existing is not None:
                existing.chat_ids.add(chat_id)
                return "joined"
            self._jobs[exchange_id] = _ActiveJob(
                chat_ids={chat_id}, started_at_utc=datetime.now(timezone.utc).isoformat()
            )
        threading.Thread(
            target=self._run,
            args=(exchange_id, run_job, deliver_result, deliver_failure),
            daemon=True,
        ).start()
        return "started"

    def _run(self, exchange_id, run_job, deliver_result, deliver_failure) -> None:
        try:
            result = run_job()
            with self._lock:
                chat_ids = list(self._jobs.get(exchange_id, _ActiveJob()).chat_ids)
            deliver_result(chat_ids, result)
        except Exception as exc:
            with self._lock:
                chat_ids = list(self._jobs.get(exchange_id, _ActiveJob()).chat_ids)
            deliver_failure(chat_ids, exc)
        finally:
            with self._lock:
                self._jobs.pop(exchange_id, None)

    def active_exchange_ids(self) -> set[str]:
        with self._lock:
            return set(self._jobs)
