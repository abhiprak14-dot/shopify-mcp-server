import asyncio
import time
import logging
from datetime import datetime, timezone
from functools import lru_cache
import shopify
from pymongo import MongoClient
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

# ─── MongoDB (Singleton + Async) ──────────────────────────────────────
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

async def log_to_mongo(session_id, ip_address, tool, input_args, output_summary, status, error_message, response_time_ms):
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

# ─── Safe Shopify Wrapper ─────────────────────────────────────────────
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

async def find_orders(limit=250, status="any"):
    async def _run():
        def _find():
            init_shopify()
            return shopify.Order.find(limit=limit, status=status)
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

async def find_customer_orders(customer_id, limit=50):
    async def _run():
        def _find():
            init_shopify()
            return shopify.Order.find(customer_id=customer_id, limit=limit)
        return await asyncio.to_thread(_find)
    return await safe_shopify_call(_run)

# ─── Formatters ───────────────────────────────────────────────────────
def format_customer_for_ads(c):
    addr = getattr(c, 'default_address', None)
    return {
        "id": c.id,
        "name": f"{getattr(c, 'first_name', 'N/A')} {getattr(c, 'last_name', 'N/A')}",
        "email": getattr(c, 'email', 'N/A'),
        "phone": getattr(c, 'phone', 'N/A'),
        "city": getattr(addr, 'city', 'N/A') if addr else 'N/A',
        "state": getattr(addr, 'province', 'N/A') if addr else 'N/A',
        "country": getattr(addr, 'country', 'N/A') if addr else 'N/A',
        "total_spent": getattr(c, 'total_spent', '0.00'),
        "orders_count": getattr(c, 'orders_count', 0),
        "accepts_marketing": str(getattr(c, 'accepts_marketing', getattr(c, 'email_marketing_consent', 'N/A'))),
        "tags": getattr(c, 'tags', 'N/A'),
        "created_at": getattr(c, 'created_at', 'N/A'),
    }

def format_order(o):
    return {
        "order_id": o.id,
        "order_number": o.order_number,
        "email": getattr(o, 'email', 'N/A'),
        "total": o.total_price,
        "financial_status": o.financial_status,
        "fulfillment_status": getattr(o, 'fulfillment_status', 'N/A'),
        "items": [i.title for i in o.line_items] if o.line_items else [],
        "source": getattr(o, 'referring_site', 'N/A'),
        "created_at": o.created_at,
    }

def format_abandoned(a):
    return {
        "checkout_id": a.id,
        "email": getattr(a, 'email', 'N/A'),
        "phone": getattr(a, 'phone', 'N/A'),
        "total": getattr(a, 'total_price', '0.00'),
        "items": [i.title for i in a.line_items] if getattr(a, 'line_items', None) else [],
        "abandoned_at": getattr(a, 'created_at', 'N/A'),
        "recovery_url": getattr(a, 'abandoned_checkout_url', 'N/A'),
    }

