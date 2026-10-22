# HPE OpsRamp Gateway CI/CD + Admin Automation + ServiceNow Event Integration

## 1. Goal

Build a Python package and CI/CD pipelines that:

1. **Deploy** an HPE OpsRamp Gateway end-to-end using the HPE OpsRamp API (create management profile, obtain activation token, download gateway package, provision the appliance, register it with OpsRamp).
2. **Allow admin** of a deployed OpsRamp Gateway through the gateway appliance admin API (`https://<gateway>:5480`) — network, NTP, syslog, timezone, static routes, proxy, password rotation, service control, config inspection.
3. **Integrate OpsRamp events with ServiceNow** — map OpsRamp alerts to ServiceNow records idempotently (dedup + lifecycle update/close), using the OpsRamp alerts API and the ServiceNow Table API.

---

## 2. Assumptions

| # | Assumption | Why | If wrong |
|---|---|---|---|
| A1 | **CI/CD platform = GitHub Actions** |  | Move `.github/workflows/*` to the target platform; the Python code is platform-agnostic |
| A2 | **Gateway = Classic OpsRamp Gateway** (the appliance exposing the `:5480` admin API), OVA package on VMware vSphere | The `:5480` admin API is only on the Classic/Windows appliance; "allow admin" points at it | NextGen (K3s/Helm) has no `:5480` API — replace `gateway_admin.py` with a Helm/`opsramp-collector-start` driver |
| A3 | **Event transport = polling** the OpsRamp alerts API (`GET /api/v2/tenants/{tenantId}/alerts/search`) | Uses only fully documented public API; resumable; no inbound firewall hole | Webhook fast-path is already in scope as an optional alternative (see §5.4) |
| A4 | **ServiceNow target table = ITSM `event` table** by default | "Service now for opsramp events" reads most directly as ServiceNow *Events* | Field maps for `em_event` and `incident` are shipped; switch with one config key |
| A5 | **ServiceNow auth = Basic** with a dedicated integration user | Simplest, works on every release | OAuth/JWT is a config-only swap in `servicenow/client.py` |
| A6 | **No ServiceNow-side app install required** — stock Table API only | Keeps the deliverable self-contained in this repo | OpsRamp's certified ServiceNow Store app is an alternative, mutually exclusive path; out of scope |

---

## 3. Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │            GitHub Actions pipelines           │
                    │  ci.yml │ deploy-gateway.yml │ admin.yml │ sync │
                    └───────────────┬──────────────────────────────┘
                                    │ runs CLI (or container)
                                    ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  opsramp_automation (Python 3.11+, src layout, CLI: opsramp-gw)│
        │                                                               │
        │  opsramp/            servicenow/          sync/     deploy/  │
        │   ├── client.py       ├── client.py        ├── mapper.py ├──  │
        │   │  OAuth2+retry    │  Table API CRUD    ├── engine.py│orch. │
        │   ├── gateway_admin.py├── models.py        ├── state.py │prov/ │
        │   │  :5480 admin API  │                    └── report.py│base │
        │   ├── mgmt_profiles.py                                         │
        │   ├── alerts.py                                             │
        │   └── integrations.py                                        │
        └───────┬──────────────────────────────┬───────────────────────┘
                │                              │
     OpsRamp Cloud API                OpsRamp Gateway Appliance
     api.opsramp.com/api              https://<gw>:5480
                │                              │
                │                    (Classic gateway: register +
                │                     network/ntp/syslog/tz/proxy/
                │                     password/service admin)
                ▼
      ┌──────────────────────────────────────────┐
      │  ServiceNow Instance                      │
      │  POST/PATCH /api/now/table/event          │
      │  (dedup via correlation_id)               │
      └──────────────────────────────────────────┘
```

Two distinct OpsRamp surfaces are used and must not be conflated:

- **OpsRamp Cloud API** (`https://api.opsramp.com/api` or `https://<sub>.api.opsramp.com/api`) — OAuth2 `client_credentials`, scope `global:manage`, token TTL 7199 s. Used for management profiles, alerts, integrations.
- **Gateway appliance admin API** (`https://<gateway-ip>:5480`) — separate credential (`admin` + appliance password), returns `{"token", "expires_in"}`; subsequent calls use `Authorization: token <token>` (note: `token`, **not** `Bearer`).

---

## 4. Repository layout to create

