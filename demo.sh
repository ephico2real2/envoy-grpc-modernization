#!/usr/bin/env bash
# Deploy the stack and prove a REST client reaches a gRPC-only service.
#
#   ./demo.sh deploy     namespace, ConfigMaps, all four Deployments, the Route
#   ./demo.sh test       exercise every endpoint through Envoy
#   ./demo.sh metrics    Envoy request counters from the admin endpoint
#   ./demo.sh load       drive load through the whole stack (loadgen/load.py)
#   ./demo.sh url        print the kiosk URL
#   ./demo.sh clean      delete the namespace
set -uo pipefail
cd "$(dirname "$0")"
: "${NS:=modernize-demo}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.crc/machines/crc/kubeconfig}"

# The database credential is generated on first deploy and then left alone, so
# nothing secret is ever committed and re-running deploy does not rotate it out
# from under a running database.
db_secret() {
  if oc get secret inventory-db -n "$NS" >/dev/null 2>&1; then
    echo "secret/inventory-db already exists - keeping it"
    return
  fi
  local pw; pw=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
  oc create secret generic inventory-db -n "$NS" \
    --from-literal=username=inventory \
    --from-literal=password="$pw" \
    --from-literal=connection-string="mongodb://inventory:$pw@inventory-db:27017/inventory?authSource=admin"
}

deploy() {
  oc apply -f manifests/00-namespace.yaml
  # source and the binary proto descriptor become ConfigMaps
  oc create configmap inventory-src -n "$NS" \
    --from-file=server.py=app/server.py \
    --from-file=inventory_pb2.py=app/inventory_pb2.py \
    --from-file=inventory_pb2_grpc.py=app/inventory_pb2_grpc.py \
    --dry-run=client -o yaml | oc apply -f -
  oc create configmap kiosk-src -n "$NS" \
    --from-file=index.html=kiosk/index.html --dry-run=client -o yaml | oc apply -f -
  oc create configmap envoy-proto -n "$NS" \
    --from-file=inventory.pb=proto/inventory.pb --dry-run=client -o yaml | oc apply -f -
  db_secret
  # spec.clusterIP is immutable, so a Service that already exists as ClusterIP
  # cannot be converted to headless in place - apply fails with
  #   spec.clusterIPs[0]: Invalid value: ["None"]: may not change once set
  # Recreate it instead. Deleting a Service does not disturb the pods.
  if [ "$(oc get svc inventory -n "$NS" -o jsonpath='{.spec.clusterIP}' 2>/dev/null)" != "None" ] \
     && oc get svc inventory -n "$NS" >/dev/null 2>&1; then
    echo "svc/inventory is not headless - recreating it"
    oc delete svc inventory -n "$NS"
  fi
  oc apply -f manifests/05-database.yaml
  oc rollout status deploy/inventory-db -n "$NS" --timeout=300s
  oc apply -f manifests/20-envoy-config.yaml -f manifests/10-inventory.yaml \
           -f manifests/30-envoy.yaml -f manifests/40-kiosk.yaml
  # ServiceMonitor needs user-workload monitoring; skip quietly when the CRD is absent
  if oc get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
    oc apply -f manifests/50-metrics.yaml -f manifests/60-alerts.yaml
  else
    echo "no ServiceMonitor CRD - skipping manifests/50-metrics.yaml and 60-alerts.yaml"
  fi
  # All three consume their content from ConfigMaps, and `oc apply` on a
  # Deployment does not restart pods when only a ConfigMap changed. Envoy in
  # particular reads the proto descriptor once at boot, so without this a
  # regenerated inventory.pb is on disk but not in the running proxy - new RPCs
  # 503 and new fields are silently dropped from the JSON.
  oc rollout restart deploy/inventory deploy/envoy deploy/kiosk -n "$NS"
  oc rollout status deploy/inventory -n "$NS" --timeout=300s
  oc rollout status deploy/envoy     -n "$NS" --timeout=180s
  oc rollout status deploy/kiosk     -n "$NS" --timeout=180s
  url
}

url() { echo "https://$(oc get route kiosk -n "$NS" -o jsonpath='{.spec.host}')"; }

