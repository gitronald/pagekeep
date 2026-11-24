---
id: 0
slug: core-fetch-and-store
status: draft
branch:
created: 2026-11-23T18:07:02-08:00
concluded:
pr:
---

# Build the fetch, pace, and store core

## Plan

### Goal

pagekeep fetches a web page or document politely and safely, records what
the fetch can vouch for, keeps the body in a store, and remembers the pace
across runs. This plan builds its core, in the standard library only.
Anything heavier (HTML metadata, browser capture) is an optional extra, in a
plan of its own.

### Modules

#### `pagekeep.client`: who is asking

```python
@dataclass(frozen=True)
class Client:
    name: str                 # the calling tool, "pagekeep" by default
    version: str
    note: str | None = None   # what the requests are for
    url: str | None = None    # a project URL, never a personal contact

    @property
    def user_agent(self) -> str: ...  # "mytool/1.4 (source check; https://example.org/mytool)"
```

- The User-Agent is always built from a `Client`. There is no browser preset
  and no free-form User-Agent parameter: a caller that wants to send a
  browser's string has to subclass and say so in its own code.

#### `pagekeep.fetch`: one guarded request

```python
def fetch(
    url: str,
    *,
    client: Client,
    timeout: float = 20.0,
    max_bytes: int = 5 * 2**20,
    max_redirects: int = 10,
    expect: Literal["any", "document", "html"] = "any",
    robots: bool = False,
    retry: RetryPolicy = RetryPolicy(),
    pacer: Pacer | None = None,
    events: EventLog | None = None,
    transport: Transport = urllib_transport,   # injectable, for tests
) -> Fetched
```

- **Guards**: http or https only; a host and a valid port; every resolved
  address `is_global`; the same check on every redirect, in the redirect
  handler. A refusal is outcome `refused`, with no request sent.
- **Caps**: the raw read and any gzip or deflate output are capped at
  `max_bytes`. A cut body is kept and flagged `truncated`, not dropped.
- **`expect="document"`**: a response that is HTML, or a `.pdf` URL that
  answered with something else, is `not-a-document`. Its body is kept as
  evidence but never stored as the document.
- **`robots.txt` is opt-in.** With `robots=True`, a host's `robots.txt` is
  read once per process with `urllib.robotparser` and matched against the
  client's name. A disallowed URL gets outcome `disallowed`, with no request.
  The `robots.txt` fetch is paced like any other.

```python
@dataclass(frozen=True)
class Fetched:
    requested_url: str
    final_url: str | None          # after redirects; None when nothing answered
    redirects: tuple[str, ...]     # each hop's URL, in order
    status: int | None
    outcome: Outcome
    content_type: str | None
    fetched_at: str                # ISO 8601 with offset, to the second
    elapsed_ms: int
    attempts: int
    bytes: int | None              # body length received, after decoding
    sha256: str | None             # of those bytes, before any redaction
    truncated: bool
    error: str | None              # "<ExcType>: <reason>"
    body: bytes = field(repr=False, default=b"")
```

`Outcome` is one of:

| Outcome | When |
|---|---|
| `ok` | 2xx, and the type passes `expect` |
| `blocked` | 401, 403, 429, or 503 left after retries |
| `missing` | 404 or 410 |
| `not-a-document` | as above |
| `failed` | a transport error, a timeout, or any other status |
| `refused` | the address guard stopped it before any request |
| `disallowed` | `robots=True` and `robots.txt` forbids the URL |
| `moved` | 2xx at a final URL on another site; the caller decides what counts as the same site |

Every field is present on every record, `None` when unknown.

#### `pagekeep.retry`: when to ask again

```python
@dataclass(frozen=True)
class RetryPolicy:
    statuses: frozenset[int] = frozenset({429, 503})
    connection_errors: bool = True
    max_retries: int = 3
    backoff_base: float = 2.0      # base * 2**attempt, plus up to half a step of jitter
    max_wait: float = 60.0         # caps backoff and Retry-After alike

NO_RETRY = RetryPolicy(statuses=frozenset(), connection_errors=False, max_retries=0)

def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None: ...
```

- A `Retry-After` above `max_wait` ends the retries at once, as `blocked`,
  rather than sleeping: a server can ask for a day.
- Building a policy with 401 or 403 in `statuses` raises.
- `retry_after_seconds` and the backoff come from citefinder's `_base.py`,
  with their tests, so citefinder can later import them from here.

#### `pagekeep.pace`: how often, across runs

```python
class Pacer:
    def __init__(
        self,
        *,
        host_gap: float = 10.0,
        global_gap: float = 2.0,
        jitter: float = 0.5,
        events: EventLog | None = None,   # seeds each host's last request from the log
        clock=time.monotonic, wall=time.time, sleep=time.sleep, rand=random.random,
    ): ...
    def wait(self, host: str) -> float: ...     # seconds slept
    def note(self, host: str) -> None: ...      # record a request made outside fetch()
```

