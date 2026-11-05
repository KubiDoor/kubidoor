# Architecture

KubiDoor is a single Python process: a FastAPI app plus an in-process asyncio scheduler. Services are
wired by hand in one composition root (`kubidoor/container.py`); no global state, no framework magic.

```mermaid
flowchart LR
    subgraph Cluster[Kubernetes cluster]
        API[(Kubernetes API)]
        MS[metrics-server<br/>optional]
    end

    subgraph KubiDoor[KubiDoor process]
        direction TB
        SCH[Scheduler<br/>asyncio loop] --> MON[Monitoring engine]
        MON -->|read-only lists| GW[Kubernetes gateway<br/>kubernetes/client.py]
        MON --> CHK[Pure health checks<br/>monitoring/checks.py]
        MON --> DIAG[Diagnostic engine<br/>rule-based]
        MON --> INC[Incident service]
        MON -.->|propose, only if enabled| REC[Recovery engine]
        REST[REST API<br/>FastAPI] --> INC
        REST --> DIAG
        REST --> REC
        REC -->|only 2 writes| GW
        REC --> INC
        INC --> AUD[Audit service<br/>append-only]
        REC --> AUD
        INC --> DB[(SQLite / PostgreSQL)]
        REC --> DB
        AUD --> DB
        MET[Prometheus metrics] --- REST
    end

    GW --> API
    GW -.-> MS
    PROM[Prometheus] -->|scrape /metrics| MET
    GRAF[Grafana] --> PROM
    USER[Operator / CI] --> REST
```

## Layers and responsibilities

| Layer | Package | Responsibility |
|---|---|---|
| API | `api/` | HTTP, validation, pagination, error format. No business logic. |
| Kubernetes integration | `kubernetes/` | Parse API objects into plain dataclasses, translate errors, the only 2 writes. |
| Monitoring | `monitoring/` | Per-cycle inspection (`engine.py`), pure rules (`checks.py`), scheduling. |
| Incidents | `incidents/` | Fingerprints, dedup, lifecycle, queries. |
| Diagnostics | `diagnostics/` | Deterministic rules: facts vs probable cause vs confidence vs actions. |
| Recovery | `recovery/` | Policy (pure), actions, verification (pure judgments), orchestration. |
| Database | `database/` | SQLAlchemy models, UTC datetime type, sessions, migrations. |
| Metrics | `metrics/` | `kubidoor_*` Prometheus metrics. |
| Audit/logging | `audit/`, `logging/` | Append-only audit trail; JSON logs with secret redaction. |

## Design decisions

* **The gateway is a `Protocol`** (`KubeGateway`). Everything above it is tested against an in-memory fake;
  no unit test needs a cluster.
* **Checks and policies are pure functions** (no I/O, time injected). Every rule is unit-testable and
  deterministic.
* **Parsed domain objects, not raw API objects**, cross the gateway boundary. They carry no env vars or
  Secret data, so the REST API cannot leak them.
* **Time is injected** (`Clock`), so cooldowns, thresholds and verification are tested without sleeping.
* **Atomic state transitions** use `UPDATE ... WHERE status = <expected>`; exactly one concurrent caller
  wins (see [recovery-policy.md](recovery-policy.md)).
* **One process, one monitoring loop.** An in-process lock skips overlapping cycles. Running several
  replicas is not supported (see Known limitations in the README).

## Data model

```mermaid
erDiagram
    incidents ||--o{ recovery_actions : "has"
    incidents {
        string id PK
        string fingerprint UK
        string status
        string severity
        int occurrence_count
        int reopen_count
    }
    recovery_actions {
        string id PK
        string incident_id FK
        string action_type
        string status
        bool dry_run
        string resource_uid
    }
    monitoring_history { int id PK }
    audit_logs { int id PK }
```

`audit_logs` rows reference incidents/actions by id **without** a foreign key on purpose, so the audit
trail survives any future data pruning. All timestamps are timezone-aware UTC (naive datetimes are rejected).
