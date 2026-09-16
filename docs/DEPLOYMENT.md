# Verified Query Service — Deployment Handoff

**For:** Muiz · **Host:** `genemco-harvester` (permanent RAG VM) · **Listens on:** `127.0.0.1:9000`
**Worker upstream after cut-over:** `http://127.0.0.1:9000/query`

This service answers equipment questions from golden-record data on disk. At runtime it
needs **no internet access and no third-party API keys** — no OpenAI, Pinecone or Shopify.
The only secret is one shared token, which you generate on the VM in step 2.5. It never
leaves the box.

Every command below is copy-pasteable. Run the steps in order.

---

## 0. At a glance

| | |
|---|---|
| Python | 3.12 (VM has 3.12.3) |
| Bind address | `127.0.0.1:9000` — loopback only, not reachable from outside the VM |
| Endpoint | `POST /query` |
| Health check | `GET /health` |
| Auth header | `X-Upstream-Token: <token>` |
| Install path | `/opt/genemco-query` |
| Config file | `/etc/genemco-query/query.env` |
| Service | `systemd` unit `genemco-query` |
| Disk | ~32 MB data + ~60 MB virtualenv |
| Memory | ~200 MB once loaded |
| Startup | about 1–2 seconds |

---

## 1. What you receive

Two files, sent to you directly:

```text
genemco-query-service-20260914-bc6facf.tar.gz          the bundle
genemco-query-service-20260914-bc6facf.tar.gz.sha256   its checksum
```

The bundle contains **no secrets**. Contents once unpacked:

```text
genemco-query-service/
├── api/               __init__.py  query.py  query_app.py
├── core/              __init__.py  config.py  logging_utils.py  schemas.py
├── faq/               __init__.py  validator.py
├── ingestion/         __init__.py  golden_record.py
├── retrieval/         __init__.py  verified_query.py
├── data/
│   ├── golden_records/                      20 *.json   ~60 KB   manual-derived records
│   ├── search_layer/
│   │   └── catalog_golden_records.jsonl     30.4 MB             15,939 catalog records
│   └── shopify_raw/                         empty (created by config at startup)
├── logs/                                    empty
├── docs/              DEPLOYMENT.md (this file)  QUERY_API.md
├── requirements-query.txt
├── VERSION
└── MANIFEST.sha256                          checksum of every file above
```

**The data is already inside the bundle** — there is nothing else to copy. After unpacking,
the service reads:

| File | Path on the VM |
|---|---|
| Catalog records (~30 MB) | `/opt/genemco-query/data/search_layer/catalog_golden_records.jsonl` |
| Authoritative records | `/opt/genemco-query/data/golden_records/*.json` |

---

## 2. Deploy

Set the bundle name once for this shell session. Every later command uses it.

```bash
BUNDLE=genemco-query-service-20260914-bc6facf
```

### 2.1 Prerequisites

```bash
python3.12 --version            # expect Python 3.12.x
python3.12 -m venv --help > /dev/null && echo "venv module OK"
```

If the `venv` check fails on Ubuntu or Debian:

```bash
sudo apt-get update && sudo apt-get install -y python3.12-venv
```

### 2.2 Verify and unpack

From the directory where you saved both files:

```bash
sha256sum -c "${BUNDLE}.tar.gz.sha256"          # expect: <file>: OK

sudo mkdir -p /opt/genemco-query
sudo tar -xzf "${BUNDLE}.tar.gz" -C /opt/genemco-query --strip-components=1

cd /opt/genemco-query
sha256sum -c --quiet MANIFEST.sha256 && echo "bundle contents OK"
cat VERSION
```

### 2.3 Service user

A dedicated system account with no login shell:

```bash
sudo useradd --system --home-dir /opt/genemco-query --shell /usr/sbin/nologin genemco-query
sudo chown -R genemco-query:genemco-query /opt/genemco-query
```

### 2.4 Virtualenv and dependencies

```bash
sudo -u genemco-query python3.12 -m venv /opt/genemco-query/venv
sudo -u genemco-query /opt/genemco-query/venv/bin/pip install --upgrade pip
sudo -u genemco-query /opt/genemco-query/venv/bin/pip install -r /opt/genemco-query/requirements-query.txt
```

