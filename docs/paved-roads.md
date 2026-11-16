# Paved roads: one safe helper per dangerous operation

Status: implemented in `fs`, `net`, `auth` and `limits` (ai-code-auditor#56). This note settles the behaviour of four shared helpers
before code depends on them. Each one replaces several local copies that grew separately across
the suite and disagree on the details.

A lint rule (argus-ci `suite-rules/`) catches the unsafe shape. These helpers are where the rule
sends people instead, so they have to be the shortest path: one import, one call, no
configuration needed for the common case.

Rules for all four:

- **Fail closed.** When the helper can't tell whether something is safe (a path it can't
  resolve, a host that won't resolve, a token that isn't configured), it refuses.
- **Raise one error type per helper**, a subclass of `ValueError` (or `PermissionError` for
  auth), and never a framework exception. Servers turn it into a 400/403/413 at their edge. The
  message never echoes a secret or a full URL with credentials.
- **Stdlib first.** `fs` needs nothing; `net` needs `httpx` (the `[remote]` extra); `auth` and
  `limits` need Starlette (the `[server]` extra). The top-level `import argus_cortex` stays
  pydantic-only.
- **Property-based tests** (hypothesis) cover the hostile inputs, not just examples.

## 1. `argus_cortex.fs`: paths from outside stay under their root

```python
safe_join(root, user_path, *, allow_root=False, allow_absolute=False) -> Path
safe_name(value, *, pattern=SAFE_NAME, max_len=128) -> str
safe_basename(filename) -> str
is_within(root, path) -> bool
PathRefused(ValueError)
```

