# Agent runtime and MCP events

## Start with one command form

```bash
python3 scripts/airsprint_cli.py agent commands
python3 scripts/airsprint_cli.py agent commands --group customs
python3 scripts/airsprint_cli.py agent commands --query passport
python3 scripts/airsprint_cli.py agent describe --command 'customs prepare'
```

Discovery is offline. The first command lists groups; it does not load every
form. `describe` gives the exact option names and types, required fields,
side effects, workflow guidance and the corresponding MCP tool. Credentials
and the maintainer raw interface are excluded from the generated forms.

Normal CLI options remain available. Parse errors, unknown options, form
validation and API errors now use JSON with `code`, `retryable`,
`requestMayHaveSucceeded` and `nextAction`. A failed write is never an
instruction to retry. `trips list` and `messages list` include a `page` object;
continue with `--offset` only when more results are needed.

Booking-code lookup is case insensitive and rejects ambiguous or incomplete
results. The trip UUID, booked-leg UUID, passenger-profile UUID, leg-passenger
UUID, passport UUID and customs-link UUID are different identifiers.

## Keep one process running

```bash
python3 scripts/airsprint_cli.py agent serve
```

Write one JSON object per line on stdin and read one reply per line on stdout:

```json
{"id":1,"command":"trips list","arguments":{"limit":10,"upcoming":true}}
```

`arguments` contains the named options returned by `agent describe`, with
underscores instead of hyphens. It is a typed form, not an AirSprint request
body. Unknown options, wrong types and missing required fields are rejected
before execution. Booleans remain booleans; repeatable options use arrays.
Dates and yes/no answers retain the command's documented string form.

The runtime reuses the command tree, caches and TLS context. Calls are
serialized, preserving the CLI's existing per-command safety checks. In an
offline benchmark on the development Mac, median `auth status` overhead was
70.8 ms for a fresh process and 0.09 ms in a warm process (15 and 100 samples).
This measures local execution overhead; it does not measure AirSprint latency.
Reproduce with `python3 scripts/benchmark_agent.py`.

### Write receipts and interrupted calls

Writes, local draft edits and cache refreshes require a stable
`operation_key` in this runtime. An AirSprint write also requires
`confirm: true` after the user's authorization:

```json
{"id":2,"command":"messages read","arguments":{"id":"NOTIFICATION_UUID","confirm":true},"operation_key":"mark-notification-001"}
```

Before execution, the runtime commits the operation key and a hash of the
command arguments. Repeating the same completed operation returns its saved
result without another AirSprint request. Changing the arguments under the
same key is an error. An interrupted or uncertain operation is blocked; the
same arguments under a different key are also blocked while it is unresolved.

```bash
python3 scripts/airsprint_cli.py agent operation --operation-key mark-notification-001
```

Keep the operation key after timeouts. Reconcile uncertain outcomes using the
saved receipt and an appropriate authorized status check. There is no automatic
unlock or write retry. Customs retains its additional journal per leg passenger,
individual certifications and separate final submission approval.

The operation journal belongs to the agent/MCP runtime. Direct legacy CLI
commands retain their existing guards. Journals cannot prevent a different
machine or application from repeating an operation; AirSprint does not expose
an audited general idempotency-key contract.

## MCP server

Python runtime dependencies remain `typer` and `truststore`. The server
implements MCP 2.0 (`2026-07-28`), including per-request protocol metadata;
older `initialize` clients are rejected with a protocol error.

```bash
export AIRSPRINT_USERNAME='owner@example.com'
export AIRSPRINT_PASSWORD='...'
export AIRSPRINT_TIMEZONE='America/Toronto'

# Local client using MCP 2.0 over stdin/stdout
python3 scripts/airsprint_cli.py mcp serve --transport stdio

# HTTP endpoint at http://127.0.0.1:8765/mcp
# Supply a private random token of at least 32 ASCII characters.
export AIRSPRINT_MCP_TOKEN='...'
python3 scripts/airsprint_cli.py mcp serve --transport http --interval 60
```

One service serves one owner. HTTP requires `Authorization: Bearer TOKEN`.
Use HTTPS through a reverse proxy for a remote client and set
`AIRSPRINT_MCP_HOST` to the external hostname. Browser origins are denied by
default; explicitly allowed origins go in the comma-separated
`AIRSPRINT_MCP_ORIGINS`. Request headers `MCP-Protocol-Version`, `Mcp-Method`
and, for tool calls, `Mcp-Name` must match the JSON-RPC request.

Eight tools keep initial discovery small:

| Tool | Use |
| --- | --- |
| `airsprint_commands` | List groups or find commands by group/query |
| `airsprint_describe` | Read one exact command form |
| `airsprint_read` | Execute a read form; writes are rejected |
| `airsprint_run` | Execute an authorized write or local edit with an operation key |
| `airsprint_operation` | Inspect a saved operation receipt |
| `airsprint_events` | Inspect observed events in local sequence order |
| `airsprint_event_record` | Read the latest observed notification/trip record |
| `airsprint_event_status` | Check collection timestamps and delivery counts |