```
servicenow-api-automation/
├── .github/workflows/
│   ├── ci.yml                      # PR gate: ruff, mypy, pytest
│   ├── deploy-gateway.yml          # deploy/register/verify a gateway
│   ├── gateway-admin.yml            # dispatch-driven admin operations
│   └── servicenow-sync.yml          # cron + manual alert→ServiceNow sync
├── src/opsramp_automation/
│   ├── __init__.py
│   ├── config.py                   # pydantic-settings; all env/secret config
│   ├── errors.py                   # typed exceptions + exit-code mapping
│   ├── logging.py                  # structlog JSON logging
│   ├── http.py                     # shared httpx client: retry, backoff, rate limit
│   ├── opsramp/
│   │   ├── client.py               # OAuth2 token manager + cloud API wrapper
│   │   ├── models.py               # pydantic models (Alert, ManagementProfile, ...)
│   │   ├── gateway_admin.py        # appliance :5480 admin client
│   │   ├── management_profiles.py  # profile CRUD, attach/detach, package download
│   │   ├── alerts.py               # alert search + pagination
│   │   └── integrations.py         # custom integration, integration event, activity
│   ├── servicenow/
│   │   ├── client.py               # Table API client
│   │   └── models.py
│   ├── sync/
│   │   ├── mapper.py               # OpsRamp alert → ServiceNow payload
│   │   ├── severity.py             # state/severity/impact/urgency mapping tables
│   │   ├── engine.py               # poll loop, upsert, lifecycle, report
│   │   ├── state.py                # SQLite checkpoint + correlation cache
│   │   └── reporting.py
│   ├── deploy/
│   │   ├── orchestrator.py         # end-to-end deploy, idempotent
│   │   └── provisioners/
│   │       ├── base.py             # Provisioner protocol
│   │       ├── existing.py         # appliance already running (default)
│   │       └── vsphere.py          # OVA deploy via ovftool
│   ├── cli.py                      # typer CLI: opsramp-gw
│   └── webhook/
│       └── receiver.py             # optional FastAPI ALERT webhook receiver
├── config/mapping/
│   ├── event.yaml                  # ServiceNow ITSM Event  (default)
│   ├── em_event.yaml               # ServiceNow Event Management
│   └── incident.yaml               # ServiceNow Incident
├── tests/
│   ├── conftest.py
│   ├── fixtures/alerts/*.json      # recorded OpsRamp alert payloads
│   ├── unit/test_opsramp_client.py
│   ├── unit/test_gateway_admin.py
│   ├── unit/test_management_profiles.py
│   ├── unit/test_servicenow_client.py
│   ├── unit/test_mapper.py
│   ├── unit/test_engine.py
│   └── unit/test_orchestrator.py
├── pyproject.toml
├── Dockerfile
├── .env.example
├── Makefile
└── README.md
```

---

## 5. Work plan (ordered, implementation-ready)

### Phase 0 — Scaffold

**0.1 `pyproject.toml`**
- Python `>=3.11`; build backend `hatchling`; src layout at `src/`.
- Runtime deps: `httpx[http2]`, `pydantic>=2`, `pydantic-settings`, `typer`, `PyYAML`, `structlog`, `tenacity`.
- Optional extras: `vsphere` (nothing — `ovftool` is an external binary), `webhook` (`fastapi`, `uvicorn`).
- Dev deps: `pytest`, `pytest-cov`, `pytest-httpx`, `respx`, `ruff`, `mypy`.
- Entry point: `opsramp-gw = "opsramp_automation.cli:app"`.
- `[tool.ruff]` line-length 100, target `py311`; `[tool.mypy]` strict-ish (`disallow_untyped_defs = true`).
- `[tool.pytest.ini_options]` testpaths, `addopts = "-ra --strict-markers"`.

**0.2 `config.py`** — pydantic-settings, all env-prefixed (`OPSRAMP_*`, `GATEWAY_*`, `SERVICENOW_*`, `SYNC_*`):
- `OpsRampSettings`: `base_url` (default `https://api.opsramp.com`), `tenant_id`, `client_id` (integration Key), `client_secret` (integration Secret), `timeout_s=30`, `max_retries=4`.
- `GatewaySettings`: `host`, `port=5480`, `username="admin"`, `password` (SecretStr), `ca_bundle: Path|None`, `verify_tls: bool = True`, `registration_server` (OpsRamp API server URL for registration).
- `ServiceNowSettings`: `instance` (e.g. `dev12345`), `table="event"`, `username`, `password` (SecretStr), `auth_mode: basic|oauth`, `client_id`/`client_secret` (oauth), `sysparm_limit=100`, `min_interval_s` (client-side throttle).
- `SyncSettings`: `poll_interval_s=300`, `lookback_minutes=15`, `state_path=".opsramp-sync/state.db"`, `dry_run=True` (default safe), `client_ids: list[str]`, `states: list[str] = ["CRITICAL","WARNING"]`, `close_on_recovery=True`, `ci_lookup=True`, `mapping_file` (default `config/mapping/event.yaml`).
- `DeploySettings`: `profile_name`, `package_name: ova|iso|windows|google|nextgen_iso|nextgen_ova`, `provisioner: existing|vsphere`, `vsphere_*` (host, user, password, datacenter, datastore, network, cluster).
- All secret fields are `SecretStr`; a `describe()` helper emits a redacted summary for logs.

**0.3 `errors.py`** — `OpsRampError`, `OpsRampAuthError`, `OpsRampRateLimitError`, `OpsRampNotFoundError`, `GatewayError`, `GatewayAuthError`, `ServiceNowError`, `ServiceNowAuthError`, `SyncError`; each maps to a distinct process exit code.

