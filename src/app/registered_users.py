"""Expose the total number of registered users (rows in `users`) as a Prometheus
gauge. Users are never deleted, so this only ever grows.

Updated in two ways, neither of which adds a dedicated DB query:
- incremented in-process when /register inserts a genuinely new row (detected
  via `xmax = 0` on the upsert's RETURNING clause, so returning users who just
  refresh their token/channels don't bump the count);
- set from the row count the periodic full sync (user_sync.py) already fetches,
  as a correctness backstop in case a pod restarts and loses the in-process count.
"""

from prometheus_client import Gauge

REGISTERED_USERS = Gauge(
    "j26_notifications_registered_users",
    "Total number of users ever registered (rows in the users table); never decreases",
)


def record_new_registration() -> None:
    REGISTERED_USERS.inc()


def set_registered_count(count: int) -> None:
    REGISTERED_USERS.set(count)