This installs four packages (`fastapi`, `uvicorn[standard]`, `pydantic`, `python-dotenv`)
plus their dependencies. It is the only step that needs internet access.

### 2.5 Configuration and the shared token

This generates the token **on the VM** and writes it straight into the config file. The
value is never printed to the screen and never needs to be sent anywhere.

```bash
sudo install -d -m 750 -o root -g genemco-query /etc/genemco-query

TOKEN="$(python3.12 -c 'import secrets; print(secrets.token_urlsafe(32))')"
sudo tee /etc/genemco-query/query.env > /dev/null <<EOF
GENEMCO_API_HOST=127.0.0.1
GENEMCO_API_PORT=9000
QUERY_UPSTREAM_TOKEN=${TOKEN}
GENEMCO_GOLDEN_DIR=/opt/genemco-query/data/golden_records
GENEMCO_CATALOG_PATH=/opt/genemco-query/data/search_layer/catalog_golden_records.jsonl
GENEMCO_MANUAL_URLS=/opt/genemco-query/data/manual_urls.json
GENEMCO_FORWARDED_ALLOW_IPS=127.0.0.1
GENEMCO_API_DOCS=0
GENEMCO_LOG_LEVEL=info
EOF
unset TOKEN

sudo chown root:genemco-query /etc/genemco-query/query.env
sudo chmod 640 /etc/genemco-query/query.env
```

| Variable | Value | Meaning |
|---|---|---|
| `GENEMCO_API_HOST` | `127.0.0.1` | Loopback only. **Do not change to `0.0.0.0`.** |
| `GENEMCO_API_PORT` | `9000` | Listen port |
| `QUERY_UPSTREAM_TOKEN` | generated above | Shared secret the Worker must send |
| `GENEMCO_GOLDEN_DIR` | `…/data/golden_records` | Authoritative records |
| `GENEMCO_CATALOG_PATH` | `…/catalog_golden_records.jsonl` | Catalog records |
| `GENEMCO_MANUAL_URLS` | `…/manual_urls.json` | Optional; absent today, which is fine |
| `GENEMCO_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trust `X-Forwarded-*` only from the local Worker |
| `GENEMCO_API_DOCS` | `0` | Keep `/docs` and `/openapi.json` off |
| `GENEMCO_LOG_LEVEL` | `info` | Log verbosity |

### 2.6 Run as a systemd service (recommended)

```bash
sudo tee /etc/systemd/system/genemco-query.service > /dev/null <<'EOF'
[Unit]
Description=Genemco Verified Query Service (POST /query on 127.0.0.1:9000)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=genemco-query
Group=genemco-query
WorkingDirectory=/opt/genemco-query
EnvironmentFile=/etc/genemco-query/query.env
ExecStart=/opt/genemco-query/venv/bin/python -m api.query_app
Restart=on-failure
RestartSec=5
TimeoutStartSec=60
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now genemco-query
sudo systemctl status genemco-query --no-pager
journalctl -u genemco-query -n 20 --no-pager
```

In the journal, expect a line containing `golden store loaded:` with `'records': 15939`.
`enable` makes the service start again automatically after a reboot.

### 2.7 Foreground run (fallback)

Use this only if you are not using systemd, or to debug interactively. Stop the systemd
service first so the port is free.

```bash
sudo systemctl stop genemco-query 2>/dev/null
cd /opt/genemco-query
sudo -u genemco-query bash -c 'set -a; . /etc/genemco-query/query.env; set +a; exec venv/bin/python -m api.query_app'
```

`Ctrl+C` stops it.

---

## 3. Listen contract

| | |
|---|---|
| Address | `127.0.0.1` |
| Port | `9000` |
| Query | `POST /query`, `Content-Type: application/json` |
| Health | `GET /health` (no token needed) |

**Request body**

```json
{ "query": "What is the oil charge?", "model_code": "RXF-85-H", "top_k": 3 }
```

| Field | Required | Rules |
|---|---|---|
| `query` | yes | string, 2–500 characters |
| `model_code` | no | string, up to 120 characters — model code or SKU, matched exactly |
| `top_k` | no | integer 1–10, **default 3** — maximum citations returned |

**Response body** — always these five fields, in this order:

```json
{
  "answer": "string or null",
  "verified": true,
  "confidence": 0.9,
  "citations": [
    { "manual": "…", "manual_slug": "…", "source_page": 7, "url": null, "quoted_span": "…" }
  ],
  "ungrounded": false
}
```

When the service cannot ground an answer it still returns **HTTP 200** with exactly:

```json
{ "answer": null, "verified": false, "confidence": 0.0, "citations": [], "ungrounded": true }
```

The reason is in the response header `x-query-outcome` (for example `ok`, `unknown_machine`,
`spec_not_carried`, `machines_disagree:3`). It is useful to log on the Worker side.

| Status | Meaning |
|---|---|
| `200` | Answered, or safely refused (`ungrounded: true`) |
| `401` | Token missing or wrong |
| `422` | Invalid request body |
| `503` | `/health` only — records not loaded |

Full reference with examples: `docs/QUERY_API.md` in the bundle.

---

## 4. Auth header

| | |
|---|---|
| Header name | **`X-Upstream-Token`** |
| Header value | The raw token — **no** `Bearer ` prefix |
| Example | `X-Upstream-Token: <value of QUERY_UPSTREAM_TOKEN>` |
| Required on | `POST /query` |
| Not required on | `GET /health` |
| Wrong or missing | `401` with `{"detail": "unauthorized"}` |

- The value is compared exactly, in constant time.
- **The token must be set.** If `QUERY_UPSTREAM_TOKEN` is empty or absent, the check is
  switched off and any process on the VM could call `/query`. Step 2.5 sets it.
- **The Worker holds the only copy outside this service.** Public clients never see the token.
  The Worker should drop any `X-Upstream-Token` a client sends and set its own.

### Giving the token to the Worker without displaying it

The Worker runs on the same VM, so the value only has to move between two files on this
box. Replace `<WORKER_ENV_FILE>` and `<WORKER_TOKEN_VAR>` with the Worker's own names:

```bash
sudo sed -n 's/^QUERY_UPSTREAM_TOKEN=/<WORKER_TOKEN_VAR>=/p' /etc/genemco-query/query.env \
  | sudo tee -a <WORKER_ENV_FILE> > /dev/null