**0.4 `http.py`** — `build_client(verify, ca_bundle, timeout)` returning a shared `httpx.Client`.
- Retry policy (tenacity): retry on connect/read timeouts and HTTP `429, 500, 502, 503, 504`; exponential backoff `2**attempt` with jitter, cap 30 s.
- **Rate limit handling:** on `429`, read `x-ratelimit-reset` (epoch seconds) and sleep until then (cap 60 s), then retry.
- **Token-expiry handling:** on `407` (OpsRamp: token expired) or `401`, invalidate the cached token and retry once.

**0.5 `logging.py`** — structlog JSON to stdout; `configure_logging(level)`; never log secrets (redact `password`, `client_secret`, `token`, `apiKey`, `newpasswd`).

### Phase 1 — OpsRamp clients

**1.1 `opsramp/client.py`**
- `OpsRampTokenManager`: `POST {base_url}/tenancy/auth/oauth/token`, body form-encoded `grant_type=client_credentials&client_id=...&client_secret=...`; caches `access_token` + `expires_in` (7199 s) and refreshes at `expires_in - 60`. Thread-safe (`threading.Lock`).
- `OpsRampClient`: base `f"{base_url}/api"`; headers `Authorization: Bearer <token>`, `Accept: application/json`.
  - `get/post/put/delete(path, **params)` → parsed JSON; raises typed errors.
  - `request()` handles 401/407 → refresh+retry once; 429 → honour `x-ratelimit-reset`; 404 → `OpsRampNotFoundError`; 410 → hard fail ("tenant deactivated").
  - `paginate(path, params)` generator following `nextPage`/`nextPageNo` for `/search` endpoints.

**1.2 `opsramp/management_profiles.py`** (OpsRamp Cloud API)
- `list_profiles(query: str|None)` → `GET /api/v2/tenants/{tenantId}/managementProfiles/search?queryString=...&pageSize=100`
- `create_profile(name, description, type="Gateway")` → `POST /api/v2/tenants/{tenantId}/managementProfiles` body `{name, description, type}` → returns profile incl. `activationToken`.
- `attach(profile_id)` → `GET /api/v2/tenants/{tenantId}/managementProfiles/{id}/attach` → `{activationToken, tunnelType, registered}`.
- `detach(profile_id)` → `GET .../managementProfiles/{profileId}/detach`.
- `reconnect_tunnel(profile_id)` → `GET .../managementProfiles/{profileId}/reconnectTunnel`.
- `get_profile(profile_id)` → `GET .../managementProfiles/{profileId}` → includes `status`, `registered`, `softwareVersion`, `device.generalInfo`.
- `set_services(profile_id, services)` → `POST .../managementProfiles/{profileId}` body `{"name","type","services":[{"name":"sipAgent","status":"true"|"false"}]}`.
- `find_out_of_sync(profile_id)` → `GET .../managementProfiles/search?outOfSync=true&uuid=<uuid>`.
- `download_package(package_name)` → `GET /api/v2/download/gateway/{packageName}` → `{"url","expires_in":900}`; stream to disk, return path + sha256.

**1.3 `opsramp/gateway_admin.py`** (appliance `:5480`) — the "allow admin" surface
- Base URL `https://{host}:{port}`. TLS: `verify=ca_bundle or verify_tls`; the appliance ships a **self-signed** certificate, so `ca_bundle` is the production path and `verify_tls=false` is an explicit, warned opt-in.
- `authenticate()` → `POST /v2/auth/token` body `{"username","password"}` → `{"token","expires_in"}`; header for all later calls `Authorization: token <token>`; re-auth on `401/403` once.
- `get_config(info: generalInfo|syslogInfo|ntpInfo|tzInfo|servicesInfo|routesInfo)` → `GET /v2/gateway?info=<info>`.
- Actions → `POST /v2/gateway/actions/{actionName}` (each returns `{"success": "<message>"}`):
  - `register(server, activation_token)` → body `{"registration": {"server": "<opsramp apiserver>", "activation_token": "<token>"}}`
  - `set_network_settings(domainname, hostname, interface, dhcp, ipaddr, subnet, gateway, dnsArray)` → body `{"networkSettings": {...}}` (all keys required)
  - `add_static_route(srouteIP, srouteGateway, srouteSubnet, srouteMetric, srouteInterface)` → `{"staticRoutes": {...}}`
  - `set_ntp_servers([...])` → `{"ntpUpdate": {"ntpServers": [...]}}`; `ntp_sync()` → action `ntpSync`
  - `add_syslog(receiver, port, protocol, selectors)` / `update_syslog(...)` / `delete_syslog(...)` → `{"syslogInfo": {selectors, protocol, port, receiver}}`; `reset_syslog()` → action `syslogReset`
  - `set_timezone(tzs)` → `{"timeZone": {"tzs": "Alaska"}}`
  - `set_datetime(hour, min, sec, date)` → `{"dateTime": {...}}`
  - `set_proxy(connection, proxy_ip, proxy_port, proxy_usr, proxy_pwd)` → `{"externalProxy": {...}}` (`connection` ∈ `proxy|direct`)
  - `change_password(username, oldpasswd, newpasswd)` → `{"passwdChange": {...}}`
  - `monit_service(service, action)` → `{"monitService": {"service": "network|squid|postgresql|ntp", "action": "start|stop|status|restart"}}`
  - `restart_all_services()` → action `restartAllServices`
