# Reading and changing `inventory.proto`

Written for someone who has not used Protocol Buffers before. By the end you
should be able to read every line of `inventory.proto`, add an RPC, and
understand why `inventory.pb` exists at all.

## What a `.proto` file is

A **contract**. It says what messages exist, what fields they have, and what
methods a service offers — in one language-neutral file. From it, a compiler
generates code for Python, Go, Java, and so on.

Two consequences worth understanding straight away:

- **The contract is the source of truth.** `app/inventory_pb2.py` is *generated*.
  Never edit it; edit the `.proto` and regenerate.
- **The wire format is binary.** Field *numbers*, not names, go over the wire.
  That is why protobuf is compact, and why the numbers matter enormously.

## The file, top to bottom

### 1. Syntax

```protobuf
syntax = "proto3";
```

Which version of the language. `proto3` is the modern one. It has one behaviour
that surprises people constantly, covered under **field presence** below.

### 2. Package

```protobuf
package legacy.inventory.v1;
```

A namespace. It prevents a `Item` here colliding with an `Item` somewhere else,
and it becomes part of every method's full name on the wire:

```text
/legacy.inventory.v1.Inventory/GetItem
 └────── package ──────┘└service┘ └method┘
```

That string is the actual HTTP/2 path a gRPC call uses — which is why Envoy's
route table matches `/legacy.inventory.v1.Inventory/` and not `/v1/`.

The `.v1` is a convention: breaking changes go in a new package (`v2`) so old
clients keep working.

### 3. Imports

```protobuf
import "google/api/annotations.proto";
```

Pulls in definitions from another file — here, Google's HTTP-mapping options.
Without this import you cannot write `option (google.api.http)`.

### 4. The service

```protobuf
service Inventory {
  rpc GetItem (GetItemRequest) returns (Item) {
    option (google.api.http) = { get: "/v1/items/{sku}" };
  }
}
```

`service` is a collection of methods. Each `rpc` takes **exactly one** message
and returns **exactly one** message. That is not a limitation to work around —
it is why you see `GetItemRequest` rather than a bare `string sku`. Wrapping
arguments in a message means you can add a second argument later without
breaking anyone.

The `option (google.api.http)` block is the interesting part — see
**The HTTP annotations** below.

### 5. Messages

```protobuf
message Item {
  string sku       = 1;
  string name      = 2;
  int32  on_hand   = 3;
  int32  reserved  = 4;
  string warehouse = 5;
  string served_by = 6;
}
```

Read each line as `type name = field_number;`.

**The number is not a value and not an order.** It is the field's identity on
the wire. Rules that follow from that:

| Rule | Why |
|---|---|
| Never change a number on an existing field | Old data decodes into the wrong field. Silent corruption |
| Never reuse a number after deleting a field | Same problem, delayed |
| Adding a new field with a new number is safe | Old readers skip what they do not recognise |
| Numbers 1–15 take one byte; 16+ take two | Give the low numbers to the fields sent most often |

If you delete a field, tell the compiler so nobody reuses it:

```protobuf
reserved 4;
reserved "reserved_count";
```

### Field presence — the proto3 gotcha

In proto3, a scalar field set to its zero value (`0`, `""`, `false`) is
**indistinguishable from a field that was never set**. Both are simply absent
from the wire.

This bites in this repo, twice, and both are worth knowing:

1. **`ListItemsResponse.total`** and friends are printed even when zero only
   because Envoy is configured with `always_print_primitive_fields: true`.
   Without it, `"reserved": 0` vanishes from the JSON.

2. **`UpdateItem`** cannot tell "do not change `on_hand`" from "set `on_hand`
   to 0". That is exactly why it carries an `update_mask` — the caller names the
   fields they mean, so zero becomes expressible.

If you genuinely need optional-vs-zero on a scalar, mark it `optional`:

```protobuf
optional int32 on_hand = 3;   // now has explicit presence
```

### Common types

| In `.proto` | Python | Notes |
|---|---|---|
| `string` | `str` | always UTF-8 |
| `int32` / `int64` | `int` | `int64` becomes a **string** in JSON — beyond `Number.MAX_SAFE_INTEGER` |
| `bool` | `bool` | |
| `repeated Item items` | list | a list; never null, just empty |
| `Item item` | message | nested message |

## The HTTP annotations

This is what makes the REST surface exist. The service never reads these —
**Envoy** does.

```protobuf
rpc GetItem (GetItemRequest) returns (Item) {
  option (google.api.http) = { get: "/v1/items/{sku}" };
}
```

### Path variables

`{sku}` binds that path segment to the request's `sku` field.
`GET /v1/items/SKU-1001` → `GetItemRequest{sku: "SKU-1001"}`.

### Where each field comes from

Three sources, and getting them confused is the most common mistake:

| Source | When | Example |
|---|---|---|
| **Path** | named in `{}` | `/v1/items/{sku}` → `sku` |
| **Body** | what `body:` names | `body: "*"` or `body: "item"` |
| **Query string** | everything left over | `?warehouse=DERBY&pageSize=5` |

`body` has two forms, and the difference is load-bearing:

```protobuf
body: "*"        // the whole JSON body maps onto the request message
body: "item"     // the body maps onto the `item` field ONLY
```

With `body: "item"`, the JSON on the wire is the bare item object rather than
`{"item": {...}}` — nicer for callers. **But** any request field that is *not*
`item` can then only arrive as a path or query parameter.