```

Nothing is printed; restart the Worker afterwards so it picks the value up.

### Rotating the token

Repeat step 2.5, update the Worker the same way, then restart both:

```bash
sudo systemctl restart genemco-query
# restart the Worker
```

---

## 5. Smoke test — before repointing the Worker

Run these on the VM right after step 2.6. They talk to the service directly, so they prove
it works independently of the Worker.

**1. Listening on loopback only**

```bash
ss -ltn | grep ':9000'
```

Expect `127.0.0.1:9000`. You must **not** see `0.0.0.0:9000` or `*:9000`.

**2. Health**

```bash
curl -s http://127.0.0.1:9000/health; echo
```

Expect HTTP 200 and (`load_seconds` will vary):

```json
{"status":"ok","store":{"loaded":true,"records":15939,"authoritative":20,"catalog":15939,"dual_source":20,"precedence_conflicts":3,"unreadable_files":0,"load_seconds":1.0}}
```

**3. Token is enforced**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the oil charge?", "model_code": "RXF-85-H"}'
```

Expect `401`.

**4. A verified answer, with the token**

The token is read from the config file into a shell variable; it is never displayed.

```bash
TOKEN="$(sudo sed -n 's/^QUERY_UPSTREAM_TOKEN=//p' /etc/genemco-query/query.env)"

curl -s -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: ${TOKEN}" \
  -d '{"query": "What is the oil charge?", "model_code": "RXF-85-H"}'; echo
```

Expect HTTP 200:

```json
{"answer":"The basic charge of RXF-85-H is 36 gallon.","verified":true,"confidence":0.9,"citations":[{"manual":"070.410-IOM.pdf","manual_slug":"070-410-iom","source_page":7,"url":null,"quoted_span":"85, 101 | BASIC CHARGE (gallon): 36"}],"ungrounded":false}
```

