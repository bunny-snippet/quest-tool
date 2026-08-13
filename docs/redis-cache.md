# Redis cache

The operational and prescreener MySQL databases remain the source of truth. Redis only contains rebuildable, time-limited values and an outage must not block respondent journeys.

## Database separation

- Redis database `0`: Celery broker.
- Redis database `1`: Celery result backend.
- Redis database `2`: Django application cache.

Do not point the Django cache at database `0` or `1`. A Celery purge must never evict web caches, and clearing web caches must never delete queued work.

## Initial cached reads

The first cache integration accelerates the isolated prescreener vault:

- country, language, age-group, and gender filter options;
- filter-aware vault totals;
- bounded normalized reusable-profile snapshots by UID.

Submission rows, raw question/answer payloads, survey attempts, live status, quota reservations, and callbacks continue to read/write the authoritative databases directly. New submissions and usage-count changes increment a Redis namespace version, which invalidates every related cached value without scanning keys.

## Expiration policy

Every ordinary cache write receives a random TTL spread around its configured base. For example, a 900-second base with 180-second jitter expires independently between 720 and 1080 seconds. This avoids many keys expiring simultaneously and stampeding MySQL.

The namespace version key does not expire. Cache keys do not contain filter values or UID values; those inputs are hashed before becoming Redis key suffixes.

## Production environment

```dotenv
CACHE_ENABLED=true
REDIS_CACHE_URL=redis://127.0.0.1:6379/2
CACHE_KEY_PREFIX=quest-tool
CACHE_DEFAULT_TTL_SECONDS=900
CACHE_TTL_JITTER_SECONDS=180
CACHE_CONNECT_TIMEOUT_SECONDS=1
CACHE_SOCKET_TIMEOUT_SECONDS=1
CACHE_MAX_CONNECTIONS=100
VAULT_CACHE_OPTIONS_TTL_SECONDS=600
VAULT_CACHE_SUMMARY_TTL_SECONDS=180
VAULT_CACHE_PROFILE_TTL_SECONDS=900
```

Redis should listen only on localhost (or a private network with authentication and TLS). The short one-second timeouts intentionally fail open to MySQL rather than holding web workers during a Redis outage.

## Verification

After deploying and restarting Gunicorn, run:

```bash
.venv/bin/python manage.py cache_health
```

The command writes a 15-second probe, reads it back, deletes it, and reports the selected backend and a sample jittered TTL. It never flushes Redis.

One Redis service is sufficient initially. Adding 10–15 Redis containers on a single VPS would add coordination and memory overhead without increasing the database's throughput. Scale to a replica or Redis Cluster only after cache hit ratio, latency, memory, and eviction metrics demonstrate the need.
