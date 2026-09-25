#!/usr/bin/env python3
"""Drive load at the kiosk's REST surface and report what came back.

    python3 loadgen/load.py --url https://host --duration 60 --concurrency 16

Every request goes through the whole stack: Route -> Envoy -> transcoder ->
gRPC -> MongoDB. Latency here is end to end, which is the number worth quoting;
Envoy's own histogram and the service's /metrics split it up.

Reports the percentiles from the raw samples rather than from a bucketed
histogram, so p99 is exact for the run rather than interpolated.
"""
import argparse, json, random, ssl, statistics, sys, threading, time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

SKUS = ["SKU-1001", "SKU-1002", "SKU-1003", "SKU-1004", "SKU-1005",
        "SKU-2001", "SKU-2002", "SKU-3001", "SKU-3002",
        "SKU-4001", "SKU-4002", "SKU-5001", "SKU-5002"]
WAREHOUSES = ["LEEDS", "DERBY", "LOS ANGELES", "CHICAGO", "NEW YORK", "HOUSTON"]

# The read mix a kiosk actually produces. Read-only on purpose: the run should
# be repeatable and leave the catalogue exactly as it found it.
#
# Note quote() rather than plain concatenation - "LOS ANGELES" must arrive as
# %20. The transcoder does NOT read "+" as a space (RFC 3986), so a "+" here
# would match nothing and quietly skew the results.
def pick(rng):
    r = rng.random()
    if r < 0.35:
        return "GET", "/v1/items", None
    if r < 0.65:
        return "GET", "/v1/items/" + rng.choice(SKUS), None
    if r < 0.80:
        return "GET", "/v1/items?warehouse=" + urllib.parse.quote(rng.choice(WAREHOUSES)), None
    if r < 0.92:
        return "GET", "/v1/warehouses", None
    return "GET", "/v1/warehouses/" + urllib.parse.quote(rng.choice(WAREHOUSES)), None



class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = []
        self.codes = Counter()
        self.served_by = Counter()
        self.errors = 0

    def add(self, ms, code, pod):
        with self.lock:
            self.samples.append(ms)
            self.codes[code] += 1
            if pod:
                self.served_by[pod] += 1

    def fail(self, why):
        with self.lock:
            self.errors += 1
            self.codes[why] += 1


def worker(stop_at, url, stats, seed, ctx):
    rng = random.Random(seed)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    while time.time() < stop_at:
        method, path, body = pick(rng)
        req = urllib.request.Request(url + path, method=method)
        req.add_header("Accept", "application/json")
        t0 = time.perf_counter()
        try:
            with opener.open(req, timeout=20) as res:
                payload = res.read()
                ms = (time.perf_counter() - t0) * 1000
                pod = None
                try:
                    pod = json.loads(payload).get("servedBy")
                except Exception:
                    pass
                stats.add(ms, res.status, pod)
        except urllib.error.HTTPError as e:
            stats.add((time.perf_counter() - t0) * 1000, e.code, None)
        except Exception as e:
            stats.fail(type(e).__name__)


def pct(xs, p):
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="kiosk base URL, no trailing slash")
    ap.add_argument("--duration", type=int, default=60, help="seconds")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--insecure", action="store_true", default=True,
                    help="CRC serves a self-signed certificate")
    ap.add_argument("--json", metavar="FILE", help="also write the summary as JSON")
    a = ap.parse_args()

    ctx = ssl.create_default_context()
    if a.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    stats = Stats()
    stop_at = time.time() + a.duration
    started = time.time()
    threads = [threading.Thread(target=worker, args=(stop_at, a.url.rstrip("/"), stats, i, ctx),
                                daemon=True) for i in range(a.concurrency)]
    print("running %d workers for %ds against %s" % (a.concurrency, a.duration, a.url))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started

    xs = sorted(stats.samples)
    n = len(xs)
    summary = {
        "requests": n,
        "seconds": round(elapsed, 2),
        "rps": round(n / elapsed, 1) if elapsed else 0,
        "errors": stats.errors,
        "codes": dict(stats.codes),
        "latency_ms": {
            "min": round(xs[0], 1) if n else None,
            "p50": round(pct(xs, 50), 1) if n else None,
            "p90": round(pct(xs, 90), 1) if n else None,
            "p99": round(pct(xs, 99), 1) if n else None,
            "max": round(xs[-1], 1) if n else None,
            "mean": round(statistics.fmean(xs), 1) if n else None,
        },
        "served_by": dict(stats.served_by),
    }

    print()
    print("  requests      %d in %.1fs  =  %.1f req/s" % (n, elapsed, summary["rps"]))
    print("  status        %s" % ", ".join("%s=%d" % kv for kv in sorted(stats.codes.items(), key=str)))
    L = summary["latency_ms"]
    print("  latency ms    min %s  p50 %s  p90 %s  p99 %s  max %s" %
          (L["min"], L["p50"], L["p90"], L["p99"], L["max"]))
    if stats.served_by:
        print("  served by")
        for pod, c in sorted(stats.served_by.items()):
            print("      %-34s %6d  (%4.1f%%)" % (pod, c, 100.0 * c / n))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(summary, fh, indent=2)
        print("  wrote %s" % a.json)
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
