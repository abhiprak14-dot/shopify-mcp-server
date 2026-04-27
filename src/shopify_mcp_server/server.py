import asyncio
import time
import logging
from datetime import datetime, timezone
import shopify
from pymongo import MongoClient
from functools import lru_cache

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
import mcp.server.stdio
from mcp.server.sse import SseServerTransport

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse

import uvicorn
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("neolook-shopify")

# ─── MongoDB (PRODUCTION FIXED) ───────────────────────────────────────
@lru_cache()
def get_mongo_collection():
    try:
        client = MongoClient(
            os.getenv("MONGODB_URI"),
            maxPoolSize=50,
            minPoolSize=5,
            serverSelectionTimeoutMS=5000
        )
        db = client["neolook"]
        return db["logs"]
    except Exception as e:
        logger.error(f"MongoDB connection error: {e}")
        return None


async def log_to_mongo_async(session_id, ip_address, tool, input_args, output_summary, status, error_message, response_time_ms):
    def _log():
        try:
            collection = get_mongo_collection()
            if collection is None:
                return

            collection.insert_one({
                "datetime": datetime.now(timezone.utc),
                "session_id": session_id,
                "ip_address": ip_address,
                "tool": tool,
                "input": input_args,
                "output_summary": output_summary,
                "status": status,
                "error_message": error_message,
                "response_time_ms": response_time_ms,
            })
        except Exception as e:
            logger.error(f"[MONGO] Failed: {e}")

    await asyncio.to_thread(_log)

# ─── MCP Server ───────────────────────────────────────────────────────
server = Server("shopify")

# Session store
_session_meta = {}

# ─── Shopify Init ─────────────────────────────────────────────────────
def init_shopify():
    session = shopify.Session(
        os.getenv("SHOPIFY_SHOP_URL"),
        '2025-01',
        os.getenv("SHOPIFY_ACCESS_TOKEN")
    )
    shopify.ShopifyResource.activate_session(session)

# ─── SAFE SHOPIFY WRAPPER ─────────────────────────────────────────────
async def safe_shopify_call(fn):
    try:
        return await asyncio.wait_for(fn(), timeout=10)
    except Exception as e:
        logger.error(f"[SHOPIFY ERROR] {e}")
        return []

# ─── Shopify Helpers ──────────────────────────────────────────────────
async def find_customers(limit=250):
    async def _run():
        def _find():
            init_shopify()
            return shopify.Customer.find(limit=limit)
        return await asyncio.to_thread(_find)

    return await safe_shopify_call(_run)


async def find_orders(limit=250):
    async def _run():
        def _find():
            init_shopify()
            return shopify.Order.find(limit=limit, status="any")
        return await asyncio.to_thread(_find)

    return await safe_shopify_call(_run)


async def find_products(limit=50):
    async def _run():
        def _find():
            init_shopify()
            return shopify.Product.find(limit=limit)
        return await asyncio.to_thread(_find)

    return await safe_shopify_call(_run)


async def find_checkouts(limit=20):
    async def _run():
        def _find():
            init_shopify()
            return shopify.Checkout.find(limit=limit)
        return await asyncio.to_thread(_find)

    return await safe_shopify_call(_run)

# ─── Formatters ───────────────────────────────────────────────────────
def format_customer(c):
    return {
        "id": c.id,
        "email": getattr(c, 'email', 'N/A'),
        "orders": getattr(c, 'orders_count', 0),
        "spent": getattr(c, 'total_spent', '0')
    }

def format_order(o):
    return {
        "id": o.id,
        "total": o.total_price,
        "email": getattr(o, 'email', 'N/A')
    }

# ─── MCP Tools ───────────────────────────────────────────────────────
@server.list_tools()
async def list_tools():
    return [
        types.Tool(name="get-customers", inputSchema={"type": "object"}),
        types.Tool(name="get-orders", inputSchema={"type": "object"}),
    ]

@server.call_tool()
async def call_tool(name, arguments):
    if not arguments:
        arguments = {}

    start = time.time()

    session_id = arguments.get("session_id", "unknown")
    meta = _session_meta.get(session_id, {})
    ip_address = meta.get("ip", "unknown")

    try:
        if name == "get-customers":
            customers = await find_customers(20)
            result = [format_customer(c) for c in customers]

        elif name == "get-orders":
            orders = await find_orders(20)
            result = [format_order(o) for o in orders]

        else:
            raise ValueError("Unknown tool")

        elapsed = int((time.time() - start) * 1000)

        await log_to_mongo_async(
            session_id,
            ip_address,
            name,
            arguments,
            f"{len(result)} results",
            "success",
            None,
            elapsed
        )

        return [types.TextContent(type="text", text=str(result))]

    except Exception as e:
        elapsed = int((time.time() - start) * 1000)

        await log_to_mongo_async(
            session_id,
            ip_address,
            name,
            arguments,
            None,
            "error",
            str(e),
            elapsed
        )

        return [types.TextContent(type="text", text=f"Error: {str(e)}")]

# ─── REST (for testing) ───────────────────────────────────────────────
async def health(request):
    return JSONResponse({"status": "ok"})

# ─── SSE SERVER ───────────────────────────────────────────────────────
def run_sse_server():
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        session_id = request.query_params.get("session_id", "unknown")
        ip = request.client.host

        _session_meta[session_id] = {
            "ip": ip,
            "connected_at": time.time()
        }

        logger.info(f"[CONNECT] {session_id} | {ip}")

        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(
                streams[0],
                streams[1],
                InitializationOptions(
                    server_name="shopify",
                    server_version="1.0"
                )
            )

    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages/", endpoint=sse.handle_post_message, methods=["POST"]),
        Route("/health", endpoint=health),
    ])

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

# ─── ENTRY ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "sse"

    if mode == "sse":
        run_sse_server()
    else:
        asyncio.run(server.run())