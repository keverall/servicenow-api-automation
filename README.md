# HPE OpsRamp Gateway CI/CD, Administration and ServiceNow Integration

Automate the full lifecycle of an [HPE OpsRamp](https://www.hpe.com/opsramp)
gateway and route its alerts into ServiceNow.

* **Deploy** a gateway end to end with the OpsRamp API: create the management
  profile, obtain the activation token, download the gateway package, provision
  the appliance, apply the desired configuration, register it and verify the
  tunnel.
* **Administer** a running gateway through the appliance admin API on port
  `5480`: network, NTP, syslog, time zone, static routes, proxy, password
  rotation and service control.
* **Integrate** OpsRamp alerts with ServiceNow: alerts are mapped onto
  ServiceNow records, deduplicated with a correlation id and kept in sync
  through their whole lifecycle (raise, change, recovery).

---

## Contents

| Path | Purpose |
| --- | --- |
| `src/opsramp_automation/opsramp/` | OpsRamp cloud API client, gateway admin client, management profiles, alerts, integrations |
| `src/opsramp_automation/servicenow/` | ServiceNow Table API client |
| `src/opsramp_automation/sync/` | Alert to ServiceNow mapping, checkpointing and the sync engine |
| `src/opsramp_automation/deploy/` | Deployment orchestrator and provisioners |
| `src/opsramp_automation/webhook/` | Optional webhook receiver for the push based delivery path |
| `src/opsramp_automation/cli.py` | The `opsramp-gw` command line interface |
| `config/mapping/` | Declarative alert to ServiceNow field mappings |
| `.github/workflows/` | CI, deploy, admin and sync pipelines |
| `Dockerfile` | Container image for the CLI and the sync daemon |

---

## Requirements

* Python 3.11 or newer
* An OpsRamp tenant with an API integration (**Setup > Integrations > Custom**)
  providing a Key and a Secret
* A ServiceNow instance with a dedicated integration user
* For the vSphere provisioner: `ovftool` on the machine running the pipeline

---

## Installation

```bash
make venv            # creates .venv and installs the package with dev extras
```

Or do it by hand:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the values.  Every setting can also
be supplied as an environment variable, which is how the CI pipelines pass
their secrets.

---

## The two OpsRamp surfaces

The package talks to two different OpsRamp APIs.  They are not interchangeable.

| | OpsRamp Cloud API | Gateway appliance admin API |
| --- | --- | --- |
| Base URL | `https://api.opsramp.com/api` (or `https://<tenant>.api.opsramp.com/api`) | `https://<gateway>:5480` |
| Auth | OAuth2 `client_credentials`, `Authorization: Bearer <token>` | `POST /v2/auth/token`, then `Authorization: token <token>` |
| Used for | management profiles, alerts, integrations | configuring and registering the appliance |

---

## Command line

```
opsramp-gw --help
```

### Deploy a gateway

```bash
export OPSRAMP_TENANT_ID=client_123456
export OPSRAMP_CLIENT_ID=...
export OPSRAMP_CLIENT_SECRET=...
export GATEWAY_HOST=10.20.30.40
export GATEWAY_USERNAME=admin
export GATEWAY_PASSWORD=...
export GATEWAY_REGISTRATION_SERVER=https://api.opsramp.com

# Report only what would change.
opsramp-gw deploy --profile-name prod-gateway --package ova --dry-run

# Apply it.
opsramp-gw deploy --profile-name prod-gateway --package ova --report deploy-report.json
```

The deploy is idempotent: an already registered gateway is left alone and
configuration is only written when it differs from the appliance state.

The desired configuration is taken from the `DEPLOY_*` settings (see
`.env.example`), for example:

```bash
export DEPLOY_HOSTNAME=opsramp-gw-01
export DEPLOY_DOMAIN=example.com
export DEPLOY_INTERFACE=ens160
export DEPLOY_IP_ADDRESS=10.20.30.40
export DEPLOY_SUBNET=255.255.255.0
export DEPLOY_NETWORK_GATEWAY=10.20.30.1
export DEPLOY_DNS=10.20.30.1
export DEPLOY_DHCP=false
export DEPLOY_TIMEZONE=Europe/London
export DEPLOY_NTP_SERVERS=216.239.35.4,45.32.199.189
export DEPLOY_SYSLOG_RECEIVERS=10.20.30.50,10.20.30.51
```

### Administer a gateway

```bash
opsramp-gw admin status
opsramp-gw admin config --info generalInfo
opsramp-gw admin network set --hostname gw-01 --ip 10.20.30.40 --subnet 255.255.255.0 \
    --gateway 10.20.30.1 --dns 10.20.30.1 --domain example.com --interface ens160 --dhcp
opsramp-gw admin ntp set --server 216.239.35.4 --server 45.32.199.189 --sync
opsramp-gw admin syslog add --receiver 10.20.30.50 --port 514 --protocol udp
opsramp-gw admin syslog delete --receiver 10.20.30.50 --port 514 --protocol udp
opsramp-gw admin syslog reset
opsramp-gw admin timezone set --tz Europe/London
opsramp-gw admin datetime set --date 04-13-2021 --time 10:20:30
opsramp-gw admin route add --network 10.30.0.0 --gateway 10.20.30.254 \
    --subnet 255.255.0.0 --metric 0 --interface ens160
opsramp-gw admin proxy set --direct
opsramp-gw admin service ntp status
opsramp-gw admin restart all
opsramp-gw admin register --server https://api.opsramp.com --activation-token <token>
```

Every appliance action accepts `--payload-json`, which overrides the generated
request body.  That is how the CI workflow drives the appliance with a single
generic input:

```bash
opsramp-gw admin syslog add --payload-json \
  '{"syslogInfo": {"receiver": "10.0.0.1", "port": "514", "protocol": "udp", "selectors": "*.*"}}'
```

The password rotation reads the credentials from the environment so they never
appear in the process arguments or the shell history:

```bash
export GATEWAY_OLD_PASSWORD=...
export GATEWAY_NEW_PASSWORD=...
opsramp-gw admin password change
```

### Management profiles

```bash
opsramp-gw profile list
opsramp-gw profile list --query "name:prod-gateway"
opsramp-gw profile create prod-gateway --description "Managed by CI" --type Gateway
opsramp-gw profile attach 12345
opsramp-gw profile tunnel 12345
opsramp-gw profile reconnect 12345
opsramp-gw profile detach 12345
opsramp-gw profile services 12345 --service sipAgent --enable
opsramp-gw profile out-of-sync
```

### Inspect alerts

```bash
opsramp-gw alerts list --client client_123456 --state Critical --since 2026-01-01 --limit 50
```

### Sync OpsRamp alerts into ServiceNow

```bash
# One pass, no writes.
opsramp-gw sync --once --dry-run --report sync-report.json

# One pass, writing to ServiceNow.
opsramp-gw sync --once --report sync-report.json

# Continuously.
opsramp-gw sync --daemon
```

### Check the configuration

```bash
opsramp-gw doctor
```

`doctor` validates the settings and checks that every field referenced by the
mapping exists on the target ServiceNow table.

---

## How the ServiceNow mapping works

The mapping is data driven. `config/mapping/event.yaml` maps OpsRamp alerts
onto the ServiceNow ITSM **Event** table (`event`); `em_event.yaml` targets
ServiceNow **Event Management** and `incident.yaml` targets **Incident**.

```yaml
table: event
dedup_field: correlation_id
correlation:
  format: "opsramp:{clientUniqueId}:{uniqueId}"
  max_length: 1024
fields:
  source:      { const: OpsRamp }
  message:     { from: subject, truncate: 1000 }
  description: { render: description, truncate: 4000 }
  node:        { from: host_name }
  cmdb_ci:     { from: host_name, ci_lookup: true }
  opened_at:   { from: alertTime, format: servicenow_datetime }
  state:
    on_open:
      const: "1"
    on_recovery:
      const: "6"
```

A field specification may use `const`, `from`, `fallback`, `map`, `default`,
`template`, `render`, `truncate`, `ci_lookup`, `format`, `optional` and the
`on_open` / `on_recovery` branches.  `from` accepts a dotted path into the
alert (`subject`, `device.hostName`) or the pseudo sources `correlation` and
`state`.  `render` accepts `description` and `metadata`.

### Deduplication and lifecycle

* The dedup value is `opsramp:<clientUniqueId>:<uniqueId>`, written to the
  `correlation_id` column (`message_key` for `em_event`).
* One OpsRamp alert therefore maps to exactly one ServiceNow record.
* A **recovery** alert updates that record (state becomes `6` / `Resolved`,
  or `resolution_state` becomes `Closing` for Event Management) instead of
  creating a second one.
* A recovery for an alert that was never written is skipped.
* An alert that never had a problem state is skipped before any ServiceNow
  call, which keeps the request count inside the OpsRamp rate limits.
* The sync keeps a SQLite checkpoint (`SYNC_STATE_PATH`) holding the cursor
  and the correlation cache, so a re-run never duplicates work and repeated
  alerts are cheap.

### Sync settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `SYNC_POLL_INTERVAL_S` | `300` | Delay between polls in daemon mode |
| `SYNC_LOOKBACK_MINUTES` | `15` | Overlap applied to the cursor window |
| `SYNC_STATE_PATH` | `.opsramp-sync/state.db` | Checkpoint database |
| `SYNC_DRY_RUN` | `true` | Refuse to write to ServiceNow |
| `SYNC_CLIENT_IDS` | – | Comma separated OpsRamp client ids |
| `SYNC_STATES` | `Critical,Warning,Ok` | Monitored alert states |
| `SYNC_CLOSE_ON_RECOVERY` | `true` | Close the record on recovery |
| `SYNC_CI_LOOKUP` | `true` | Resolve `cmdb_ci` from the host name |
| `SYNC_MAPPING_FILE` | `config/mapping/event.yaml` | Mapping to use |
| `SYNC_MAX_ATTEMPTS` | `3` | Retry budget for a failing alert |
| `SYNC_CACHE_TTL_MINUTES` | `60` | How long a cached correlation is trusted |
| `SYNC_PAGE_SIZE` | `100` | Alerts requested per page |

---

## CI/CD pipelines

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `ci.yml` | pull request, push to `main` | Lint, type check, unit tests, container build |
| `deploy-gateway.yml` | manual dispatch, `gateway-v*` tags | Deploy, configure, register and verify a gateway |
| `gateway-admin.yml` | manual dispatch | Run any gateway admin action inside an environment gate |
| `servicenow-sync.yml` | every 5 minutes, manual | Run one sync pass and publish the report |

### Required secrets

Set these as **environment scoped** secrets (for example `prod`) so they are
only available to the workflows that need them.

| Secret | Used by |
| --- | --- |
| `OPSRAMP_BASE_URL` | all |
| `OPSRAMP_TENANT_ID` | all |
| `OPSRAMP_CLIENT_ID`, `OPSRAMP_CLIENT_SECRET` | all |
| `GATEWAY_HOST`, `GATEWAY_PORT` | deploy, admin |
| `GATEWAY_USERNAME`, `GATEWAY_PASSWORD` | deploy, admin |
| `GATEWAY_CA_BUNDLE` | deploy, admin (base64 encoded PEM) |
| `GATEWAY_REGISTRATION_SERVER` | deploy |
| `GATEWAY_OLD_PASSWORD`, `GATEWAY_NEW_PASSWORD` | admin (password change) |
| `SERVICENOW_INSTANCE`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` | sync |
| `VSHERE_*` | deploy (only with the vSphere provisioner) |

Non-secret values (profile name, IP addresses, NTP servers, ...) belong in
repository **variables** of the same environment rather than in the workflow
file.

The gateway appliance ships a self-signed certificate.  Export the appliance
PEM into `GATEWAY_CA_BUNDLE` for production use; `GATEWAY_VERIFY_TLS=false`
is available as an explicit, warned fallback.

---

## Container image

```bash
make docker-build
docker run --rm --env-file .env opsramp-automation:local sync --once --dry-run
```

The image ships the CLI, the mapping files and a non-root user.  Mount a volume
on `/data` to persist the sync checkpoint (`SYNC_STATE_PATH=/data/state.db`).

---

## Runbook

**A gateway is not registering.**  Check `opsramp-gw admin status` for the
registration and tunnel state, then `opsramp-gw profile attach <id>` to issue a
fresh activation token and re-run the registration action.

**The sync missed alerts.**  The cursor keeps a `SYNC_LOOKBACK_MINUTES`
overlap.  To replay a wider window, delete the cursor row and run a sync with
a larger lookback:

```bash
sqlite3 .opsramp-sync/state.db "DELETE FROM sync_cursor;"
SYNC_LOOKBACK_MINUTES=1440 opsramp-gw sync --once
```

**Records were created with the wrong fields.**  Fix the mapping YAML, then
run the sync again.  Because the correlation id is stable, the sync patches
the existing records instead of creating new ones.

**A password needs rotating.**  `opsramp-gw admin password change` changes the
appliance password.  The value has to be updated in the secret store as well.

**Integration events failed to deliver.**  Inspect the activity log and replay
the failures (the retry window may not exceed two hours):

```bash
opsramp-gw integration activity --installed-id INTG-... --status FAILED \
    --since 2026-01-01T00:00:00Z --until 2026-01-01T01:00:00Z
opsramp-gw integration retry --installed-id INTG-... \
    --since 2026-01-01T00:00:00Z --until 2026-01-01T01:00:00Z
```

---

## Development

```bash
make venv        # create .venv and install the package with dev extras
make lint        # ruff check + format check
make format      # apply ruff autofixes and formatting
make typecheck   # mypy
make test        # pytest
make coverage    # pytest with a coverage report
make build       # build the wheel and sdist
```

---

## License

Proprietary.