# ─── MCP Tools ───────────────────────────────────────────────────────
@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(name="get-customers-for-ads", description="Get full customer profiles optimized for Meta/Google ad targeting", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        types.Tool(name="get-top-spenders", description="Get highest value customers by total spend", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        types.Tool(name="get-repeat-buyers", description="Get customers with 2+ orders", inputSchema={"type": "object", "properties": {"min_orders": {"type": "number"}}}),
        types.Tool(name="get-new-customers", description="Get first-time buyers", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        types.Tool(name="get-abandoned-checkouts", description="Get abandoned carts", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        types.Tool(name="get-non-buyers", description="Get customers who never ordered", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        types.Tool(name="get-customer-orders", description="Get order history for a customer", inputSchema={"type": "object", "required": ["customer_id"], "properties": {"customer_id": {"type": "string"}}}),
        types.Tool(name="get-all-orders", description="Get all store orders", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        types.Tool(name="get-revenue-summary", description="Get store revenue metrics", inputSchema={"type": "object", "properties": {}}),
        types.Tool(name="get-marketing-subscribers", description="Get marketing opted-in customers", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        types.Tool(name="get-customers-by-location", description="Get customers by location", inputSchema={"type": "object", "properties": {"location_type": {"type": "string"}}}),
        types.Tool(name="get-product-list", description="Get product list", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
    ]

@server.call_tool()
async def handle_call_tool(name, arguments):
    if not arguments:
        arguments = {}

    start = time.time()
    session_id = arguments.get("session_id", "unknown")
    meta = _session_meta.get(session_id, {})
    ip_address = meta.get("ip", "unknown")

    logger.info(f"[MCP] {name} | session={session_id} | args={arguments}")

    try:
        if name == "get-customers-for-ads":
            limit = int(arguments.get("limit", 50))
            customers = await find_customers(limit)
            result = [format_customer_for_ads(c) for c in customers]
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(result)} customers returned", "success", None, elapsed)
            if not result:
                return [types.TextContent(type="text", text="No customers found")]
            return [types.TextContent(type="text", text=str(result))]

        elif name == "get-top-spenders":
            limit = int(arguments.get("limit", 20))
            customers = await find_customers(250)
            sorted_customers = sorted(customers, key=lambda c: float(getattr(c, 'total_spent', 0)), reverse=True)[:limit]
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(sorted_customers)} top spenders returned", "success", None, elapsed)
            return [types.TextContent(type="text", text=str([format_customer_for_ads(c) for c in sorted_customers]))]

        elif name == "get-repeat-buyers":
            min_orders = int(arguments.get("min_orders", 2))
            customers = await find_customers(250)
            repeat = [c for c in customers if int(getattr(c, 'orders_count', 0)) >= min_orders]
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(repeat)} repeat buyers returned", "success", None, elapsed)
            if not repeat:
                return [types.TextContent(type="text", text="No repeat buyers found")]
            return [types.TextContent(type="text", text=str([format_customer_for_ads(c) for c in repeat]))]

        elif name == "get-new-customers":
            limit = int(arguments.get("limit", 20))
            customers = await find_customers(limit)
            new = [c for c in customers if int(getattr(c, 'orders_count', 0)) == 1]
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(new)} new customers returned", "success", None, elapsed)
            if not new:
                return [types.TextContent(type="text", text="No first-time buyers found")]
            return [types.TextContent(type="text", text=str([format_customer_for_ads(c) for c in new]))]

        elif name == "get-abandoned-checkouts":
            limit = int(arguments.get("limit", 20))
            abandoned = await find_checkouts(limit)
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(abandoned)} abandoned checkouts returned", "success", None, elapsed)
            if not abandoned:
                return [types.TextContent(type="text", text="No abandoned checkouts found")]
            return [types.TextContent(type="text", text=str([format_abandoned(a) for a in abandoned]))]

        elif name == "get-non-buyers":
            limit = int(arguments.get("limit", 20))
            customers = await find_customers(limit)
            non_buyers = [c for c in customers if int(getattr(c, 'orders_count', 0)) == 0]
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(non_buyers)} non-buyers returned", "success", None, elapsed)
            if not non_buyers:
                return [types.TextContent(type="text", text="No non-buyers found")]
            return [types.TextContent(type="text", text=str([format_customer_for_ads(c) for c in non_buyers]))]

        elif name == "get-customer-orders":
            customer_id = arguments.get("customer_id")
            orders = await find_customer_orders(customer_id)
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(orders)} orders returned", "success", None, elapsed)
            if not orders:
                return [types.TextContent(type="text", text=f"No orders found for customer {customer_id}")]
            return [types.TextContent(type="text", text=str([format_order(o) for o in orders]))]

        elif name == "get-all-orders":
            limit = int(arguments.get("limit", 20))
            orders = await find_orders(limit)
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(orders)} orders returned", "success", None, elapsed)
            if not orders:
                return [types.TextContent(type="text", text="No orders found")]
            return [types.TextContent(type="text", text=str([format_order(o) for o in orders]))]

        elif name == "get-revenue-summary":
            customers = await find_customers(250)
            orders = await find_orders(250)
            total_revenue = sum(float(o.total_price) for o in orders)
            total_customers = len(customers)
            total_orders = len(orders)
            buyers = [c for c in customers if int(getattr(c, 'orders_count', 0)) > 0]
            repeat_buyers = [c for c in customers if int(getattr(c, 'orders_count', 0)) >= 2]
            marketing_opted = [c for c in customers if getattr(c, 'accepts_marketing', False) or getattr(c, 'email_marketing_consent', None)]
            avg_order = f"${total_revenue/total_orders:.2f}" if total_orders > 0 else "N/A"
            summary = (
                f"=== Neolook Store Revenue Summary ===\n\n"
                f"Total Customers: {total_customers}\n"
                f"Total Orders: {total_orders}\n"
                f"Total Revenue: ${total_revenue:.2f}\n"
                f"Average Order Value: {avg_order}\n\n"
                f"Buyers: {len(buyers)}\n"
                f"Repeat Buyers: {len(repeat_buyers)}\n"
                f"Non-Buyers: {total_customers - len(buyers)}\n"
                f"Marketing Subscribers: {len(marketing_opted)}\n"
            )
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"Revenue: ${total_revenue:.2f} | Customers: {total_customers} | Orders: {total_orders}", "success", None, elapsed)
            return [types.TextContent(type="text", text=summary)]

        elif name == "get-marketing-subscribers":
            limit = int(arguments.get("limit", 50))
            customers = await find_customers(limit)
            subscribers = [c for c in customers if getattr(c, 'accepts_marketing', False) or getattr(c, 'email_marketing_consent', None)]
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(subscribers)} subscribers returned", "success", None, elapsed)
            if not subscribers:
                return [types.TextContent(type="text", text="No marketing subscribers found")]
            return [types.TextContent(type="text", text=str([format_customer_for_ads(c) for c in subscribers]))]

        elif name == "get-customers-by-location":
            location_type = arguments.get("location_type", "country")
            customers = await find_customers(250)
            groups = {}
            for c in customers:
                addr = getattr(c, 'default_address', None)
                if addr:
                    key = getattr(addr, 'country', 'N/A') if location_type == "country" else getattr(addr, 'city', 'N/A')
                    if key not in groups:
                        groups[key] = []
                    groups[key].append(f"{getattr(c, 'first_name', 'N/A')} {getattr(c, 'last_name', 'N/A')}")
            result = "\n".join([f"{loc}: {len(names)} customers" for loc, names in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)])
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(groups)} locations returned", "success", None, elapsed)
            if not groups:
                return [types.TextContent(type="text", text="No location data found")]
            return [types.TextContent(type="text", text=result)]

        elif name == "get-product-list":
            limit = int(arguments.get("limit", 10))
            products = await find_products(limit)
            elapsed = int((time.time()-start)*1000)
            await log_to_mongo(session_id, ip_address, name, arguments, f"{len(products)} products returned", "success", None, elapsed)
            if not products:
                return [types.TextContent(type="text", text="No products found")]
            return [types.TextContent(type="text", text="\n".join([f"{p.title} - ${p.variants[0].price if p.variants else 'N/A'} ({p.status})" for p in products]))]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        elapsed = int((time.time()-start)*1000)
        logger.error(f"[MCP] {name} failed | {str(e)} | {elapsed}ms")
        await log_to_mongo(session_id, ip_address, name, arguments, None, "error", str(e), elapsed)
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]

