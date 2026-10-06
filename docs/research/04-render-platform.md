# Render platform research for TokenSage (as of 2026-10-05)

## 0. How this was researched (read first)

- **render.com, docs.render.com and the render-web.onrender.com docs mirror were all blocked by this sandbox's egress proxy.** I could not open any Render docs page directly.
- **Primary source used instead:** Render's own official agent-skills repo, `github.com/render-oss/skills` (MIT, `author: Render`, 21 skills). I downloaded it from raw.githubusercontent.com. Render maintains these files as a condensed version of its docs. Cited below as `[skills:<path>]`, for example `[skills:render-blueprints/references/field-reference.md]`, which lives at `https://github.com/render-oss/skills/blob/main/skills/<path>`.
- **Secondary source:** WebSearch result snippets. Most are quoted from render.com/docs pages, and the URL is given inline. Some come from third-party pricing blogs, and those are flagged.
- **Pricing changed in April 2026.** I could only confirm the new prices through third-party sources. **Check https://render.com/pricing before you budget.**
- Uncertain items are marked **[UNVERIFIED]** or **[CONFLICT]**.

---

## 1. Blueprint spec (render.yaml)

### 1.1 File location and sync
- By default the file is `render.yaml` at the repo root. **Custom filenames and paths are now supported.** Set the "Blueprint Path" when you create the Blueprint, or change it later in Blueprint Settings (https://render.com/changelog/blueprints-now-support-custom-filenames-and-paths, https://render.com/docs/infrastructure-as-code).
- **Auto Sync defaults to Yes.** Each push to the linked branch updates the affected resources. If you set Auto Sync to No, you sync by hand with "Manual Sync" (https://render.com/docs/infrastructure-as-code).
- You can still edit Blueprint-managed resources in the Dashboard. However, **any Dashboard change that conflicts with the Blueprint is overwritten on the next sync** (same source).
- **If you delete a Blueprint-managed resource in the Dashboard, Render recreates it on the next sync.** To remove a resource for good, first remove it from the YAML and sync, then delete the now-unmanaged resource (same source).
- Validation:
  - JSON Schema: `https://render.com/schema/render.yaml.json`. Point your IDE at it.
  - CLI: `render blueprints validate`, which needs Render CLI v2.7.0+ [skills:render-blueprints/SKILL.md].

### 1.2 Top-level keys [skills:render-blueprints/SKILL.md, render-deploy/references/blueprint-spec.md]

| Key | Purpose |
|---|---|
| `services` | Services of type `web`, `pserv`, `worker`, `cron`, `keyvalue` (deprecated alias `redis`). A static site is `type: web` with `runtime: static`. |
| `databases` | Managed Postgres instances. |
| `envVarGroups` | Reusable sets of env vars. |
| `projects` | Optional. Each project contains `environments`, and each environment contains its own `services`, `databases` and `envVarGroups`, plus optional `networking.isolation` and `permissions.protection`. |
| `ungrouped` | Resources that are explicitly outside any environment (when you use projects). |
| `previews` | Preview environments: `generation` (`off`, the default, or `manual` or `automatic`) and `expireAfterDays`. |

**Projects and environments ARE supported in Blueprints.** Rules:
- Define each resource exactly once, either at the root or inside one environment, never both.
- `fromService` and `fromDatabase` only resolve within the same environment. There is no cross-environment wiring.
- Environment network isolation needs a Pro workspace or higher.

Source: [skills:render-blueprints/references/field-reference.md]

### 1.3 Service fields [skills:render-blueprints/references/field-reference.md, render-deploy/references/blueprint-spec.md]

| Field | Notes |
|---|---|
| `type` | `web`, `pserv`, `worker`, `cron` or `keyvalue`. **Immutable.** |
| `name` | Unique. Used by `fromService` references. |
| `runtime` | `node`, `python`, `go`, `ruby`, `rust`, `elixir`, `docker`, `image` or `static`. Documented as **immutable**. **[CONFLICT]** A Render changelog entry, "Change an existing service's runtime via API or Blueprint" (https://render.com/changelog/change-an-existing-services-runtime-via-api-or-blueprint), suggests runtime can now be changed. Verify before relying on either behaviour. The deprecated key `env` was replaced by `runtime`. |
| `plan` | Compute services: `free` (web only), `starter`, `standard`, `pro`, `pro plus`, `pro max`, `pro ultra`. The default for a new service is `starter`. |
| `region` | `oregon` (default), `ohio`, `virginia`, `frankfurt`, `singapore`. Region cannot be changed after creation (https://render.com/docs/regions). |
| `branch`, `repo` | Branch defaults to the repo's default branch. |
| `rootDir` | Monorepo subdirectory. |
| `buildCommand`, `startCommand` | |
| `preDeployCommand` | Runs after the build and before the deploy, on a **separate instance**. Filesystem changes there are not kept. **30-minute timeout.** Uses pipeline minutes. Documented for **paid** web, private and worker services (https://render.com/docs/deploys). **[CONFLICT]** One search snippet of /docs/free claimed free web services support it. Assume paid only. |
| `autoDeployTrigger` | `commit` (default), `checksPass` or `off`. Replaces the deprecated `autoDeploy`. If both are present, `autoDeployTrigger` wins. |
| `maxShutdownDelaySeconds` | 1 to 300, **default 30**. This is the gap between SIGTERM and SIGKILL. |
| `healthCheckPath` | Web services only. |
| `domains` | Custom domains. |
| `envVars` | See 1.5. |
| `buildFilter` | `paths` and `ignoredPaths` globs. **On sync, a missing key is treated as empty, which replaces the existing filters.** Always send both lists in full. |
| `disk` | `{name, mountPath, sizeGB}`. Paid web, pserv and worker only. |
| `numInstances` | Manual instance count. |
| `scaling` | `minInstances`, `maxInstances`, `targetCPUPercent`, `targetMemoryPercent`. **Autoscaling needs a Pro workspace or higher.** |
| `dockerfilePath` (default `./Dockerfile`), `dockerContext` (default `.`), `dockerCommand` (overrides CMD), `registryCredential.fromRegistryCreds.name` | Fields for `runtime: docker`. |
| `image.url`, `image.creds.fromRegistryCreds.name` | Fields for `runtime: image`. **Prebuilt images do NOT auto-deploy when a registry tag moves.** Use a deploy hook instead. |
| `schedule` | Cron services only. 5-field expression in UTC. **Quote it in YAML.** |
| `previews` (service level) | `generation` (`manual` or `automatic`) turns on that service's own PR previews. `plan` and `numInstances` size the service in preview environments. |
| `maxmemoryPolicy`, `ipAllowList`, `previewPlan` | Key Value fields. **`ipAllowList` is REQUIRED for Key Value**, and validation fails without it. `[]` means internal-only access. |
| `staticPublishPath`, `headers`, `routes` | Static site fields. |

### 1.4 Database fields [skills:render-blueprints/references/field-reference.md]
- **Immutable after creation:** `name`, `databaseName`, `user`, `region`, `postgresMajorVersion` (a string such as `"17"`; if omitted, the latest supported version is used).
- Mutable fields:
  - `plan`
  - `diskSizeGB` (paid plans only)
  - `storageAutoscalingEnabled`
  - `highAvailability: {enabled: true}` (Pro or Accelerated plans, PG13+)
  - `readReplicas: [{name: ...}]`. **Setting this to an empty list deletes every replica.**
  - `ipAllowList`
  - `previewPlan`, `previewDiskSizeGB`
  - `connectionPool: pgbouncer` (paid plans only; enabling it restarts the DB) [skills:render-postgres/references/connection-guide.md]
- Postgres `ipAllowList` defaults:
  - **Omitted:** allow all (`0.0.0.0/0`, credentials still required).
  - **`[]`:** no external access, private network only.
  - Sources: https://render.com/docs/postgresql-creating-connecting and the blueprint-spec search snippet.

### 1.5 Env vars [skills:render-blueprints/references/wiring-patterns.md, render-env-vars/references/wiring-reference.md]
Each `envVars` entry has a `key` plus exactly one of the following:

- `value: "..."`: a literal value.
- `generateValue: true`: a random base64 256-bit value. It is generated **once** at creation and is not regenerated on later syncs.
- `sync: false`: the Dashboard asks for the value.
  - It only prompts on the **initial** Blueprint creation. If you add a new `sync: false` key to an existing Blueprint, there is **no prompt**, and you must set the value in the Dashboard yourself.
  - Values are kept across syncs.
  - **Not copied to preview environments.**
  - **Invalid inside envVarGroups**, where it is silently ignored.
- `fromDatabase: {name, property}`, where `property` is one of:
  - `connectionString` (the **internal** URL)
  - `connectionPoolString` (PgBouncer)
  - `host`, `port`, `user`, `password`, `database`
- `fromService: {type, name, property | envVarKey}`:
  - Key Value properties: `connectionString`, `host`, `port`, `hostport`.
  - pserv and web properties: `host`, `hostport`. `envVarKey` copies another service's env var.
  - **`type` is required.**
- `- fromGroup: <group-name>`.

Env var groups can only hold `value` and `generateValue` entries. They cannot use `fromDatabase`, `fromService` or `fromGroup`.

### 1.6 Example render.yaml for TokenSage

Notes on this example:
- It is Docker-based, so tesseract and other apt packages are available.
- It runs a web API with the dashboard, a websocket ingest worker, a cron job, Postgres and Key Value.
- Everything is in **one region**, which the private network requires.

```yaml
# render.yaml  (validate: render blueprints validate)
previews:
  generation: off

envVarGroups:
  - name: tokensage-shared
    envVars:
      - key: LOG_LEVEL
        value: info
      - key: PUMP_WS_URL
        value: wss://pumpportal.fun/api/data   # example feed; adjust
      - key: ENABLE_CLIP
        value: "false"

services:
  # ---------- Web API + dashboard ----------
  - type: web
    name: tokensage-api
    runtime: docker
    plan: starter            # 'free' works but spins down after 15 min idle; no preDeploy on free
    region: oregon
    dockerfilePath: ./Dockerfile
    dockerContext: .
    dockerCommand: sh -c "uvicorn tokensage.api:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"
    preDeployCommand: alembic upgrade head      # paid plans only; 30-min timeout; runs on separate instance
    healthCheckPath: /healthz
    autoDeployTrigger: commit
    maxShutdownDelaySeconds: 30
    buildFilter:
      paths: ["tokensage/**", "Dockerfile", "pyproject.toml", "uv.lock", "alembic/**", "render.yaml"]
      ignoredPaths: ["**/*.md", "tests/**"]
    envVars:
      - fromGroup: tokensage-shared
      - key: DATABASE_URL
        fromDatabase: { name: tokensage-db, property: connectionString }
      - key: REDIS_URL
        fromService: { type: keyvalue, name: tokensage-kv, property: connectionString }
      - key: API_ADMIN_TOKEN
        generateValue: true

  # ---------- Long-lived websocket ingester / analyzer ----------
  - type: worker
    name: tokensage-ingest
    runtime: docker
    plan: starter            # workers cannot be free; use 'standard' (2 GB) if CLIP is enabled
    region: oregon
    dockerfilePath: ./Dockerfile
    dockerContext: .
    dockerCommand: python -m tokensage.worker
    numInstances: 1          # keep exactly one websocket consumer
    maxShutdownDelaySeconds: 60
    autoDeployTrigger: commit
    buildFilter:
      paths: ["tokensage/**", "Dockerfile", "pyproject.toml", "uv.lock", "render.yaml"]
      ignoredPaths: ["**/*.md", "tests/**"]
    envVars:
      - fromGroup: tokensage-shared
      - key: DATABASE_URL
        fromDatabase: { name: tokensage-db, property: connectionString }
      - key: REDIS_URL
        fromService: { type: keyvalue, name: tokensage-kv, property: connectionString }
      - key: TWITTER_BEARER_TOKEN
        sync: false          # prompted only at initial Blueprint creation

  # ---------- Periodic maintenance ----------
  - type: cron
    name: tokensage-rescore
    runtime: docker
    plan: starter
    region: oregon
    schedule: "*/15 * * * *"     # UTC; quote it
    dockerfilePath: ./Dockerfile
    dockerContext: .
    dockerCommand: python -m tokensage.jobs.rescore
    envVars:
      - fromGroup: tokensage-shared
      - key: DATABASE_URL
        fromDatabase: { name: tokensage-db, property: connectionString }
      - key: REDIS_URL
        fromService: { type: keyvalue, name: tokensage-kv, property: connectionString }

  # ---------- Key Value (Valkey 8) ----------
  - type: keyvalue
    name: tokensage-kv
    plan: starter              # 'free' = 25 MB, no persistence
    region: oregon
    maxmemoryPolicy: allkeys-lru   # use 'noeviction' if used as a job queue
    ipAllowList: []                # REQUIRED; [] = private network only

databases:
  - name: tokensage-db
    plan: basic-256mb          # 'free' expires after 30 days
    region: oregon
    databaseName: tokensage
    user: tokensage
    postgresMajorVersion: "17"
    diskSizeGB: 5              # [UNVERIFIED] storage sizing/increments; check dashboard
    ipAllowList: []            # no public access; omit => 0.0.0.0/0 allowed
```

Alternative: a **native Python** runtime with uv (no apt packages) would replace the docker fields with the following.

```yaml
    runtime: python
    buildCommand: uv sync --frozen
    startCommand: uv run uvicorn tokensage.api:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.11      # fully qualified when set via env var
```

---

## 2. Instance types and pricing

### 2.1 Compute: web, private service, worker [skills:render-scaling/references/instance-types.md, render-deploy/references/blueprint-spec.md; prices from search snippets of render.com/pricing and third-party summaries]

| Plan | CPU | RAM | Price/mo | Availability |
|---|---|---|---|---|
| free | 0.1 | 512 MB | $0 | **Web services only** |
| starter | 0.5 | 512 MB | $7 | web, pserv, worker |
| standard | 1 | 2 GB | $25 | web, pserv, worker |
| pro | 2 | 4 GB | $85 | web, pserv, worker |
| pro plus | 4 | 8 GB | ~$175 [UNVERIFIED] | web, pserv, worker |
| pro max | 4 | 16 GB | ~$225 [UNVERIFIED] | web, pserv, worker |
| pro ultra | 8 | 32 GB | ~$450 [UNVERIFIED] | web, pserv, worker |

- Billing is prorated by the second.
- **Cron jobs:** billed per running minute by instance type. The Starter rate is about $0.00016/min. There is a **$1/month minimum per cron service** (search snippets of https://render.com/pricing and https://render.com/docs/cronjobs). Cron jobs are not free.

### 2.2 Workspace plans: **new pricing from 2026-04-23; legacy workspaces auto-migrated 2026-08-01**
**[UNVERIFIED]** This comes from third-party sources only (bex.co blog posts, whichdevtool, checkthat.ai, all summarised by WebSearch). Confirm at render.com/pricing.

| Plan | Fee | Included bandwidth | Build minutes |
|---|---|---|---|
| Hobby | $0 | 5 GB (was 100 GB) | 500 |
| Pro | $25/mo flat (was "Professional" at $19 per member) | 25 GB | 1,000 |
| Scale | $499/mo | 1 TB | 5,000 |
| Enterprise | custom | | |

- Bandwidth overage is **$0.15/GB**.
- Extra build minutes cost **$5 per 1,000**.
- The Hobby plan allows up to 25 services and 2 custom domains.
- **Impact on TokenSage:** if the dashboard serves token images or proxies them, 5 GB/month of egress on Hobby can run out quickly. Serve image URLs directly from IPFS or the CDN rather than proxying them through Render.

### 2.3 Postgres (flexible plans, introduced Oct 2024: compute and storage billed separately)
- Basic plans, all with a 100-connection limit (search snippet, third-party summary of render.com/pricing):

  | Plan | CPU | RAM | Price/mo |
  |---|---|---|---|
  | `basic-256mb` | 0.1 | 256 MB | **$6** |
  | `basic-1gb` | 0.5 | 1 GB | **$19** |
  | `basic-4gb` | 2 | 4 GB | **$75** |

- `pro-*` plans (4 GB to 512 GB, CPU:RAM 1:4) and `accelerated-*` plans (16 GB to 1024 GB, CPU:RAM 1:8). Plan IDs come from [skills:render-deploy/references/blueprint-spec.md]. I did not obtain reliable prices for these.
- **Storage is $0.30/GB/month.** It can grow but never shrink. One source says it grows in 5 GB increments **[UNVERIFIED]**.
- **Free Postgres:**
  - 1 GB storage, 256 MB RAM, 0.1 CPU.
  - **One per workspace.**
  - **Expires 30 days after creation**, followed by a **14-day grace period** to upgrade before it is deleted (https://render.com/docs/free via search).
  - No PgBouncer, and **no backups** **[UNVERIFIED: backups]**.
- Legacy plan names (starter/standard, etc.) still exist on old DBs: https://render.com/docs/postgresql-legacy-instance-types.

### 2.4 Key Value (Valkey 8; instances created before Feb 2025 run Redis 6) [skills:render-keyvalue/SKILL.md; prices from search snippets]

| Plan | RAM | Max connections | Price/mo |
|---|---|---|---|
| free | 25 MB | 50 | $0 |
| starter | 256 MB | 250 | $10 |
| standard | 1 GB | 1,000 | $32 |
| pro and larger | | | [not retrieved] |

- **Free Key Value has no persistence.** Data is lost on restart, and also lost when you upgrade from free.
- Paid instances persist to disk with `appendfsync everysec`, so up to 1 second of writes can be lost.
- You can upgrade a Key Value instance (about 1-2 minutes of downtime), but **you cannot downgrade it**.

### 2.5 Persistent disks
- **$0.25/GB/month** (search snippet of https://render.com/docs/disks).
- Constraints [skills:render-disks/SKILL.md]:
  - **The service is limited to a single instance**, and **zero-downtime deploys are disabled.**
  - The disk is not available during the build or preDeploy steps, or to cron jobs or one-off jobs.
  - Size can only increase.
  - Snapshots are taken every 24 hours and kept for at least 7 days.
  - Mount paths you cannot use: `/`, `/opt`, `/opt/render`, `/opt/render/project`, `/opt/render/project/src`, `/home`, `/home/render`, `/etc`, `/etc/secrets`.
- TokenSage should not need a disk. Use Postgres, and keep the ONNX model inside the image.

### 2.6 Free tier summary (https://render.com/docs/free via search; [skills:render-networking/SKILL.md])
- **Only web services, Postgres and Key Value can be free.** Background workers, private services and cron jobs cannot be free.
- **Free web services:**
  - **Spin down after 15 minutes without inbound traffic.** The next HTTP request or new websocket connection wakes them, and spin-up takes **about 1 minute**.
  - They draw on **750 free instance hours per workspace per calendar month**. Spun-down time doesn't count. If the hours run out, free services are suspended until the next month.
  - Not supported: persistent disks, SSH or Dashboard shell, one-off jobs, edge caching, scaling beyond one instance.
  - They **cannot receive private-network traffic**, but they can send it.
- **Consequence for TokenSage:** you **cannot** run the websocket ingester for free.
  - A worker needs at least Starter ($7).
  - A free web service hosting the ingester would sleep after 15 minutes, because outbound websocket traffic doesn't count as inbound.
  - **Cheapest always-on setup:** free web (API, sleeps) + Starter worker ($7) + basic-256mb Postgres ($6 + storage) + free or Starter Key Value. That is roughly **$13-25/month plus storage** on the Hobby workspace.

---

## 3. Python specifics

### 3.1 Version
- **Default Python for services created on or after 2026-02-11 is 3.14.3.** Older services keep their previous default.
- Ways to set the version:
  - `PYTHON_VERSION` env var: must be **fully qualified**, e.g. `3.13.5`. Any release from 3.7.3 up is allowed.
  - `.python-version` file in the repo root: the patch number may be omitted, e.g. `3.13`.
- Sources: https://render.com/docs/python-version, https://render.com/changelog/updated-version-defaults-python-to-3-14-3-uv-to-0-10-2.
- **Gotcha:** pin 3.12 or 3.13 explicitly. Heavy wheels such as onnxruntime, opencv and tesserocr may lag behind 3.14. **[UNVERIFIED]** Check wheel availability for 3.14.

### 3.2 Package managers (native runtime)
- **pip** is the default and uses `requirements.txt`.
- **uv** is supported natively. A `uv.lock` file must be in the service root. Set the version with `UV_VERSION`; the default is 0.10.2. Use `uv sync` as the build command (https://render.com/changelog/added-uv-to-the-python-native-runtime, search of https://render.com/docs/python-version).
- **Poetry** is auto-detected from `pyproject.toml`. `POETRY_VERSION` selects the version **[UNVERIFIED: env var name]**.
- **Pipenv** is auto-detected from `Pipfile`.

Source: [skills:render-deploy/references/runtimes.md]

### 3.3 Build caching
- **Native runtime:** Render keeps a build cache between builds, and the Dashboard has "Clear build cache & deploy". **[UNVERIFIED: exact cached dirs]**
- **Docker:** BuildKit layer caching between builds. BuildKit cache mounts (`RUN --mount=type=cache,target=/root/.cache/uv`) and secret mounts are supported [skills:render-docker/references/optimization-guide.md].

### 3.4 Native system dependencies
- **`apt-get` is NOT available on native runtimes** because of restricted permissions. Tesseract is not preinstalled.
- **Use `runtime: docker`** to get apt packages such as `tesseract-ocr` or `libgl1`. The other option is to download static binaries during the build.
- Sources: community.render.com/t/installing-tesseract/11213, https://render.com/docs/native-runtimes, community.render.com/t/install-custom-packages-on-my-deploy-do-i-need-docker/5857.

### 3.5 Docker on Render
- Builds use **BuildKit** and target **linux/amd64**. docker-compose is not supported. Each service is one container.
- **Bind to `0.0.0.0:$PORT`.** `PORT` defaults to `10000`. Exec-form CMD doesn't expand env vars, so use `sh -c` or a shell-form CMD, and use `exec` so the app receives SIGTERM.
- Secret files appear at `/etc/secrets/<name>`.
- Never put secrets in `ARG`. Use runtime env vars or BuildKit secret mounts.
- The `PYTHON_VERSION` and `UV_VERSION` env vars do not apply to Docker builds; pin versions in the Dockerfile.
- Sources: [skills:render-docker/SKILL.md, render-env-vars/references/platform-variables.md].

**Limits** (search snippets of https://render.com/docs/build-pipeline):
- Build timeout: **120 minutes**.
- Build machines:
  - Starter pipeline (default): **2 CPU / 8 GB RAM**.
  - Performance pipeline: **16 CPU / 64 GB RAM** (Pro workspace or higher).
- The build is cancelled if it exceeds the pipeline's memory or uses **more than 16 GB of disk**.
- **Max compressed image size: 10 GB.**

Recommended Dockerfile sketch:

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr libgl1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["sh","-c","exec uvicorn tokensage.api:app --host 0.0.0.0 --port ${PORT:-10000}"]
```

- Bake the ONNX model into the image at build time, either downloaded in a `RUN` step or committed with LFS.
- Don't download the model at startup. A startup download slows boot and health checks, and the filesystem is ephemeral.

### 3.6 Platform env vars [skills:render-env-vars/references/platform-variables.md]
- `RENDER=true`
- `RENDER_SERVICE_NAME`, `RENDER_SERVICE_TYPE`, `RENDER_INSTANCE_ID`
- `RENDER_GIT_COMMIT`
- `RENDER_EXTERNAL_URL`
- `RENDER_CPU_COUNT`
- `RENDER_WEB_CONCURRENCY` / `WEB_CONCURRENCY`: defaults changed for services created after 2025-12-08.
- `IS_PULL_REQUEST`
- `PORT`

---

## 4. Long-lived websocket client in a background worker

### 4.1 Workers and outbound traffic [skills:render-background-workers/SKILL.md, render-networking/SKILL.md]
- A worker is a long-running process with **no public URL and no internal hostname**. Nothing can connect *to* it.
- Workers **can make outbound connections** to the public internet (for example the pump.fun or PumpPortal websocket) and to Postgres, Key Value and private services.
- **Render restarts the process if it crashes or exits.** It has no HTTP health check.
- For outbound IP allowlisting there are shared regional outbound IP ranges (https://render.com/docs/outbound-ip-addresses). **Dedicated outbound IPs** need a Pro workspace and cost extra; each set has 3 IPv4 addresses.

### 4.2 Shutdown and deploy semantics
- On a deploy, restart or scale-in, Render sends **SIGTERM**, waits **`maxShutdownDelaySeconds`** (default 30, max 300), then sends **SIGKILL** [skills:render-background-workers/references/graceful-shutdown.md].
- **Deploys overlap.** Zero-downtime deploys start the new instance *alongside* the old one. After the switchover, the old instance keeps running about **60 seconds** before it gets SIGTERM. Sources: https://render.com/docs/zero-downtime-deploys and https://render.com/articles/how-render-handles-zero-downtime-deploys; for workers, community.render.com/t/background-worker-redeploy-question/1129.
- **Implication: for a short window, two ingesters are subscribed to the feed at the same time.** Design for this:
  1. Make writes idempotent, e.g. `INSERT ... ON CONFLICT (mint) DO NOTHING/UPDATE`. That alone is enough. If the analysis is expensive, you can also take a Redis or Postgres advisory-lock leader election with a TTL so only one instance processes events.
  2. On SIGTERM:
     - stop reading;
     - flush in-flight analyses or re-queue them (a Redis list or Postgres status column);
     - close the websocket and DB pools;
     - exit 0.
  3. Set `maxShutdownDelaySeconds: 60` or more if a single analysis can take a long time.
  4. **Reconnect logic is mandatory.** Use exponential backoff with jitter, use websocket ping/pong keepalives, and resubscribe after reconnecting.
  5. **Gap recovery:** events are lost while disconnected and during deploys. Have the cron job backfill recent tokens from a REST source.
  6. With asyncio, register the handler with `loop.add_signal_handler(signal.SIGTERM, stop_event.set)`.
- Inbound websockets on **web** services have no fixed Render timeout, but they close whenever the instance is replaced, e.g. on a deploy (https://render.com/docs/websocket). This matters if the dashboard pushes live updates to browsers over a websocket or SSE: clients must reconnect.

### 4.3 Cron jobs [skills:render-cron-jobs/SKILL.md; https://render.com/docs/cronjobs via search]
- Schedules are standard 5-field cron in **UTC**.
- **The minimum interval is technically 1 minute (`* * * * *`).** Each run has to start up, run and exit, so 1-minute schedules are "ambitious". 5 to 15 minutes is more realistic.
- **At most one active run** per cron service at a time. Overlapping ticks don't start a second run.
- A manual "Trigger Run" cancels the active run.
- **12-hour maximum** per run.
- **No persistent disk.**
- Cron jobs can send traffic on the private network but cannot receive it.
- Failed runs are **not retried** automatically.
- Docker-image crons pull the image fresh for every run.

### 4.4 Health checks (web) (https://render.com/docs/health-checks via search; [skills:render-web-services/references/health-check-patterns.md])
- Health checks send an HTTP GET to `healthCheckPath` and expect a 2xx or 3xx response.
- One snippet says checks run **every ~5 s**, and that this is not configurable. **[CONFLICT]** The skills docs say interval and timeout are configurable in settings.
- **After 15 s of failures, Render stops routing traffic** to the instance. **After 60 s of failures, it restarts the instance.**
- **During a deploy:** if the new instances aren't all healthy within **15 minutes**, the deploy is cancelled and the old instances keep serving.
- Keep `/healthz` cheap. A DB check makes the service "unhealthy" whenever the DB has a blip.

### 4.5 Logs and metrics
- Logs come from stdout/stderr. They can be viewed in the Dashboard, through the CLI (`render logs`), or sent to external log streams **[UNVERIFIED: syslog streams]**.
- Log and metrics retention by workspace plan (https://render.com/docs/logging, https://render.com/docs/service-metrics, https://render.com/docs/professional-features via search):

  | Workspace plan | Retention |
  |---|---|
  | Hobby | 7 days |
  | Pro | 14 days |
  | Scale/Enterprise | 30 days |

- Metrics cover CPU, memory, HTTP requests and latency.

### 4.6 Private network [skills:render-networking/SKILL.md, render-deploy/references/service-types.md]
- Services can only talk privately when they are in the **same workspace AND same region**.
- Which services have an internal hostname:
  - web and pserv: yes;
  - worker and cron: no (outbound only);
  - free web: can send but not receive.
- Use the internal address `host:port` (add `http://` when needed). Wire it with `fromService ... property: hostport`.
- Discovery DNS `<host>-discovery` resolves to every instance (`RENDER_DISCOVERY_SERVICE`).
- Up to 75 open ports. **Reserved ports: 10000, 18012, 18013, 19099.**
- **Architecture consequence:** the API cannot call the worker directly. Communicate through Postgres and Key Value (Redis pub/sub or streams).

---

## 5. Memory and OOM

- **OOM behaviour:** the process is killed (exit 137 / "Ran out of memory (used over 512MB)"), and Render restarts the instance and records an event. For a web service, the whole startup sequence runs again. Sources: community.render.com/t/server-unhealthy-ran-out-of-memory-used-over-512mb-while-running-your-code/14648, [skills:render-debug/references/error-patterns.md].
- There is no swap. **[UNVERIFIED]** The memory limit is hard.
- The metrics tab samples memory and can **miss short spikes**.
- Python frameworks size worker counts from `WEB_CONCURRENCY`, so set `WEB_CONCURRENCY=1` on 512 MB plans.

**Rough budget for TokenSage.** These are my estimates, not from Render docs, so profile them:

| Component | Approx RSS |
|---|---|
| CPython + FastAPI/uvicorn (1 worker) + SQLAlchemy/asyncpg | 80–150 MB |
| Pillow + imagehash + numpy + rapidfuzz / scikit-learn small models | +50–150 MB |
| pytesseract (subprocess per call) | +50–100 MB transient per call |
| onnxruntime library | +50–100 MB |
| CLIP ViT-B/32 **image encoder** fp32 (~88M params ≈ 350 MB weights) | +350–450 MB |
| CLIP ViT-B/32 text encoder fp32 (~63M params ≈ 250 MB) | +250–300 MB |
| CLIP both encoders int8-quantized | ~+150–200 MB total |

- **512 MB (free/starter):**
  - Fine for the API, and for the worker doing classical analysis (hashing, fuzzy matching, regex, OCR in short subprocesses).
  - **Not safe for fp32 CLIP.** Even int8 CLIP plus everything else is tight.
  - The 0.1 CPU free instance is very slow for any image work.
  - Starter has 0.5 CPU, so CLIP inference takes roughly 2x longer than on one core.
- **2 GB (standard, $25):** comfortable for the worker with fp32 CLIP (both encoders), image decode and OCR.
- **Recommendation:**
  - API on starter, or free while developing.
  - Ingest worker on starter with CLIP off.
  - Switch the worker to standard when `ENABLE_CLIP=true`.
  - Alternatively, run CLIP in a separate small worker that reads a Redis queue.
  - Load the model once at startup and use one onnxruntime `InferenceSession` with `intra_op_num_threads` set to `RENDER_CPU_COUNT`.
  - Bound image sizes before decoding, e.g. Pillow `draft()` and a max-pixels limit, so a malicious huge image can't OOM the worker.

---

## 6. Gotchas checklist

1. **Blueprint sync overwrites Dashboard edits** that conflict with the YAML. **Resources deleted in the Dashboard come back** on the next sync. Some fields are immutable: service `type`; database `name`, `databaseName`, `user`, `region`, `postgresMajorVersion`; the region of any service. Runtime is documented as immutable but see the [CONFLICT] in 1.3.
2. **`sync: false`** prompts only on the first Blueprint creation. Keys added later must be set by hand. Such values are not copied to previews and are not allowed in env groups.
3. **`generateValue`** is generated once and never rotated by sync.
4. **`buildFilter`:** always send both `paths` and `ignoredPaths`, because a missing key wipes that filter. Remember `render.yaml` and the Dockerfile in `paths`.
5. **`fromDatabase connectionString` is the INTERNAL URL.** It only works from Render services in the **same region**.
   - External URLs need TLS 1.2+ and `?sslmode=require`.
   - Using the external URL from inside Render adds latency.
   - Render URLs use the `postgres://` scheme. **SQLAlchemy 2 needs `postgresql://` or `postgresql+psycopg://` / `postgresql+asyncpg://`**, so rewrite the scheme in config code. asyncpg also doesn't accept the `sslmode` query parameter.
6. **Postgres `ipAllowList`:** if omitted it is open to `0.0.0.0/0` (password-protected). Set `[]` for private-only access. Use an explicit CIDR for your laptop when running psql or migrations.
7. **Key Value `ipAllowList` is required.** `[]` means internal-only access.
   - Internal URL `redis://red-xxx:6379` has **no auth by default**. You can enable internal auth in the Dashboard, but that breaks existing unauthenticated clients.
   - External URL `rediss://` requires an allowlist.
   - Use `noeviction` for queues and `allkeys-lru` for caches.
8. **Free Postgres expires after 30 days**, then has a 14-day grace period. Don't keep production data on it.
9. **Free web services sleep** after 15 minutes. Workers, cron jobs and pserv can't be free.
10. **All resources must be in one region** for the private network and internal DB URLs to work. The region can't be changed later.
11. **preDeployCommand** runs on separate compute and can't see a disk. It is paid only and has a 30-minute limit. Use it for `alembic upgrade head`.
12. **Worker deploys overlap** for about 60 seconds, so the ingest must be idempotent. Websocket clients connected to the web service drop on every deploy.
13. **Autoscaling, environment isolation, dedicated IPs and Performance build pipelines** all need the Pro workspace or higher.
14. **Bandwidth:** the Hobby workspace includes only 5 GB/month under the April 2026 pricing, with overage at $0.15/GB **[UNVERIFIED third-party]**.
15. **Python 3.14** is the new default. Pin the version so onnxruntime and other wheels resolve.
16. **Docker builds target amd64 only.** Use shell-form CMD for `$PORT` and `exec` so signals reach the app.
17. **Prebuilt-image services** (`runtime: image`) don't redeploy when a tag is updated. Use a deploy hook, or prefer `runtime: docker`.
18. **Deprecated keys to avoid:**
    - `env` → use `runtime`
    - `redis` → use `keyvalue`
    - `autoDeploy` → use `autoDeployTrigger`
    - `previewsEnabled` → use top-level `previews.generation`
    - `pullRequestPreviewsEnabled` → use service-level `previews.generation`

## Key sources
- Render official skills repo: https://github.com/render-oss/skills. Files used:
  - `skills/render-blueprints/{SKILL.md, references/*}`
  - `render-deploy/references/{blueprint-spec,runtimes,service-types}.md`
  - `render-docker/*`
  - `render-background-workers/*`
  - `render-cron-jobs/*`
  - `render-networking/SKILL.md`
  - `render-postgres/references/connection-guide.md`
  - `render-keyvalue/SKILL.md`
  - `render-disks/SKILL.md`
  - `render-env-vars/references/platform-variables.md`
  - `render-web-services/references/*`
  - `render-scaling/references/instance-types.md`
- Render docs, via search snippets only:
  - /docs/blueprint-spec
  - /docs/infrastructure-as-code
  - /docs/free
  - /docs/python-version
  - /docs/build-pipeline
  - /docs/deploys
  - /docs/zero-downtime-deploys
  - /docs/health-checks
  - /docs/websocket
  - /docs/cronjobs
  - /docs/disks
  - /docs/regions
  - /docs/postgresql-creating-connecting
  - /docs/outbound-ip-addresses
  - /docs/logging
  - /docs/service-metrics
  - /changelog/added-uv-to-the-python-native-runtime
  - /changelog/blueprints-now-support-custom-filenames-and-paths
- Pricing, third-party and unverified:
  - bex.co/blog/2026/07/31/render-pricing-overhaul-flat-tiers
  - bex.co/blog/2026/07/09/render-april-2026-egress-repricing-hobby
  - whichdevtool.com/tools/render
  - kuberns.com/blogs/render-pricing
  - srvrlss.io/provider/render
