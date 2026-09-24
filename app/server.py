"""
A deliberately old-fashioned service: it speaks gRPC and nothing else.

There is no HTTP server here, no JSON, no CORS, no REST router. A browser
cannot talk to it. curl cannot talk to it. That is the point - everything
modern about this service is added by the Envoy in front of it, without a
line of code changing here.

State lives in MongoDB, so the service itself is stateless and can be scaled
horizontally. Every reply carries the pod that served it (Item.served_by,
ListItemsResponse.served_by), which is how the kiosk shows Envoy balancing
across the inventory replicas.

The one concession to the modern world is a Prometheus endpoint on a separate
port. It is not an HTTP API - it serves /metrics and nothing else - so the
claim that this service speaks no HTTP API still holds.

The generated stubs (inventory_pb2*.py) are committed beside this file and are
produced from proto/inventory.proto - regenerate them with the command in the
README whenever the proto changes.
"""
import os, time, logging
from concurrent import futures

import grpc
from grpc_reflection.v1alpha import reflection
from prometheus_client import Counter, Histogram, Gauge, start_http_server
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

import inventory_pb2 as pb
import inventory_pb2_grpc as rpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("inventory")

POD = os.environ.get("POD_NAME", "inventory")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://inventory-db:27017")
DB_NAME = os.environ.get("MONGO_DB", "inventory")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))

# Labelled by method and gRPC status so a dashboard can separate a NOT_FOUND
# from a real failure. `pod` is deliberately NOT a label: Prometheus already
# attaches one per target, and duplicating it multiplies the series count.
RPC_TOTAL = Counter("inventory_rpc_total", "gRPC calls handled", ["method", "code"])
RPC_SECONDS = Histogram("inventory_rpc_duration_seconds", "gRPC handler latency", ["method"],
                        buckets=(.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5))
INFLIGHT = Gauge("inventory_rpc_inflight", "calls currently being handled")
ITEMS_TOTAL = Gauge("inventory_items_total", "documents in the catalogue")

# The catalogue the collection is seeded with on first start. After that the
# database is the source of truth and the kiosk can add to it.
SEED = [
    dict(sku="SKU-1001", name="Hydraulic pump, 12L",     on_hand=42,  reserved=0, warehouse="LEEDS"),
    dict(sku="SKU-1002", name="Bearing assembly 60mm",   on_hand=118, reserved=0, warehouse="LEEDS"),
    dict(sku="SKU-1003", name="Control board rev C",     on_hand=7,   reserved=0, warehouse="DERBY"),
    dict(sku="SKU-1004", name="Seal kit, nitrile",       on_hand=260, reserved=0, warehouse="DERBY"),
    dict(sku="SKU-1005", name="Drive belt 1400mm",       on_hand=0,   reserved=0, warehouse="LEEDS"),
    dict(sku="SKU-2001", name="Coupling, 40mm steel",    on_hand=96,  reserved=0, warehouse="LOS ANGELES"),
    dict(sku="SKU-2002", name="Gasket set, viton",       on_hand=310, reserved=0, warehouse="LOS ANGELES"),
    dict(sku="SKU-3001", name="Servo motor 750W",        on_hand=14,  reserved=0, warehouse="CHICAGO"),
    dict(sku="SKU-3002", name="Roller chain, 3m",        on_hand=58,  reserved=0, warehouse="CHICAGO"),
    dict(sku="SKU-4001", name="Pressure sensor 0-10bar", on_hand=5,   reserved=0, warehouse="NEW YORK"),
    dict(sku="SKU-4002", name="Contactor 40A",           on_hand=77,  reserved=0, warehouse="NEW YORK"),
    dict(sku="SKU-5001", name="Hose, braided 2m",        on_hand=141, reserved=0, warehouse="HOUSTON"),
    dict(sku="SKU-5002", name="Filter element 10um",     on_hand=0,   reserved=0, warehouse="HOUSTON"),
]

FIELDS = ("sku", "name", "on_hand", "reserved", "warehouse")


def connect():
    """Block until MongoDB answers. Every replica runs this, so it doubles as
    the readiness gate - the gRPC port only opens once the database is up."""
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    for attempt in range(1, 61):
        try:
            client.admin.command("ping")
            log.info("connected to mongodb (attempt %d)", attempt)
            break
        except PyMongoError as exc:
            log.info("waiting for mongodb: %s", exc.__class__.__name__)
            time.sleep(2)
    else:
        raise SystemExit("mongodb unreachable after 120s")

    items = client[DB_NAME].items
    items.create_index("sku", unique=True)
    # Seeded with upserts, so all replicas can race through this harmlessly and
    # a restart never clobbers items added through the kiosk.
    for doc in SEED:
        items.update_one({"sku": doc["sku"]}, {"$setOnInsert": doc}, upsert=True)
    log.info("catalogue holds %d item(s)", items.count_documents({}))
    return items


class Metrics(grpc.ServerInterceptor):
    """One interceptor rather than per-method instrumentation, so a new RPC is
    measured the moment it is added and cannot be forgotten."""

    def intercept_service(self, continuation, handler_call_details):
        method = handler_call_details.method.rsplit("/", 1)[-1]
        handler = continuation(handler_call_details)
        if handler is None or not handler.unary_unary:
            return handler

        def wrapper(request, context):
            INFLIGHT.inc()
            started = time.perf_counter()
            try:
                return handler.unary_unary(request, context)
            finally:
                # The status is read from the context, not from an exception
                # type. context.abort() raises a BARE Exception - not a
                # grpc.RpcError - so catching RpcError would label every
                # NOT_FOUND as INTERNAL. context.code() is set by then, and is
                # None for a call that succeeded.
                INFLIGHT.dec()
                code = context.code()
                RPC_SECONDS.labels(method).observe(time.perf_counter() - started)
                RPC_TOTAL.labels(method, code.name if code else "OK").inc()

        return grpc.unary_unary_rpc_method_handler(
            wrapper,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer)