# ─── REST Endpoints ───────────────────────────────────────────────────
async def rest_revenue(request: Request):
    start = time.time()
    logger.info(f"[REST] GET /revenue | ip={request.client.host}")
    try:
        customers = await find_customers(250)
        orders = await find_orders(250)
        total_revenue = sum(float(o.total_price) for o in orders)
        buyers = [c for c in customers if int(getattr(c, 'orders_count', 0)) > 0]
        repeat_buyers = [c for c in customers if int(getattr(c, 'orders_count', 0)) >= 2]
        marketing_opted = [c for c in customers if getattr(c, 'accepts_marketing', False) or getattr(c, 'email_marketing_consent', None)]
        return JSONResponse({
            "total_customers": len(customers),
            "total_orders": len(orders),
            "total_revenue": round(total_revenue, 2),
            "average_order_value": round(total_revenue/len(orders), 2) if orders else 0,
            "buyers": len(buyers),
            "repeat_buyers": len(repeat_buyers),
            "non_buyers": len(customers) - len(buyers),
            "marketing_subscribers": len(marketing_opted),
        })
    except Exception as e:
        logger.error(f"[REST] /revenue error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

async def rest_customers(request: Request):
    try:
        limit = int(request.query_params.get("limit", 50))
        customers = await find_customers(limit)
        return JSONResponse({"count": len(customers), "customers": [format_customer_for_ads(c) for c in customers]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def rest_orders(request: Request):
    try:
        limit = int(request.query_params.get("limit", 20))
        orders = await find_orders(limit)
        return JSONResponse({"count": len(orders), "orders": [format_order(o) for o in orders]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def rest_abandoned(request: Request):
    try:
        limit = int(request.query_params.get("limit", 20))
        abandoned = await find_checkouts(limit)
        return JSONResponse({"count": len(abandoned), "abandoned_checkouts": [format_abandoned(a) for a in abandoned]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def rest_products(request: Request):
    try:
        limit = int(request.query_params.get("limit", 20))
        products = await find_products(limit)
        return JSONResponse({"count": len(products), "products": [{"id": p.id, "title": p.title, "price": p.variants[0].price if p.variants else 'N/A', "status": p.status} for p in products]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def rest_top_spenders(request: Request):
    try:
        limit = int(request.query_params.get("limit", 20))
        customers = await find_customers(250)
        sorted_customers = sorted(customers, key=lambda c: float(getattr(c, 'total_spent', 0)), reverse=True)[:limit]
        return JSONResponse({"count": len(sorted_customers), "top_spenders": [format_customer_for_ads(c) for c in sorted_customers]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def rest_subscribers(request: Request):
    try:
        limit = int(request.query_params.get("limit", 50))
        customers = await find_customers(limit)
        subscribers = [c for c in customers if getattr(c, 'accepts_marketing', False) or getattr(c, 'email_marketing_consent', None)]
        return JSONResponse({"count": len(subscribers), "subscribers": [format_customer_for_ads(c) for c in subscribers]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

async def rest_health(request: Request):
    return JSONResponse({"status": "ok", "service": "neolook-shopify-mcp"})

async def rest_docs(request: Request):
    from starlette.responses import HTMLResponse
    html = """<!DOCTYPE html>
    <html>
    <head>
        <title>Neolook Shopify API</title>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="stylesheet" type="text/css" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.1.0/swagger-ui.css">
    </head>
    <body>
    <div id="swagger-ui"></div>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.1.0/swagger-ui-bundle.js"></script>
    <script>
    SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: '#swagger-ui',
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: "BaseLayout"
    })
    </script>
    </body>
    </html>"""
    return HTMLResponse(html)

async def rest_openapi(request: Request):
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Neolook Shopify API", "version": "1.0.0", "description": "Shopify customer and order data API for Neolook ad targeting"},
        "paths": {
            "/health": {"get": {"summary": "Health check", "tags": ["System"], "responses": {"200": {"description": "OK"}}}},
            "/revenue": {"get": {"summary": "Store revenue summary", "tags": ["Analytics"], "responses": {"200": {"description": "Revenue metrics"}}}},
            "/customers": {"get": {"summary": "All customer profiles", "tags": ["Customers"], "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}}], "responses": {"200": {"description": "Customer list"}}}},
            "/customers/top-spenders": {"get": {"summary": "Top spenders", "tags": ["Customers"], "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}], "responses": {"200": {"description": "Top spenders"}}}},
            "/customers/subscribers": {"get": {"summary": "Marketing subscribers", "tags": ["Customers"], "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}}], "responses": {"200": {"description": "Subscribers"}}}},
            "/orders": {"get": {"summary": "All store orders", "tags": ["Orders"], "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}], "responses": {"200": {"description": "Orders"}}}},
            "/abandoned": {"get": {"summary": "Abandoned checkouts", "tags": ["Orders"], "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}], "responses": {"200": {"description": "Abandoned checkouts"}}}},
            "/products": {"get": {"summary": "Product list", "tags": ["Products"], "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}], "responses": {"200": {"description": "Products"}}}},
        }
    }
    return JSONResponse(spec)

# ─── SSE Server ───────────────────────────────────────────────────────
def run_sse_server():
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        session_id = request.query_params.get("session_id", "unknown")
        ip = request.client.host
        _session_meta[session_id] = {"ip": ip, "connected_at": time.time()}
        logger.info(f"[SSE CONNECT] session={session_id} | ip={ip}")
        try:
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await server.run(
                    streams[0], streams[1],
                    InitializationOptions(
                        server_name="shopify",
                        server_version="1.0.0",
                        capabilities=server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    ),
                )
        except Exception as e:
            logger.error(f"[SSE ERROR] session={session_id} | {e}")
        finally:
            logger.info(f"[SSE DISCONNECT] session={session_id} | ip={ip}")
            _session_meta.pop(session_id, None)

    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages/", endpoint=sse.handle_post_message, methods=["POST"]),
        Route("/health", endpoint=rest_health),
        Route("/revenue", endpoint=rest_revenue),
        Route("/customers", endpoint=rest_customers),
        Route("/customers/top-spenders", endpoint=rest_top_spenders),
        Route("/customers/subscribers", endpoint=rest_subscribers),
        Route("/orders", endpoint=rest_orders),
        Route("/abandoned", endpoint=rest_abandoned),
        Route("/products", endpoint=rest_products),
        Route("/docs", endpoint=rest_docs),
        Route("/openapi.json", endpoint=rest_openapi),
    ])

    port = int(os.getenv("PORT", 8000))
    logger.info(f"✅ Neolook Shopify API starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

async def run_stdio_server():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="shopify",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "sse":
        run_sse_server()
    else:
        asyncio.run(run_stdio_server())