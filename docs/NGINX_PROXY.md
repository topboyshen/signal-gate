# Signal Gate — nginx high-entropy proxy (NOT enabled yet)

Service listens on **127.0.0.1:8095** only (localhost-first, same pattern as Quant).

## Suggested future location (do not enable until ready)

Follow existing relay high-entropy style under `/etc/nginx/sites-available/relay`:

```nginx
# ONLY add when intentionally exposing; keep path high-entropy and undocumented in public repos.
location /REPLACE_WITH_HIGH_ENTROPY_TOKEN/hooks/v1/pending-signals {
    proxy_pass http://127.0.0.1:8095/hooks/v1/pending-signals;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Content-Type $content_type;
    proxy_set_header X-Timestamp $http_x_timestamp;
    proxy_set_header X-Signature $http_x_signature;
    proxy_set_header X-Source $http_x_source;
    client_max_body_size 1m;
    access_log /var/log/nginx/signal-gate.access.log;
    error_log /var/log/nginx/signal-gate.error.log crit;
}
```

Until then, callers (JD / nnG8) should SSH-tunnel or use an existing private path to reach `127.0.0.1:8095`.

## Auth reminder

HMAC headers still required end-to-end; nginx must forward `X-Timestamp`, `X-Signature`, `X-Source` unchanged.
