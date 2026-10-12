"""Background data refresh for the hosted backend.

One daemon thread wakes every few hours and refreshes any data source whose
snapshot is older than a threshold (two weeks by default). Because it is one
thread working through the sources in order, only one refresh ever runs at a
time. Requests keep using the current data the whole time; new data is swapped
in only after it has been fetched and sanity-checked, so a failed or
suspiciously small fetch can never replace good data.

Disable with DATA_REFRESH=0. Tune with DATA_MAX_AGE_DAYS (default 14) and
DATA_CHECK_INTERVAL_HOURS (default 6).

The container's disk is wiped on every deploy or restart, so refreshed data
lives only in memory: after a restart the app starts from the data baked into
the image and refreshes again if that is stale. Shipping fresh data in the
image (see refresh_data.py and redeploy.sh) avoids that.
"""

import gc
import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Callable

log = logging.getLogger("uvicorn.error")


class Source:
    def __init__(self, name: str, as_of: date | None, fetch: Callable[[], object],
                 validate: Callable[[object], bool], apply: Callable[[object], None],
                 enabled: bool = True, disabled_reason: str = ""):
        self.name = name
        self.as_of = as_of
        self.fetch = fetch
        self.validate = validate
        self.apply = apply
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self.refreshed_at: str | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.next_attempt = 0.0

    def age_days(self) -> int | None:
        return (date.today() - self.as_of).days if self.as_of else None


class Refresher:
    def __init__(self, sources: list[Source], max_age_days: int | None = None,
                 check_interval_hours: float | None = None, first_delay_seconds: float = 60):
        self.sources = sources
        self.max_age_days = max_age_days if max_age_days is not None else int(os.environ.get("DATA_MAX_AGE_DAYS", "14"))
        hours = check_interval_hours if check_interval_hours is not None else float(os.environ.get("DATA_CHECK_INTERVAL_HOURS", "6"))
        self.check_interval = hours * 3600
        self.first_delay = first_delay_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def enabled_by_env() -> bool:
        return os.environ.get("DATA_REFRESH", "1") != "0"

    def start(self) -> None:
        if not self.enabled_by_env():
            log.info("Background data refresh disabled (DATA_REFRESH=0).")
            return
        self._thread = threading.Thread(target=self._run, name="data-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(self.first_delay):
            return
        while not self._stop.is_set():
            self.check_all()
            if self._stop.wait(self.check_interval):
                return

    def check_all(self) -> None:
        for src in self.sources:
            if self._stop.is_set():
                return
            self.maybe_refresh(src)

    def maybe_refresh(self, src: Source, force: bool = False) -> bool:
        """Refresh one source if it is stale. Returns True if data was swapped."""
        if not src.enabled:
            return False
        age = src.age_days()
        if not force and age is not None and age < self.max_age_days:
            return False
        if not force and time.time() < src.next_attempt:
            return False
        started = time.time()
        try:
            log.info("Refreshing %s data (age: %s days)...", src.name, age)
            new = src.fetch()
            if not src.validate(new):
                raise RuntimeError("fetched data failed the sanity check")
            src.apply(new)
            src.as_of = date.today()
            src.refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            src.failures = 0
            src.last_error = None
            log.info("Refreshed %s data in %.0fs.", src.name, time.time() - started)
            return True
        except Exception as exc:  # keep serving the old data; retry later with backoff
            src.failures += 1
            src.last_error = f"{type(exc).__name__}: {exc}"[:200]
            src.next_attempt = time.time() + min(3600 * 2 ** (src.failures - 1), 24 * 3600)
            log.warning("Refreshing %s failed (%s); keeping current data.", src.name, src.last_error)
            return False
        finally:
            gc.collect()

    def status(self) -> dict:
        return {
            "enabled": self.enabled_by_env(),
            "max_age_days": self.max_age_days,
            "sources": {
                s.name: {
                    "as_of": s.as_of.isoformat() if s.as_of else None,
                    "age_days": s.age_days(),
                    "refreshed_at": s.refreshed_at,
                    "last_error": s.last_error,
                    **({"disabled": s.disabled_reason} if not s.enabled else {}),
                }
                for s in self.sources
            },
        }