That is precisely the trap in `UpdateItem`:

```protobuf
rpc UpdateItem (UpdateItemRequest) returns (Item) {
  option (google.api.http) = { patch: "/v1/items/{sku}" body: "item" };
}
message UpdateItemRequest {
  string sku         = 1;   // from the PATH
  Item   item        = 2;   // from the BODY
  string update_mask = 3;   // from the QUERY STRING - nowhere else
}
```

```bash
# works - mask in the query string
curl -X PATCH "$H/v1/items/SKU-1005?updateMask=on_hand" -d '{"onHand":0}'

# silently does nothing - mask in the body is not bound to update_mask,
# and onHand:0 is indistinguishable from unset
curl -X PATCH "$H/v1/items/SKU-1005" -d '{"onHand":0,"updateMask":"on_hand"}'
```

### Custom verbs

```protobuf
post: "/v1/items/{sku}:reserve"
post: "/v1/items:reset"
post: "/v1/items"              // CreateItem
```

Three POSTs coexist because the suffix after the `:` distinguishes them. A colon
verb is the Google API convention for an action that is not plain CRUD.

### Field naming across the boundary

The proto says `on_hand`. The JSON says `onHand`. Nobody configured that — the
JSON mapping specifies **lowerCamelCase** by default. So:

| Layer | Spelling |
|---|---|
| `.proto` and the database | `on_hand` |
| JSON on the wire | `onHand` |
| Generated Python | `on_hand` |

## `inventory.pb` — the descriptor set

Envoy cannot read `.proto` files. It reads a **compiled descriptor set**: the
same information, serialised as protobuf itself.

```bash
python -m grpc_tools.protoc -Iproto \
  --include_imports --include_source_info \
  --descriptor_set_out=proto/inventory.pb \
  --python_out=app --grpc_python_out=app \
  proto/inventory.proto
```

| Flag | What it does |
|---|---|
| `-Iproto` | where to look for imports |
| `--descriptor_set_out` | the binary Envoy reads |
| `--include_imports` | **not optional** — bundle imported files too |
| `--include_source_info` | keeps comments; useful for generated docs |
| `--python_out` | `inventory_pb2.py`, the message classes |
| `--grpc_python_out` | `inventory_pb2_grpc.py`, the client/server stubs |

**Why `--include_imports` is mandatory here:** without it the descriptor
contains only `inventory.proto`, and Envoy rejects the config because it cannot
resolve `google.api.http`. With it, the file contains four:

```text
google/api/http.proto
google/protobuf/descriptor.proto
google/api/annotations.proto
inventory.proto
```

Inspect it yourself:

```python
from google.protobuf import descriptor_pb2
fds = descriptor_pb2.FileDescriptorSet()
fds.ParseFromString(open('proto/inventory.pb', 'rb').read())
for f in fds.file:
    print(f.name)
```

The output is byte-for-byte reproducible — regenerating without changing the
`.proto` produces an identical file, so a diff means a real change.

## Changing the contract

1. **Edit `inventory.proto`.** New field → new, never-used number.
2. **Regenerate** with the command above. Commit `inventory.pb` and both
   `inventory_pb2*.py` — they are build outputs, but committing them means the
   pods need no build step.
3. **Update `app/server.py`** to implement any new RPC.
4. **Redeploy all three** — and this is the one people miss:

```bash
./demo.sh deploy     # recreates the ConfigMaps AND restarts the Deployments
```

**Envoy reads the descriptor once, at startup.** Updating the `envoy-proto`
ConfigMap changes the file on disk but not the running proxy, and `oc apply` on
a Deployment does not restart pods when only a ConfigMap changed. The symptoms
are specific and misleading: new RPCs return `upstream connect error ... remote
reset` or 503, and new response fields are silently dropped from the JSON.

### Compatibility, in one table

| Change | Safe? |
|---|---|
| Add a field with a fresh number | Yes |
| Add an RPC | Yes |
| Rename a field (same number) | Yes on the wire; **breaks JSON clients** |
| Change a field's type | No |
| Change a field's number | No |
| Delete a field | Only with `reserved` |
| Delete an RPC | Breaks callers of it |

## Hands-on

```bash
# explore the running service without a local .proto - it has reflection on
grpcurl -plaintext inventory:50051 list
grpcurl -plaintext inventory:50051 describe legacy.inventory.v1.Inventory

# read the descriptor's own view of the service
PYTHONPATH=app python -c "
import inventory_pb2 as pb
svc = pb.DESCRIPTOR.services_by_name['Inventory']
for m in svc.methods:
    print('%-16s %s -> %s' % (m.name, m.input_type.name, m.output_type.name))
"
```

## References

- [Protocol Buffers — proto3 language guide](https://protobuf.dev/programming-guides/proto3/)
- [Proto best practices](https://protobuf.dev/best-practices/dos-donts/)
- [ProtoJSON mapping](https://protobuf.dev/programming-guides/json/) — the camelCase rules
- [`google.api.HttpRule`](https://github.com/googleapis/googleapis/blob/master/google/api/http.proto) — every field of the annotation, with examples
- [Google AIP-134 `Update`](https://google.aip.dev/134) — why update masks exist
- [Envoy gRPC-JSON transcoder](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/grpc_json_transcoder_filter)