- A global gap plus a per-host gap. `fetch` calls `wait` for the requested
  host and `note` for each host a redirect lands on.
- The 10 s per-host default suits the Internet Archive; a caller sets less
  for a host it knows.
- `PacerLike` is the protocol (`wait(host)`, `note(host)`), so another
  package can share a pace with pagekeep's fetches without depending on it.
- One process, one thread. Several workers are out of scope.

#### `pagekeep.events`: the request log

```python
class EventLog:
    def __init__(self, path: Path, *, run_id: str, script: str): ...
    def __call__(self, event: str, **fields) -> None: ...   # one JSONL line, flushed
    def last_request_at(self, host: str | None = None) -> float | None: ...
```

- Events: `run_start`, `request` (host, URL, attempt, slept, elapsed,
  status, outcome, bytes), `retry`, `stop`, and `run_end`. Each line carries
  `ts`, `run_id`, and `script`.
- **Redacted at write**: a URL is logged as scheme, host, and path. The query
  string is dropped, since it can carry tokens or keys.
- Reading the last line per host survives a torn last line.

#### `pagekeep.stop`: when a batch should give up

```python
class StopAfter:
    def __init__(self, n: int = 3, outcomes: Iterable[Outcome] = ("blocked", "failed")): ...
    def see(self, fetched: Fetched) -> str | None: ...   # a reason once n in a row match, else None
```

- A host that refuses everything for a while should end a run, not be
  retried through. The caller owns the loop and calls `see` after each
  fetch. Any outcome outside the set resets the count. When `see` returns a
  reason, the caller logs a `stop` event and ends the run.

#### `pagekeep.store`: records and bodies

```python
class BodyStore(Protocol):
    def put(self, record: Fetched) -> str | None: ...   # returns a body reference
    def get(self, ref: str) -> bytes: ...

class FileBodies(BodyStore): ...    # <dir>/<sha256[:16]><suffix>; suffix from the bytes
class JsonlBodies(BodyStore): ...   # one {"ref", "body_b64" | "text"} line per body

class Index:
    def __init__(self, path: Path, bodies: BodyStore | None): ...
    def add(self, key: str, fetched: Fetched, **extra) -> dict: ...  # appends one line
    def latest(self, key: str) -> dict | None: ...
    def pending(self, keys: Iterable[str], *, retry: Iterable[Outcome] = ()) -> list[str]: ...
```

- One record format, a JSONL index, for every store; the body sits behind
  `BodyStore`.
- An index line is the `Fetched` fields without `body`, plus `key` (the
  caller's: a cite key, an id, a URL), `body_ref`, and the caller's `extra`
  fields.
- Append-only, the latest line per key winning, never rewritten in place. A
  torn last line is healed before the next append. `pending` skips keys that
  already have a line and retries only the outcomes asked for.
- **The suffix comes from the bytes**: `%PDF` gives `.pdf`, zip magic
  `.docx`, else the content type. Bodies are named by hash, so a refetch of
  the same content writes no new file, and a synced folder does not upload
  it again.
- A redaction hook, `redact: Callable[[bytes, str], bytes] | None`, runs
  before `put`. The line then carries `stored_sha256` beside `sha256`, so the
  raw hash still describes what the server sent.

#### `pagekeep.download`: a pinned file

`download(url, dest, *, sha256, client)` streams to `<dest>.part`, checks a
pinned hash, renames into place, and cleans up on any failure.

### Package shape

- Python 3.11 and up. No runtime dependencies in core: the scaffold's
  `typer` dependency and its `hello` CLI stub are removed, and a CLI, if one
  is wanted, comes later as an extra.
- Tests are offline. `transport`, `clock`, and `sleep` are injected, and one
  test runs against a local `http.server` for redirects and the byte cap.
- Fixtures are synthetic: example hosts (`example.org`), invented records.

### Implementation order

1. `client`, `retry`, and `Fetched` with `Outcome`, with tests.
2. `fetch` with the guards, caps, `expect`, and retries, against the
   injected transport and the local server.
3. `events` and `pace`, then `fetch`'s use of them.
4. `store`: `FileBodies`, `JsonlBodies`, `Index`, and the redaction hook.
5. `stop` and `download`.
6. `robots=True`.
7. README usage, changelog, and the first prerelease.

### Out of scope

- HTML metadata extraction and browser capture (later extras).
- Wayback lookups, which stay in the wayback package.
- Site discovery (robots and sitemap walks).
- The batch loop itself: each caller keeps its own loop and order.
- Several workers sharing one pace.