- Every action raises `GatewayError` unless the response contains a `success` key.

**1.4 `opsramp/alerts.py`**
- `search(client_ids, states, start_date, end_date, page_size=100)` → `GET /api/v2/tenants/{tenantId}/alerts/search` with `queryString`, `states`, `clientIds`, `startDate`, `endDate`, `alertTimeBase=updated`, `pageNo`, `pageSize`, `sortName=id`, `isDescendingOrder=false`.
  - Date format: `yyyy-MM-ddTHH:mm:ss 0000` (GMT), URL-encoded (`%20`).
- `iter_alerts(...)` → generator over pages, yielding `Alert` models.
- `get_alert(alert_id)` → `GET /api/v2/tenants/{tenantId}/alerts/{alertId}`.
- `Alert` model fields used: `uniqueId`, `subject`, `description`, `currentState`, `oldState`, `serviceName`, `metric`, `alertType`, `app`, `alertTime`, `createdDate`, `updatedTime`, `clientUniqueId`, `tenantId`, `tenantName`, `mspName`, `status`, `priority`, `acknowledged`, `suppressed`, `closed`, `ticketed`, `isRecoveryAlert`, `repeatCount`, `incidentId`, `eventType`, `device.{id,name,hostName,ipAddress,type,resourceType}`, `resource.{id,name,hostName,ipAddress,type,tags}`, `tags`, `customFields`.

**1.5 `opsramp/integrations.py`** (optional push path + observability)
- `install_custom_integration(display_name, category="Custom")` → `POST /api/v2/tenants/{tenantId}/integrations/install/CUSTOM` body `{displayName, category, inboundConfig:{authentication:{authType:"WEBHOOK", token}}}` → `INTG-<uuid>`.
- `find_installed(query)` → `GET /api/v2/tenants/{tenantId}/integrations/installed/search?queryString=...`
- `create_alert_event(installed_intg_id, name, endpoint_uri, payload_template, headers)` → `POST /api/v2/tenants/{tenantId}/integrations/installed/{installedIntgId}/event`; body `{name, entity:"ALERT", eventType:"NONE", notifier:{type:"REST_API", authType:"NONE", baseURI}, endPointURI, headers:[{key,value}], thirdPartyEventType:"POST", payload: <base64 of JSON template with $alert.* placeholders>}`.
  - Known `$alert.*` placeholders from the vendor spec: `alert.serviceName`, `alert.subject`, `alert.currentState`, `alert.resource.id`, `alert.app`.
- `set_event_active(event_id, active)` → `POST .../installed/{id}/event/{eventId}/activate|deactivate`.
- `activity(installed_intg_id, status, start, end)` → `GET /api/v2/tenants/{tenantId}/integrations/activity?installedintgId=...&status=SUCCESS|FAILED&startTime=...&endTime=...`.
- `retry_failed_events(installed_intg_id, start, end)` → `POST .../installed/{id}/events/re-try` body `{"retry-events":[{"timeRange":{"startTime","endTime"}}]}` (max 2 h window).

### Phase 2 — ServiceNow client

**2.1 `servicenow/client.py`**
- Base `https://{instance}.service-now.com`.
- Auth: `Authorization: Basic base64(user:password)`; `auth_mode=oauth` → `POST /api/now/oauth/token.do` with `grant_type=password` and cached token.
- `create(table, payload)` → `POST /api/now/table/{table}` body JSON → `{"result": {...}}`.
- `update(table, sys_id, payload)` → `PATCH /api/now/table/{table}/{sys_id}` (partial).
- `query(table, query, fields=None, limit=100)` → `GET /api/now/table/{table}?sysparm_query=...&sysparm_fields=...&sysparm_limit=...`; URL-encode.
- `find_by_correlation(table, correlation_id)` → `query(table, f"correlation_id={correlation_id}", fields=["sys_id","number","state","correlation_id"], limit=1)`.
- `lookup_ci(hostname)` → `GET /api/now/table/cmdb_ci?sysparm_query=name={hostname}^ORsys_host_name={hostname}&sysparm_fields=sys_id,name&sysparm_limit=1` (skipped when `SYNC_CI_LOOKUP=false`).
- Retries: `429` and `5xx` with backoff; `401/403` → `ServiceNowAuthError` (fail fast, no retry).
- Client-side throttle to `min_interval_s` between calls (default 0 = off).

**2.2 `servicenow/models.py`** — `ServiceNowRecord` (sys_id, number, raw), `UpsertResult` enum (`CREATED`, `UPDATED`, `UNCHANGED`, `FAILED`).

### Phase 3 — Mapping + sync engine

**3.1 `config/mapping/*.yaml` + `sync/severity.py`**
Declarative, instance-overridable maps. Shape:

```yaml
table: event
dedup_field: correlation_id
correlation:
  format: "opsramp:{clientUniqueId}:{uniqueId}"
  max_length: 1024
fields:
  source:            {const: OpsRamp}
  category:          {from: alertType, map: {Monitoring: Software, Infrastructure: Hardware}, default: Software}
  subcategory:       {from: serviceName}
  impact:            {from: currentState, map: {Critical: "1", Warning: "2"}, default: "3"}
  urgency:           {from: currentState, map: {Critical: "1", Warning: "2"}, default: "3"}
  message:           {from: subject}
  description:       {template: "<alert.description>\n\nOpsRamp alert {uniqueId} ..."}
  node:              {from: device.hostName, fallback: resource.hostName}
  cmdb_ci:           {ci_lookup: true}
  correlation_id:    {from: correlation}
  correlation_display:{template: "OpsRamp {uniqueId} - {subject}"}
  assigned_to:       {const: null, optional: true}
  group:             {const: null, optional: true}
  opened_at:         {from: alertTime, format: servicenow_datetime}
  state:             {map: {open: "1", recovery: "6"}}
```

- `em_event` map: `source`, `event_class` (tenantName), `node`, `type` (serviceName), `resource` (resource.name), `message_key` (dedup field — EM's native dedup key), `severity` (`Critical:5, Major:4, Minor:3, Warning:2, Ok:1, Clear:0`), `resolution_state` (`New` / `Closing`), `time_of_event` (alertTime), `description`, `additional_info` (JSON metadata string).
- `incident` map: `short_description` (subject), `description`, `correlation_id`, `impact`, `urgency`, `priority` (map), `assignment_group`, `cmdb_ci`, `opened_at`, `state` (`1`/`6`), `contact_type` (`alert`).
- Shared severity tables in `sync/severity.py` (Python, unit-testable): `OPSRAMP_STATE_TO_EM_SEVERITY`, `OPSRAMP_STATE_TO_SNOW_URGENCY`, `RECOVERY_STATES = {"Ok","OK","Resolved","Clear"}`.

**3.2 `sync/mapper.py`**
- `map_alert(alert, mapping) -> dict` — resolves `from` / `map` / `const` / `template` / `fallback` / `ci_lookup`; casts to the ServiceNow field type; truncates `description` to 4000 chars, `message`/`correlation_display` to 1000, `correlation_id` to 1024.
- `is_recovery(alert) -> bool` — `alert.isRecoveryAlert is True`, else `currentState in RECOVERY_STATES and oldState not in RECOVERY_STATES`.
- `correlation_id(alert, mapping) -> str`.
- `render_description(alert) -> str` — human-readable body containing: OpsRamp alert id, state transition (`oldState → currentState`), resource host/IP, client, tenant, monitor, service, occurrence count, and a JSON block of `tags`/`customFields`.

**3.3 `sync/state.py`** (stdlib `sqlite3`)
Tables:
- `sync_cursor(name TEXT PK, last_updated_time TEXT, last_alert_unique_id TEXT, updated_at TEXT)`
- `correlation_cache(correlation_id TEXT PK, sys_id TEXT, number TEXT, table_name TEXT, last_state TEXT, last_seen_alert_time TEXT, updated_at TEXT)`
- `sync_failures(id INTEGER PK, correlation_id TEXT, opsramp_unique_id TEXT, error TEXT, attempts INT, created_at TEXT)`
- Behaviour: on start, resume from cursor; overlapping lookback window (`lookback_minutes`) protects against OpsRamp's eventual consistency; correlation cache short-circuits the ServiceNow lookup; `sync_failures` retried on the next run up to `max_attempts`.

**3.4 `sync/engine.py`**
`SyncEngine.run_once() -> SyncReport`:
1. Ensure cursor (initialise to `now - lookback_minutes`).
2. Iterate `alerts.iter_alerts(client_ids, states, start_date=cursor, end_date=now, alertTimeBase=updated)`.
3. For each alert: compute `correlation_id`; look up in `correlation_cache` → if absent, `servicenow.find_by_correlation()`.
4. Decide action:
   - not found **and** not recovery → **create** (unless `dry_run`).
   - not found **and** recovery → skip (nothing to close) but record in report as `skipped_no_record`.
   - found **and** open state changed (e.g. state/urgency/message differ) → **update** (PATCH only changed fields).
   - found **and** recovery **and** `close_on_recovery` → **update** `state=6` (Resolved) + `close_notes`/`resolution_notes`; for `em_event` set `resolution_state=Closing`, `severity=0`.
   - found **and** no meaningful change → **unchanged** (no API write).
5. Update the cache and advance the cursor to `max(updatedTime)`; on per-alert exception, record a failure, log, continue.
6. Emit `SyncReport` (counts by action, failures, duration, window).
`SyncEngine.run_forever()` — loop with `poll_interval_s` jitter, graceful SIGTERM shutdown, structured heartbeat log.

**3.5 `sync/reporting.py`** — `SyncReport` dataclass → JSON (for CI artifact) and a short Markdown summary (for `$GITHUB_STEP_SUMMARY`).

### Phase 4 — Deploy + CLI

**4.1 `deploy/provisioners/`**
- `base.py` — `Protocol Provisioner` with `prepare(package_path) -> Endpoint`, `teardown()`, `describe()`.
- `existing.py` (default) — no provisioning; probes `https://{host}:5480/v2/gateway?info=generalInfo` and fails fast with a clear message if unreachable.
- `vsphere.py` — OVA deploy via the `ovftool` binary invoked through `subprocess` (more reliable than pyvmomi's `DeployOva`): `ovftool --noSSLVerify --acceptAllEula --diskMode=thin --powerOn -ds <datastore> -n <network> <ova> <vcenter>`, then polls the vCenter API/VM guest IP until the appliance answers on `:5480`. Credentials from `DeploySettings`; `ovftool` is a **prerequisite** and its absence is a clear, actionable error.

**4.2 `deploy/orchestrator.py`** — `deploy_gateway(settings) -> DeployReport`, idempotent, stage-by-stage with clear failure messages:
1. **Profile** — search for a management profile named `settings.profile_name`; create if missing (`type="Gateway"`).
2. **Attach** — `GET .../managementProfiles/{id}/attach` → `activationToken`; also capture the OpsRamp API server URL for registration (from config `gateway.registration_server`; the API does not return it in the documented schema — see Risks R5).
3. **Package** — `GET /api/v2/download/gateway/{packageName}` → `{url, expires_in:900}` → stream download, record sha256.
4. **Provision** — hand the package to the provisioner; get the gateway host/IP.
5. **Admin auth** — `POST /v2/auth/token`.
6. **Configure** — read current config with `GET /v2/gateway?info=...` and apply **only deltas** (idempotent): `networkSettings`, `timeZone`, `ntpUpdate`+`ntpSync`, `syslogAdd` per receiver, `staticRoutes`, `externalProxy`.
7. **Register** — skip if `generalInfo.applianceInformation.registrationStatus == "Registered"`; else `POST /v2/gateway/actions/registration` with `{server, activation_token}`.
8. **Verify** — poll `GET /api/v2/tenants/{tenantId}/managementProfiles/{profileId}` until `registered == true` and `status == "UP"` (default timeout 15 min, 15 s interval), and `GET /v2/gateway?info=generalInfo` until `tunnelStatus == "Established"`.
9. **Report** — `deploy-report.json` with stage outcomes, profile id, activation token *redacted*, firmware version, timings.

**4.3 `cli.py`** — `typer` app `opsramp-gw`:

```
opsramp-gw deploy            --profile-name --package --provisioner [--dry-run]
opsramp-gw profile list|create|attach|detach|reconnect|services|tunnel
opsramp-gw admin status
opsramp-gw admin config      --info generalInfo|syslogInfo|ntpInfo|tzInfo|servicesInfo|routesInfo
opsramp-gw admin network set --hostname --ip --subnet --gateway --dns --domain --interface [--dhcp]
opsramp-gw admin ntp set     --server (repeatable) [--sync]
opsramp-gw admin syslog add|update|delete|reset  --receiver --port --protocol --selectors
opsramp-gw admin timezone set --tz
opsramp-gw admin datetime set --date --time
opsramp-gw admin route add   --network --gateway --subnet --metric --interface
opsramp-gw admin proxy set   --direct | --proxy-ip --proxy-port --user --password
opsramp-gw admin password change --old --new
opsramp-gw admin service     <network|squid|postgresql|ntp> <start|stop|status|restart>
opsramp-gw admin restart all
opsramp-gw admin register    --server --activation-token
opsramp-gw alerts list       [--client] [--state] [--since] [--limit]
opsramp-gw sync              --once | --daemon [--dry-run]
opsramp-gw integration apply --name --webhook-url        # optional push path
opsramp-gw integration activity --status FAILED --since
```
- Global flags: `--config` (YAML file), `--log-level`, `--json-output`.
- Secrets read from env, never from argv (except `--old/--new` password, which is read from env `GATEWAY_OLD_PASSWORD`/`GATEWAY_NEW_PASSWORD` to avoid shell history leakage).
- Exit codes: `0` ok, `1` generic, `2` config error, `3` auth error, `4` rate limited, `5` gateway unreachable, `6` partial sync failure.

### Phase 5 — CI/CD pipelines (GitHub Actions)

**5.1 `.github/workflows/ci.yml`** — on `pull_request` + `push` to `main`:
- job `lint`: `ruff check`, `ruff format --check`
- job `typecheck`: `mypy src`
- job `test`: `pytest --cov=opsramp_automation --cov-report=xml` (matrix `3.11`, `3.12`)
- job `container`: `docker build .` (smoke) — optional, `needs: [lint, typecheck, test]`.
- No secrets required; no deployment.

**5.2 `.github/workflows/deploy-gateway.yml`** — on `workflow_dispatch` (inputs: `environment`, `profile_name`, `package`, `provisioner`, `dry_run`) and `push` tags `gateway-v*`:
- `environment: ${{ inputs.environment }}` → gives you the environment protection rules / approval gate / scoped secrets.
- `permissions: contents: read, id-token: write, deployments: write`.
- Steps: checkout → `actions/setup-python` → `pip install -e .[dev]` → `opsramp-gw deploy ...` → upload `deploy-report.json` artifact → append the report to `$GITHUB_STEP_SUMMARY`.
- Secrets (environment-scoped): `OPSRAMP_BASE_URL`, `OPSRAMP_TENANT_ID`, `OPSRAMP_API_KEY`, `OPSRAMP_API_SECRET`, `GATEWAY_HOST`, `GATEWAY_PORT`, `GATEWAY_ADMIN_USER`, `GATEWAY_ADMIN_PASSWORD`, `GATEWAY_CA_BUNDLE` (base64 PEM), `OPSRAMP_APISERVER`, `VSHERE_*` (when `provisioner=vsphere`).
- Guard: `dry_run` default `true` for `workflow_dispatch`; tag-triggered runs execute for real.

**5.3 `.github/workflows/gateway-admin.yml`** — `workflow_dispatch` only:
- Inputs: `action` (enum mirroring the CLI), `payload` (raw JSON), `environment`.
- Runs `opsramp-gw admin <action> --payload-json "${{ inputs.payload }}"` inside the `environment` gate.
- `password change` is a separate, opt-in job that additionally rotates the GitHub secret via `gh secret set` (requires `GITHUB_TOKEN` with `secrets: write`); it is skipped unless the input is explicitly `rotate`.

**5.4 `.github/workflows/servicenow-sync.yml`** — `schedule: '*/5 * * * *'` + `workflow_dispatch`:
- `concurrency: {group: servicenow-sync, cancel-in-progress: false}` to avoid overlapping runs.
- Runs `opsramp-gw sync --once`; uploads `sync-report.json`; fails the run when `failed > 0` (threshold configurable via repo variable `SYNC_MAX_FAILURES`).
- Secrets: `OPSRAMP_*` (same set) + `SERVICENOW_INSTANCE`, `SERVICENOW_USER`, `SERVICENOW_PASSWORD`, `SERVICENOW_TABLE`.

**5.5 `Dockerfile`** — `python:3.12-slim`, non-root user, `pip install .`, `ENTRYPOINT ["opsramp-gw"]`. Used by the sync job (optional `container:` runs the same image so the runner needs no Python).

### Phase 6 — Tests & docs

**6.1 Tests** (`respx` for outbound HTTP mocking; no live calls in CI):
- `test_opsramp_client.py` — token fetch/refresh/reuse, 401→refresh→retry, 429→`x-ratelimit-reset` honoured, pagination stops at `nextPage:false`, 410 raises.
- `test_gateway_admin.py` — each action posts the exact documented body; non-`success` response raises; re-auth on 401; TLS-CA-bundle path used when configured.
- `test_management_profiles.py` — create/attach/detach/search/download URL handling.
- `test_servicenow_client.py` — query encoding, correlation lookup, PATCH vs POST selection, 429 backoff.
- `test_mapper.py` — table-driven: 12 representative OpsRamp alerts (from `tests/fixtures/alerts/`) → expected ServiceNow payloads for each of the 3 maps; recovery detection; field truncation; CI lookup fallback.
- `test_engine.py` — create → update → recovery-close sequence against a fake ServiceNow; dedup prevents a second create; a failing alert is isolated and recorded.
- `test_orchestrator.py` — deploy is idempotent (second run performs no registration); registration skipped when already `Registered`; verification timeout path.

**6.2 `README.md`** — architecture, prerequisites, secret list, per-CLI-command examples, pipeline setup, runbook (rotate gateway password, re-run deploy, replay failed integration events, backfill a date range into ServiceNow).

**6.3 `.env.example`**, **`Makefile`** (`make lint test typecheck ci local-sync`).

---

## 6. Data contracts (reference)

### OpsRamp → ServiceNow field mapping (ITSM `event` table, default)

| ServiceNow field | OpsRamp source | Notes |
|---|---|---|
| `source` | constant `OpsRamp` | must exist in `sys_choice` (`sn_si_incident_source`) on the target instance |
| `category` | `alertType` via map | default `Software` |
| `subcategory` | `serviceName` | |
| `impact` | `currentState` | Critical→1, Warning→2, else 3 |
| `urgency` | `currentState` | Critical→1, Warning→2, else 3 |
| `message` | `subject` | truncated 1000 |
| `description` | `description` + rendered metadata | truncated 4000 |
| `node` | `device.hostName` → fallback `resource.hostName` | |
| `cmdb_ci` | CI lookup by hostname | optional (`SYNC_CI_LOOKUP`) |
| `correlation_id` | `opsramp:{clientUniqueId}:{uniqueId}` | **dedup key** |
| `correlation_display` | `OpsRamp {uniqueId} - {subject}` | |
| `opened_at` | `alertTime` | `YYYY-MM-DD HH:MM:SS` UTC |
| `state` | open→1, recovery→6 | `6` = Resolved; `7` = Closed is configurable |
| `assigned_to` / `group` | configurable map | left null by default |
| `severity` | `currentState` | only if the field exists on the instance |
| `comments` / `work_notes` | JSON metadata block | optional |

### Dedup / lifecycle rules
- One OpsRamp alert (`clientUniqueId` + `uniqueId`) ⇒ exactly one ServiceNow record, enforced by `correlation_id`.
- A **recovery** alert updates the existing record; it never creates a second one.
- Changed `urgency`/`message` on an open alert triggers a `PATCH` (no duplicate).
- A recovery for an alert that was never created is skipped (no orphan close).
- `correlation_cache` avoids a ServiceNow `GET` per alert; the cache is bypassed once per run for records not seen in the last `cache_ttl`.

### Failure modes and handling
| Condition | Handling |
|---|---|
| OpsRamp `401`/`407` | refresh token, retry once |
| OpsRamp `429` | sleep until `x-ratelimit-reset` (cap 60 s), retry |
| OpsRamp `410 Gone` | fail fast — tenant deactivated |
| OpsRamp `404` on alert | log and skip (alert purged) |
| Gateway appliance unreachable | fail with a clear "cannot reach https://host:5480" message; check firewall/self-signed cert |
| Gateway self-signed TLS | require `GATEWAY_CA_BUNDLE`; `verify_tls=false` is an explicit warned opt-in |
| Gateway already registered | skip registration (idempotent) |
| Registration tunnel slow | poll up to 15 min, then fail with the last observed `tunnelStatus` |
| ServiceNow `401`/`403` | fail fast — do not retry |
| ServiceNow `429`/`5xx` | exponential backoff, then record the alert as failed |
| Duplicate record in ServiceNow | prevented by `correlation_id`; if a duplicate still appears, `find_by_correlation` returns the first match and the cache is corrected |
| Malformed `alertTime` | log, fall back to the record's `createdDate`, then to `now` |
| Partial sync failure | per-alert isolation, failures persisted, non-zero exit, `sync_failures` retried next run |

### Security
- All secrets in GitHub **environment-scoped** secrets; never in the repo, never in logs (redaction list in `logging.py`).
- ServiceNow: dedicated integration user with the minimum role (`itil` + write on the target table). Documented in README.
- Gateway admin password rotation is supported (`admin password change`) and the pipeline can write the rotated value back to the GitHub secret via `gh secret set`.
- OpsRamp integration Key/Secret are tenant-wide `global:manage` — scope them to a dedicated OpsRamp integration and rotate.

---

## 7. Risks and open questions

| # | Risk / question | Mitigation |
|---|---|---|
| R1 | ServiceNow field names and choice values differ per instance (e.g. `source` must be an existing `sys_choice` entry; `event` may not have `severity`) | Field maps are YAML-configurable; add a `opsramp-gw doctor` command that validates the target table's dictionary (`GET /api/now/table/sys_dictionary?sysparm_query=name=<table>^nameISNOTEMPTY`) and the required choice values, failing fast with a precise list of what to fix |
| R2 | `$alert.*` placeholders for ALERT integration events are only partially documented (spec enumerates Incident/Change/Service Request/Problem/Task) | The polling path (§3) is the default and is unaffected; the webhook path (§4.5, §5.4) is opt-in and validated against `GET /api/v2/tenants/{tenantId}/integrations/events/placeHolders/{entityType}` at apply time |
| R3 | High alert volume against client rate limits (Alerts GET-paginated = 50/min client, 150/min partner) | `poll_interval_s` + `clientIds` narrowing + `states` filter + `pageSize` tuning; `http.py` honours `x-ratelimit-*` headers |
| R4 | Classic gateway appliance ships a self-signed certificate | `GATEWAY_CA_BUNDLE` is the documented production path; `verify_tls=false` prints a warning |
| R5 | The OpsRamp API server URL needed for the gateway `registration` action is not returned by the documented management-profile schema | Take it from config `GATEWAY_REGISTRATION_SERVER` (shown in the OpsRamp UI next to the activation token); `deploy` fails with an explicit message if it is unset |
| R6 | `ovftool` is an external binary not present on GitHub-hosted runners | The `vsphere` provisioner is opt-in and runs on a self-hosted runner or a runner image that ships `ovftool`; the default `existing` provisioner needs nothing |
| R7 | OpsRamp `alerts/search` is eventually consistent, so a very recent alert can be missed by a strict time window | The cursor keeps a `lookback_minutes` overlap (default 15) and the dedup key makes re-processing harmless |
| R8 | It is not yet confirmed whether the target instance wants `event`, `em_event`, or `incident` | All three maps ship; switching is one config value (`SERVICENOW_TABLE` / `SYNC_MAPPING_FILE`) |

---

## 8. Out of scope (default)

- The OpsRamp-certified ServiceNow Store app (Store method) and its OAuth2 bidirectional sync — mutually exclusive with the stock Table API path used here.
- NextGen Gateway (K3s/Helm) deployment and upgrade — the `:5480` admin API does not exist there.
- CMDB synchronisation of OpsRamp resources into ServiceNow (`cmdb_ci` lookup only, no CI creation).
- OpsRamp alert auto-escalation policy authoring, ticket/incident creation inside OpsRamp, RBA/automation jobs.
- Multi-tenant fan-out beyond one OpsRamp client per run (the client filter is configurable, but one credential set is assumed).
- Windows Gateway and ISO/Google package provisioning mechanics (the download API supports them; only OVA/`existing` provisioners are implemented).
