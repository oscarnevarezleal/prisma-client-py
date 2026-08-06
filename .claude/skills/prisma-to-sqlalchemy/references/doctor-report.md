# `prisma py sqlalchemy doctor --json`, key by key

Produced by `prisma.sa._doctor.to_json`. That function's docstring calls this an
interface: **keys are added, never renamed**, and `doctor.schema_version` says
which shape you have. Current shape is `1`.

Nothing here touches a database or the network. It parses Python source with
`ast` and reads the `.prisma` files as text.

```bash
python -m prisma py sqlalchemy doctor --json > /tmp/doctor.json
python -m prisma py sqlalchemy doctor --json --path src --path jobs --schema prisma/
python -m prisma py sqlalchemy doctor            # human-readable, truncated at 40 per section
```

`--path` is repeatable and defaults to the working directory. `--schema` takes a
file *or a directory* and is discovered from `./schema.prisma`,
`./prisma/schema.prisma`, `./prisma/` when omitted. The text report truncates
long lists; `--json` never does.

Paths in the report are relative to the working directory when the scanned path
is under it, and absolute otherwise. If you pass `--path /elsewhere` you get
absolute paths in `call_sites[].path` and `modules[].key`.

---

## Top level

```
doctor      {schema_version, sa_version, runbook, statuses}
roots       [str]        the --path values, relative to cwd
schema      {...}        see below
summary     {...}        see below
call_sites  [{...}]      every call site found, classified
modules     [{...}]      grouped by file
models      [{...}]      grouped by Prisma model
transactions[{...}]      one entry per tx()/batch_() `with` block
caveats     [{...}]      what the scan could not see
```

`doctor.runbook` is the runbook's path relative to the project source
(`docs/prisma-to-sqlalchemy-runbook.md`). `doctor.sa_version` is
`prisma.sa.__version__`, independent of `prisma.__version__`.

## `schema`

```
available        bool    False → the client has no schemaMetadata payload
detail           str     why, verbatim — quote it rather than paraphrasing
provider         str|null
models, tables, enums, join_tables   int
relation_shapes  {shape: count}      e.g. {"one-to-many": 7, "many-to-many": 2}
blockers         [{code, detail, reference}]
scope_check      {paths, provider, relation_mode, native_types, checks}
```

`available: false` means `generate` and `baseline` will raise
`SchemaNotAvailableError`. `doctor` still works — the scope check is read from
the raw `.prisma` text and does not need the payload.

`schema.blockers` is schema-level, not call-site-level. The one that exists today
is `self-referential-m2m`: which of the join table's two columns a side traverses
is not recoverable from the DMMF, so traversing it would be a coin flip.

### `schema.scope_check.checks`

A list of `{name, status, detail}` where status is `pass` / `fail` / `unknown`.

| name | `fail` means |
| --- | --- |
| `provider is PostgreSQL` | the type table has only been verified against PostgreSQL — migration ends here |
| `relationMode is not "prisma"` | the database has no foreign keys at all — Prisma enforces relations in the query engine and SQLAlchemy will not. Since `prisma.sa` 1.3.0 the emitted DDL matches that database, so this is no longer a diff; it still ends here, because referential integrity stops being enforced |

`unknown` means no `datasource` block was read: the `--schema` path was wrong.
Fix the path rather than proceeding on an unread schema.

`native_types` is a `{annotation: count}` census of `@db.*` usage. These are
**not** a stop — they are lexed from the raw schema text and honoured — but if
one is unknown to `prisma/sa/_types.py`, `sa.metadata()` raises naming it. Record
the census in the report; it says how much of the schema depends on the lexer.

## `summary`

```
call_sites, verified, stop, unknown   int
files_scanned, files_with_call_sites  int
unparsed_files   [{path, error}]
not_analysed     {call_sites, files, codes: {caveat_code: count}}
```

`not_analysed` exists so a summary cannot read as more complete than the scan
was. Report it alongside the headline numbers, always.

## `call_sites[]`

```
path, line, column
entry        "client" | "model" | "transaction"
action       find_many | create | upsert | tx | batch_ | query_raw | ...
receiver     as written, e.g. "db.post" or "User.prisma()"
model        the Prisma model name, resolved through the schema when available
resolution   "binding" | "convention" | "unresolved" | "dynamic"
block        id of the innermost enclosing tx()/batch_() block, or null
arguments    sorted argument names actually passed (positional ones named)
opaque_arguments  ["*"] and/or ["**"]
status       "verified" | "stop" | "unknown"
code         machine-readable classification, see below
reason       prose, quoting the runbook row — use this text with the user
reference    "§4.1 Reads", "§5 Atomic updates", or "none — neither §4.1 nor §5"
notes        [str] caveats that do not change the status
```

`entry` distinguishes the three call syntaxes: `db.post.find_many()` is
`client`, `Post.prisma().find_many()` is `model`, `db.tx()`/`db.batch_()` is
`transaction`.