| Question | Decision | Why |
|---|---|---|
| What is checked | The **resolved** path (symlinks followed) must be the root or under it | A lexical `..` check misses symlinks; `resolve()` doesn't |
| Return value | The resolved path | What was checked is what gets used (forge's lesson: checking one spelling and using another reopens the hole) |
| The root itself | Refused unless `allow_root=True` | Most callers want a file under the root; deleting or overwriting the root is the dangerous case |
| Absolute input | Refused unless `allow_absolute=True` (then it must still be inside) | Request input should be relative; a few callers legitimately take an absolute path already inside the root |
| Empty, NUL, `\` | Refused | NUL makes `resolve()` raise (a 500 in several servers today); `\` is a separator on Windows and never meant literally here |
| `~` | Not expanded | Request input shouldn't reach a home directory; `expanduser` belongs to operator config |
| Errors from `resolve()` | `OSError`, `RuntimeError` (symlink loop), `ValueError` become `PathRefused` | One error type, never a 500 |
| Single segments (ids, slugs) | `safe_name` uses `fullmatch` with an allow-list pattern that must start alphanumeric, so `.` and `..` can't match | `.match` with `$` lets a trailing newline through, which several id checks do today |
| Upload filenames | `safe_basename` keeps the last component across both `/` and `\`, then applies the `safe_name` rules but allows spaces | `Path(x).name` returns `..` for `..` and keeps control characters |

Not covered: the check-then-use race (a symlink swapped in after the check). Callers that write
into a directory other users can write to need `O_NOFOLLOW`/`openat`, not this helper.

## 2. `argus_cortex.net`: outbound fetches can't be steered at the inside

```python
check_url(url, *, allow_hosts=()) -> CheckedUrl
fetch(url, *, method="GET", content=None, headers=None, allow_hosts=(), timeout=30,
      max_bytes=50 MiB, max_redirects=5, deadline=120) -> Reply   # .status .headers .body .raise_for_status()
afetch(...)  # the same, async
is_public_ip(ip) -> bool
UrlRefused(ValueError), FetchFailed(RuntimeError)
```

| Question | Decision | Why |
|---|---|---|
| What is blocked | Every address that isn't **global** (private, loopback, link-local, CGNAT, multicast, reserved), checked on the **resolved** IPs; IPv4-mapped/compat IPv6, 6to4 and NAT64 forms are unwrapped and checked as IPv4 | Checking the hostname only (one local copy does) is beaten by any DNS name that points inside |
| Every address or one | **All** resolved addresses must be global | A name with one public and one private record is how a rebinding attack starts |
| Rebinding | The connection is **pinned** to the checked IP; the Host header and TLS SNI keep the original name | Checking then letting the client resolve again is a race |
| Redirects | Followed by hand for GET/HEAD, up to 5, each hop checked again; any other method gets the 3xx back | A public URL redirecting to a metadata address is the common bypass, and a POST body must never be re-sent to a host nobody checked |
| Error status | Returned in the `Reply`; `raise_for_status()` turns 4xx/5xx into `FetchFailed` | Callers that proxy a status need it; callers that don't call one method |
| Allow-list | Entries are `host` or `host:port`; an allow-listed host skips the public-address rule but is **still resolved and pinned** | An allow-list is for internal services by name, not a way to switch off pinning |
| Userinfo in the URL | Refused | Different parsers disagree about where the host is |
| Body | Capped (`max_bytes`) after decoding; a declared `Content-Length` over the cap is refused before reading; gzip/deflate are inflated only up to the cap, and any other `Content-Encoding` is refused | The fetch must not become a memory exhaustion, and httpx inflates a whole network chunk before any cap is checked |
| Credentials on redirect | `Authorization`, `Cookie` and `Proxy-Authorization` are dropped on a hop to another origin | A public URL must not be able to redirect a caller's token to a host of its choosing |
| Time | Per-request `timeout` and an overall `deadline` across redirects | `timeout` alone is per read, so a slow drip never ends |
| Proxies | Ignored (`trust_env=False`) | An environment proxy would reach the inside on our behalf |
| Errors | `UrlRefused` with a generic message (the reason without the resolved address) | The message reaches clients; it shouldn't map the internal network |

Not covered: protocols other than http(s), and calls to operator-configured services (those
need timeouts, not an SSRF policy; `timeout=` is required by the `missing-timeout` rule).

## 3. `argus_cortex.auth`: one token check that fails closed

```python
bearer(env_var="ARGUS_ADMIN_TOKEN", *, open_when_unset=False)  # a FastAPI/Starlette dependency
check_bearer(header, token) -> bool                           # the constant-time core
```

| Question | Decision | Why |
|---|---|---|
| Token not configured | **Refuse** (503 "admin token not configured") unless `open_when_unset=True` | The `fail_open` class: today most admin routes are open when the variable is unset, and nothing says so |
| Migrating a route that's open today | The codemod passes `open_when_unset=True` explicitly | Behaviour doesn't change silently; the open default becomes visible in code and can be removed per repo |
| Comparison | `hmac.compare_digest` on UTF-8 bytes | `==` leaks through timing; `compare_digest` on `str` raises on non-ASCII |
| Header format | `Authorization: Bearer <token>`, scheme case-insensitive, surrounding whitespace ignored | RFC 6750; comparing the whole header string rejects valid clients |
| Wrong or missing token | 401 with `WWW-Authenticate: Bearer` | What clients expect for bearer auth |
| Shape | A per-route dependency, not middleware | Admin routes are a handful per server; read routes stay open |

## 4. `argus_cortex.limits`: client input can't exhaust memory

```python
await read_body(request, max_bytes) -> bytes
await read_upload(upload, max_bytes) -> bytes
decode_base64(data, max_bytes) -> bytes
open_image(data, *, max_pixels=40_000_000, max_bytes=None, formats=None) -> PIL.Image.Image
TooLarge(ValueError), BadInput(ValueError)
```

| Question | Decision | Why |
|---|---|---|
| `Content-Length` | Over the cap: refused before reading. Absent (chunked): allowed, but counted while streaming | Rejecting chunked uploads breaks real clients; counting is the real guard |
| Malformed `Content-Length` | Refused | It means a broken or hostile client |
| Multipart uploads | `read_upload` reads in chunks and stops at the cap | `UploadFile.read()` reads everything; no server caps it today |
| Base64 | The encoded length is checked **before** decoding | Decoding first allocates the whole thing |
| Images | The pixel count is checked from the header before `load()`; formats can be restricted | Pillow's global default is a warning threshold, not a limit |
| Status | `TooLarge` → 413, `BadInput` (bad length, invalid base64, not an image) → 400 at the edge | 413 is the standard code, and clients can tell it from a validation error |

## Adoption

Each helper ships with a libcst codemod that rewrites the common local shapes to the helper.
The codemods run as packs per repo (the ai-code-auditor flow), and each migrated repo deletes
its local copy. A helper counts as adopted once two repos use it with the matching argus-ci suite
rule active.