def to_item(doc):
    return pb.Item(served_by=POD, **{k: doc.get(k) for k in FIELDS})


class Inventory(rpc.InventoryServicer):
    def __init__(self, items):
        self.items = items

    def GetItem(self, request, context):
        log.info("GetItem sku=%s", request.sku)
        doc = self.items.find_one({"sku": request.sku})
        if not doc:
            context.abort(grpc.StatusCode.NOT_FOUND, "no such sku: %s" % request.sku)
        return to_item(doc)

    def ListItems(self, request, context):
        log.info("ListItems warehouse=%r page_size=%d", request.warehouse, request.page_size)
        q = {"warehouse": request.warehouse} if request.warehouse else {}
        total = self.items.count_documents(q)
        cur = self.items.find(q).sort("sku", 1)
        if request.page_size:
            cur = cur.limit(request.page_size)
        return pb.ListItemsResponse(items=[to_item(d) for d in cur],
                                    total=total, served_by=POD)

    def ReserveStock(self, request, context):
        log.info("ReserveStock sku=%s qty=%d order=%s",
                 request.sku, request.quantity, request.order_id)
        if request.quantity <= 0:
            doc = self.items.find_one({"sku": request.sku})
            if not doc:
                context.abort(grpc.StatusCode.NOT_FOUND, "no such sku: %s" % request.sku)
            return pb.ReserveStockResponse(sku=request.sku, reserved=doc["reserved"],
                                           ok=False, message="quantity must be positive",
                                           served_by=POD)
        # One conditional update, so two replicas cannot oversell the same item:
        # the $expr filter and the $inc are applied atomically by the server.
        doc = self.items.find_one_and_update(
            {"sku": request.sku,
             "$expr": {"$gte": [{"$subtract": ["$on_hand", "$reserved"]}, request.quantity]}},
            {"$inc": {"reserved": request.quantity}},
            return_document=ReturnDocument.AFTER)
        if doc:
            return pb.ReserveStockResponse(
                sku=request.sku, reserved=doc["reserved"], ok=True, served_by=POD,
                message="reserved %d for %s" % (request.quantity, request.order_id or "-"))

        # The update matched nothing: either the sku is gone or there is not
        # enough free stock. Read it back to say which.
        doc = self.items.find_one({"sku": request.sku})
        if not doc:
            context.abort(grpc.StatusCode.NOT_FOUND, "no such sku: %s" % request.sku)
        free = doc["on_hand"] - doc["reserved"]
        return pb.ReserveStockResponse(
            sku=request.sku, reserved=doc["reserved"], ok=False, served_by=POD,
            message="only %d free of %d on hand" % (free, doc["on_hand"]))

    def ResetStock(self, request, context):
        log.info("ResetStock")
        res = self.items.update_many({"reserved": {"$gt": 0}}, {"$set": {"reserved": 0}})
        return pb.ResetStockResponse(
            items_reset=res.modified_count, served_by=POD,
            message="cleared reservations on %d item(s)" % res.modified_count)

    def CreateItem(self, request, context):
        it = request.item
        log.info("CreateItem sku=%s", it.sku)
        if not it.sku:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "sku is required")
        if not it.name:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "name is required")
        if it.on_hand < 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "onHand cannot be negative")
        doc = dict(sku=it.sku, name=it.name, on_hand=it.on_hand,
                   reserved=0, warehouse=it.warehouse or "LEEDS")
        try:
            self.items.insert_one(dict(doc))
        except DuplicateKeyError:
            # ALREADY_EXISTS is what the transcoder turns into HTTP 409.
            context.abort(grpc.StatusCode.ALREADY_EXISTS, "sku already exists: %s" % it.sku)
        return to_item(doc)

    def DeleteItem(self, request, context):
        log.info("DeleteItem sku=%s", request.sku)
        res = self.items.delete_one({"sku": request.sku})
        if not res.deleted_count:
            context.abort(grpc.StatusCode.NOT_FOUND, "no such sku: %s" % request.sku)
        return pb.DeleteItemResponse(sku=request.sku, deleted=True, served_by=POD,
                                     message="deleted %s" % request.sku)


def serve():
    items = connect()
    port = os.environ.get("PORT", "50051")
    # /metrics on its own port, before the gRPC port opens, so a scrape during
    # a slow rollout gets a real answer rather than a connection refused.
    start_http_server(METRICS_PORT)
    log.info("prometheus metrics on :%d/metrics", METRICS_PORT)
    ITEMS_TOTAL.set_function(lambda: items.count_documents({}))

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                         interceptors=(Metrics(),))
    rpc.add_InventoryServicer_to_server(Inventory(items), server)
    # Reflection lets grpcurl explore the service without a local .proto copy.
    reflection.enable_server_reflection(
        (pb.DESCRIPTOR.services_by_name["Inventory"].full_name, reflection.SERVICE_NAME), server)
    server.add_insecure_port("0.0.0.0:%s" % port)
    server.start()
    log.info("gRPC only, listening on :%s as %s - no HTTP, no JSON, no CORS", port, POD)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