**5. A safe refusal, with the token**

```bash
curl -s -D - -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -H "X-Upstream-Token: ${TOKEN}" \
  -d '{"query": "What is the oil charge?", "model_code": "RXF-999"}'; echo

unset TOKEN
```

Expect HTTP 200, a header `x-query-outcome: unknown_machine`, and:

```json
{"answer":null,"verified":false,"confidence":0.0,"citations":[],"ungrounded":true}
```

### Pass criteria

| Check | Pass |
|---|---|
| 1 | `127.0.0.1:9000` only |
| 2 | `200`, `"status":"ok"`, `"records":15939` |
| 3 | `401` |
| 4 | `200`, `"verified":true`, `"source_page":7` |
| 5 | `200`, `"ungrounded":true`, header `x-query-outcome: unknown_machine` |

All five pass → repoint the Worker.

---

## 6. Repoint the Worker

| Setting | Value |
|---|---|
| Upstream URL | `http://127.0.0.1:9000/query` |
| Method | `POST` |
| Body | Forward the client's JSON unchanged |
| Headers to send | `Content-Type: application/json` and `X-Upstream-Token: <token>` |
| Headers to strip from clients | `X-Upstream-Token` |
| Pass back | HTTP status and JSON body unchanged |
| Worth logging | the `x-query-outcome` response header |
| Suggested timeout | 10 seconds (typical responses take a few milliseconds) |

Then repeat smoke test 4 **through the Worker** with a valid JWT. The response body should be
identical to the direct call.

---

## 7. Operating the service

```bash
sudo systemctl status genemco-query --no-pager        # state
journalctl -u genemco-query -f                         # live logs
sudo systemctl restart genemco-query                   # restart
sudo systemctl stop genemco-query                      # stop
```

**Updating the data.** The service reads records only at startup. Replace the files under
`/opt/genemco-query/data/`, keep ownership, then restart:

```bash
sudo chown -R genemco-query:genemco-query /opt/genemco-query/data
sudo systemctl restart genemco-query
curl -s http://127.0.0.1:9000/health; echo
```

**Updating the code.** Unpack a newer bundle over `/opt/genemco-query` (steps 2.2–2.4; your
`/etc/genemco-query/query.env` is untouched), then restart.

**Removing the service.**

```bash
sudo systemctl disable --now genemco-query
sudo rm /etc/systemd/system/genemco-query.service && sudo systemctl daemon-reload
sudo rm -rf /opt/genemco-query /etc/genemco-query
sudo userdel genemco-query
```

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/health` returns `503` or `"records":0` | Data paths wrong or files missing | Check the two `GENEMCO_*` paths in `query.env` against step 1 |
| `401` on every `/query` | Token mismatch, or the Worker not sending the header | Re-copy the token as in §4; confirm the header name is exactly `X-Upstream-Token` |
| `Address already in use` | Something already on port 9000, or the foreground run left open | `ss -ltnp \| grep ':9000'`; stop the other process |
| `ModuleNotFoundError: No module named 'api'` | Not started from `/opt/genemco-query` | Keep `WorkingDirectory=/opt/genemco-query` in the unit |
| `Permission denied` under `/opt/genemco-query` | Ownership lost after copying files | `sudo chown -R genemco-query:genemco-query /opt/genemco-query` |
| `No module named venv` | Package not installed | `sudo apt-get install -y python3.12-venv` |
| Service keeps restarting | Startup error | `journalctl -u genemco-query -n 50 --no-pager` |

---

## 9. Verified before handoff

Checked by unpacking this exact bundle into an empty directory, creating a fresh Python 3.12
virtualenv, installing only `requirements-query.txt`, and starting the service with the §2.5
variables:

- bundle checksum and `MANIFEST.sha256` verify
- `/health` returns `200` with 15,939 records
- `/query` without the token returns `401`
- smoke tests 4 and 5 return exactly the responses shown in §5

The systemd unit (§2.6) and the Linux account commands (§2.3) could not be executed in the
build environment. They use standard systemd and `useradd` options only.