`resolution` grades the evidence. `binding` means the receiver was traced to a
`Prisma()` construction or a `Prisma` annotation. `convention` means it only
matched a conventional name (`db`, `client`, `prisma`, `self.db`, …) — weaker,
and counted as a caveat. `unresolved` and `dynamic` produce `unknown`.

`model` may be a lowercase client attribute (`"channel"`) rather than a model
name when `schema.available` is `false` — the schema is what resolves
`db.channel` → `Channel`.

`notes` carries things worth saying that are not blockers, e.g. the `include`
reshape note, the `max_wait` → `pool_timeout` default mismatch, and the upsert
precondition.

### Status vocabulary

| status | meaning | what you do |
| --- | --- | --- |
| `verified` | a §4.1 row covers it *in the shape this call uses* | translate |
| `stop` | a §5 row blocks it; `reason` names the row | leave on Prisma, record |
| `unknown` | neither list covers it | ask the user; do not guess |

The method name is not the classification. `update` is §4.1;
`update(data={'n': {'increment': 1}})` is an atomic operation and therefore §5.
Half the STOP list is only reachable by reading the arguments.

### `code` values

STOPs: `aggregate`, `atomic-update`, `relation-connect`, `nested-write`,
`json-filter`, `scalar-list-filter`, `full-text-search`, `transaction-timeout`,
`upsert-include`, `upsert-empty-update`, `upsert-where-mismatch`,
`transaction-blocked`.

Unknowns: `opaque-where`, `opaque-data`, `opaque-arguments`,
`argument-not-covered`, `where-operator-not-covered`, `action-not-covered`,
`raw-sql`, `dynamic-model`, `unresolved-client`.

`transaction-blocked` is applied *after* classification: see `transactions`
below.

## `modules[]` and `models[]`

```
key          the file path, or the model name
call_sites, verified, stop, unknown   int
phase        "migratable-now" | "one-blocker" | "mixed" | "blocked"
```

`phase` is computed, and it is what picks the slice:

| phase | rule | read as |
| --- | --- | --- |
| `migratable-now` | `stop == 0 and unknown == 0` | the first phase |
| `one-blocker` | `stop == 1 and unknown == 0 and verified > 0` | second: translate around the one STOP |
| `blocked` | `stop > verified + unknown` | not a phase — a conversation |
| `mixed` | anything else | third: call site by call site |

Group by `modules` for the work; group by `models` for the conversation about
which parts of the domain are reachable.

## `transactions[]`

```
block    "tx@path/to/file.py:12"   the id used in call_sites[].block
kind     "tx" | "batch_"
path, line
members  how many call sites are in the block — the tx() call itself counts
blocked  bool
```

`blocked: true` means at least one member is a STOP. When that happens the
doctor **rewrites** the other members: every `verified` member in the block
becomes `status: "stop"`, `code: "transaction-blocked"`,
`reference: "§5 A transaction spanning a Prisma client and a SQLAlchemy
connection"`. That is not a heuristic — a transaction spanning both clients is
measured to be two transactions on two connections, and rolling back one leaves
the other committed. So the block migrates whole or stays whole.

A `tx()` opened without a `with` block has `block: null` on its members and shows
up as the `transaction-body-not-visible` caveat: membership could not be
determined, so nothing in it can be certified.

## `caveats[]`

`{code, count, detail}`, sorted by code. The full vocabulary and what each means
is in `detail` — quote it. The ones that most change a plan:

| code | why it matters |
| --- | --- |
| `opaque-where` / `opaque-data` | the payload is built elsewhere; §5 filters and nested writes cannot be ruled out |
| `opaque-arguments` | the call splats `*`/`**`; nothing is visible |
| `convention-client` | matched a name, not a binding — weaker evidence, may not be a client at all |
| `transaction-body-not-visible` | a `tx()` with no `with` block; §4.1 requires blocks to migrate whole |
| `unparsed-file` | not scanned at all; `summary.unparsed_files` names them and why |

The scanner reads **one module at a time**. A client, a `where` or a write
payload arriving from another module is not followed, and a call site reached
only through a helper is attributed to the helper, not to its callers. Say this
when you present the numbers.

---

## Reading it

```python
import json

report = json.load(open('/tmp/doctor.json'))

assert report['doctor']['schema_version'] == 1

for check in report['schema']['scope_check']['checks']:
    if check['status'] == 'fail':
        raise SystemExit(f"§0 scope check failed: {check['name']} — {check['detail']}")

slice_ = [m['key'] for m in report['modules'] if m['phase'] == 'migratable-now']

blocked = {t['block'] for t in report['transactions'] if t['blocked']}
todo = [
    c for c in report['call_sites']
    if c['path'] in slice_ and c['status'] == 'verified' and c['block'] not in blocked
]

questions = [c for c in report['call_sites'] if c['status'] == 'unknown']
stops = [c for c in report['call_sites'] if c['status'] == 'stop']
```

The `blocked` filter above is belt-and-braces: the doctor has already demoted
those members to `stop`. Keep it anyway — it survives a `schema_version` bump
that changes when the demotion happens.