test_all() {
  H=$(oc get route kiosk -n "$NS" -o jsonpath='{.spec.host}')
  echo "== GET /v1/items =="
  curl -sk "https://$H/v1/items" | head -12
  echo; echo "== GET /v1/items/SKU-1003 (path parameter) =="
  curl -sk "https://$H/v1/items/SKU-1003"
  echo; echo "== GET /v1/items?warehouse=DERBY (query parameter) =="
  curl -sk "https://$H/v1/items?warehouse=DERBY" | head -8
  echo; echo "== POST /v1/items/SKU-1002:reserve (JSON body) =="
  curl -sk -X POST "https://$H/v1/items/SKU-1002:reserve" \
    -H 'Content-Type: application/json' -d '{"quantity":3,"orderId":"DEMO-1"}'
  echo; echo "== POST /v1/items (create - writes to MongoDB) =="
  SKU="SKU-T$(date +%s | tail -c 5)"
  curl -sk -X POST "https://$H/v1/items" -H 'Content-Type: application/json' \
    -d "{\"sku\":\"$SKU\",\"name\":\"demo.sh test item\",\"onHand\":9,\"warehouse\":\"DERBY\"}"
  echo; echo "== the new item is readable back =="
  curl -sk "https://$H/v1/items/$SKU"
  echo; echo "== creating it twice is ALREADY_EXISTS -> HTTP 409 =="
  curl -sk -o /dev/null -w "HTTP %{http_code}\n" -X POST "https://$H/v1/items" \
    -H 'Content-Type: application/json' -d "{\"sku\":\"$SKU\",\"name\":\"dup\",\"onHand\":1}"
  echo "== DELETE /v1/items/$SKU =="
  curl -sk -X DELETE "https://$H/v1/items/$SKU"
  echo; echo "== gRPC NOT_FOUND becomes HTTP 404 =="
  curl -sk -o /dev/null -w "HTTP %{http_code}\n" "https://$H/v1/items/NOPE"
  echo; echo "== Envoy spreads calls over the inventory replicas =="
  # GetItem returns a single Item, so this is exactly one servedBy per call -
  # ListItems would emit one per item as well and inflate the counts.
  for i in $(seq 1 12); do curl -sk "https://$H/v1/items/SKU-1001"; done \
    | grep -o '"servedBy": "[^"]*"' | sort | uniq -c
  echo "== what the backend saw - gRPC methods only =="
  oc logs -n "$NS" -l app=inventory --tail=8 --prefix \
    | grep -Eo '(GetItem|ListItems|ReserveStock|ResetStock|CreateItem|DeleteItem)' | sort | uniq -c
}

load() {
  H=$(oc get route kiosk -n "$NS" -o jsonpath='{.spec.host}')
  mkdir -p results
  python3 loadgen/load.py --url "https://$H" \
    --duration "${DURATION:-60}" --concurrency "${CONCURRENCY:-16}" \
    --json "results/load-$(date +%Y%m%d-%H%M%S).json"
}

metrics() {
  # The Envoy image carries no shell tooling - no curl, no wget - so the scrape
  # goes through the kiosk pod, which has python3. Fetching via the envoy-stats
  # Service rather than a pod IP also proves that Service resolves, which is
  # what the ServiceMonitor in manifests/50-metrics.yaml relies on.
  KP=$(oc get pod -n "$NS" -l app=kiosk -o jsonpath='{.items[0].metadata.name}')
  echo "== Envoy counters, scraped through svc/envoy-stats =="
  oc exec -n "$NS" "$KP" -- python3 -c "
import urllib.request
body = urllib.request.urlopen('http://envoy-stats:9901/stats/prometheus', timeout=10).read().decode()
keep = ('envoy_cluster_upstream_rq_total', 'envoy_cluster_upstream_cx_active',
        'envoy_cluster_membership_healthy')
for line in body.splitlines():
    if line.startswith(keep) and ('inventory' in line or 'kiosk' in line):
        print('  ' + line)
"
  echo
  echo "== is Prometheus scraping it? =="
  oc get servicemonitor envoy -n "$NS" >/dev/null 2>&1 \
    && echo "  servicemonitor/envoy exists - targets appear under Observe > Targets" \
    || echo "  no ServiceMonitor (user-workload monitoring not enabled)"
}

case "${1:-deploy}" in
  deploy) deploy ;;
  test)   test_all ;;
  metrics) metrics ;;
  load)   load ;;
  url)    url ;;
  clean)  oc delete namespace "$NS" ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
