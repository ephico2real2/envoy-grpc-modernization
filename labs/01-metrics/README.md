# Lab 01 — metrics, and what the numbers actually say

Both tiers publish Prometheus metrics, OpenShift scrapes them, and a load runner
produces numbers you can check against each other.

## What gets exposed

| Source | Endpoint | Exposed by |
|---|---|---|
| Envoy | `:9901/stats/prometheus` | `svc/envoy-stats` + `servicemonitor/envoy` |
| the service | `:9100/metrics` | `svc/inventory` (port `metrics`) + `servicemonitor/inventory` |

The application metrics come from one gRPC **interceptor**, not from per-method
instrumentation, so a new RPC is measured the moment it is added:

```python
RPC_TOTAL   = Counter("inventory_rpc_total", "gRPC calls handled", ["method", "code"])
RPC_SECONDS = Histogram("inventory_rpc_duration_seconds", "gRPC handler latency", ["method"])
INFLIGHT    = Gauge("inventory_rpc_inflight", "calls currently being handled")
ITEMS_TOTAL = Gauge("inventory_items_total", "documents in the catalogue")
```

`pod` is deliberately *not* a label — Prometheus attaches one per target already,
and duplicating it multiplies the series count for nothing.

## The trap in the interceptor

`context.abort()` raises a **bare `Exception`**, not a `grpc.RpcError`. Verified
on grpc-python before shipping:

```console
  caught_as        bare Exception
  code_in_finally  NOT_FOUND
  client_saw       NOT_FOUND
```

So the status is read from the context in a `finally` block. Catching
`grpc.RpcError` would label every `NOT_FOUND` and `ALREADY_EXISTS` as
`INTERNAL`, and the dashboard would report failures that never happened.
Measured after the fix — 5 lookups of a missing sku and 2 duplicate creates:

```text
inventory_rpc_total{code="NOT_FOUND",method="GetItem"}        3
inventory_rpc_total{code="NOT_FOUND",method="GetItem"}        1
inventory_rpc_total{code="NOT_FOUND",method="GetItem"}        1
inventory_rpc_total{code="ALREADY_EXISTS",method="CreateItem"} 2
```

## Run it

```bash
oc apply -f manifests/50-metrics.yaml
./demo.sh metrics                                   # counters straight off the admin port
python3 loadgen/load.py --url "https://$(oc get route kiosk -n modernize-demo \
  -o jsonpath='{.spec.host}')" --duration 60 --concurrency 16
```

User-workload monitoring must be on:

```bash
oc -n openshift-monitoring get cm cluster-monitoring-config \
  -o jsonpath='{.data.config\.yaml}'     # enableUserWorkload: true
```

## Measured — 60s, 16 workers, 3 backend replicas

```text
requests      73989 in 60.0s  =  1233.0 req/s
status        200=73989
latency ms    min 2.6  p50 8.9  p90 27.8  p99 59.0  max 116.2
served by
    inventory-8467457d96-4zhx2          24662  (33.3%)
    inventory-8467457d96-8b24l          24667  (33.3%)
    inventory-8467457d96-9f4pr          24660  (33.3%)
```

Three things worth noticing.

**The split is 33.3 / 33.3 / 33.3.** That is the headless Service plus Envoy's
`ROUND_ROBIN`, measured rather than assumed.

**The counters reconcile.** The service's own totals were 24662 + 24667 + 24660
= 73,989 — exactly what the load runner counted. If those two disagree, the
instrumentation is wrong, and this is the check that says so.

**Server time is not round-trip time.** The histogram put p99 handler latency at
**17.6 ms** while the client measured **59 ms** end to end. The difference is
queueing, TLS, the Route, and Envoy — which is the argument for measuring at
both ends rather than quoting one number.

## Checking it landed in Prometheus

```bash
TOKEN=$(oc create token metrics-reader -n modernize-demo --duration=60m)
THANOS=$(oc get route thanos-querier -n openshift-monitoring -o jsonpath='{.spec.host}')
curl -sk -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'query=sum(rate(inventory_rpc_total[5m]))' \
  "https://$THANOS/api/v1/query"
```

`oc whoami -t` returns nothing on CRC — the kubeconfig authenticates with client
certificates, not a bearer token, so a ServiceAccount token has to be minted.

![all five scrape targets up](../../docs/lab01/prometheus-targets.jpg)

Two Envoy targets and three inventory targets, all `Up`.

![request rate per pod](../../docs/lab01/metrics-query.jpg)

`sum by (pod) (rate(inventory_rpc_total[2m]))` — three pods, evenly matched.

## Note on `demo.sh metrics`

The Envoy image ships no shell tooling — no `curl`, no `wget` — so the scrape
runs from the kiosk pod, which has `python3`. Going through `svc/envoy-stats`
rather than a pod IP also proves the Service the ServiceMonitor depends on
resolves.

Counters are **per Envoy pod** and Prometheus scrapes each separately: 40 calls
appeared as 18 and 22, summing to 40.