Authentication, password changes, raw requests and recursive server commands
are excluded from MCP. Read the `mayNotifyOwner` guidance before a booked-trip
detail read. Such reads are never used for event collection.

## Events

The authenticated MCP endpoint implements `events/list`, `events/subscribe`
and `events/unsubscribe`. ChatGPT supplies the HTTPS callback and signing
secret when the user requests monitoring.

| Event | Available filters |
| --- | --- |
| `notification.created` | `resourceId`, `tripId`, `action` |
| `notification.updated` | `resourceId`, `tripId`, `action` |
| `trip.created` | `legId`, `bookingId` |
| `trip.updated` | `legId`, `bookingId` |
| `customs.submitted` | Required `legId` |
| `operation.completed` | `command` |
| `operation.uncertain` | `command` |

Notifications cover any category present in AirSprint's in-app notification
feed. Trip updates include changes to schedule, route, status and passenger
information in the upcoming-leg collection. A missing item is not interpreted
as a cancellation. Customs monitoring requires a leg already observed for this
owner. Operation events describe agent-runtime writes; `operation.completed`
includes a `status` indicating success or a definite error.

### Collection and delivery

AirSprint has no audited push feed. While subscriptions are active, the service checks authorized accounts and
reads the paginated `/my-notifications` and `/my-leg` collections every 60
seconds by default. It reads `/myCanadianCustomsDeclaration` only for subscribed
legs. These are collection POST reads; it never polls `/trip/{id}` or
`/my-leg/{id}`. The initial complete snapshot is silent. Incomplete pages do
not advance that collection's snapshot. Collection failures delay delivery;
authentication failures revoke subscriptions. Errors go to stderr without
callback URLs or signing secrets.

Collections are bounded at 5,000 records per source per cycle. A larger or
inconsistent collection is reported as incomplete and is not used to infer
changes. This bounds API work and prevents silently treating a partial result
as a complete account history.

Webhook delivery includes:

- A signed challenge before activation; only public HTTPS destinations, with
  DNS validation on every connection, pinned addresses and no redirects.
- Standard Webhooks HMAC signatures over the exact body bytes, stable event
  IDs and fresh signing timestamps on each attempt.
- Persistent subscriptions, owner isolation, a 24-hour maximum/default lifetime,
  idempotent refresh/unsubscribe and a five-minute signing-key rotation window.
- Durable queued deliveries, bounded exponential retries for transient failures,
  and no retries for `410` or `413` responses.
- Small identifier/change summaries; document numbers and payment-card fields
  are omitted. Fetch notification text through the read tool and treat it as
  untrusted application content.

Events have `cursor: null`: protocol history replay is not supported. Queued
deliveries survive restarts, but changes missed while collection is stopped
can only be detected from the next available snapshot. Delivery is at least
once; receivers must deduplicate by `eventId`.

### Local inspection and storage

```bash
python3 scripts/airsprint_cli.py events collect
python3 scripts/airsprint_cli.py events status
python3 scripts/airsprint_cli.py events list --after 0 --limit 20
python3 scripts/airsprint_cli.py events get --source notifications --id NOTIFICATION_UUID
```

State defaults to `~/.airsprint-agent/state.sqlite3`, with a private directory
and mode-0600 database. Set `AIRSPRINT_AGENT_DB` to use another path. The owner
namespace is the normalized `AIRSPRINT_USERNAME` (`local` for offline CLI use
without a username). Preserve this database across restarts and deployments;
it contains write receipts, subscription secrets, observed records and queued
events. It is intentionally excluded from the repository. Journals are retained
until the operator archives them; there is no automatic deletion of uncertain
operations.

## Verification

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
ruff check scripts/*.py
python3 scripts/benchmark_agent.py
```

Tests use synthetic records. The transport test starts a local HTTP MCP server
and a local HTTPS callback, verifies subscription and signed delivery, reopens
the database, and unsubscribes. It makes no AirSprint requests. `openssl` is
required for its temporary test certificate.

For optional validation against the official MCP schema, install `jsonschema`
in the test environment and set `AIRSPRINT_MCP_SCHEMA` to its local JSON file.
The audited schema SHA-256 is
`ef70b61f99b6d2e5e3b46863822eab08dff6a45bedc7a08914e0e5b133f40203`.

This repository provides the server and tests. A real ChatGPT subscription
also requires a reachable HTTPS deployment and a connected plugin; the local
transport test does not establish that connection.

Protocol references: [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events),
[MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
[MCP Events proposal](https://github.com/modelcontextprotocol/experimental-ext-triggers-events/blob/main/docs/design-sketch-proposal.md).
