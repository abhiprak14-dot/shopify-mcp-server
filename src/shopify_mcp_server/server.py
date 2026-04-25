from typing import Any
import asyncio
import shopify
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
import mcp.server.stdio
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
import uvicorn
import os
import sys
from dotenv import load_dotenv

load_dotenv()

server = Server("shopify")

def init_shopify():
    shop_url = os.getenv("SHOPIFY_SHOP_URL")
    api_key = os.getenv("SHOPIFY_API_KEY")
    password = os.getenv("SHOPIFY_PASSWORD")
    access_token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    if not all([shop_url, api_key, password]):
        raise ValueError("Missing required Shopify credentials")
    session = shopify.Session(shop_url, '2025-01', access_token)
    shopify.ShopifyResource.activate_session(session)

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get-product-list",
            description="Get a list of products from the Shopify store",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Maximum number of products to return (default: 10)"}}},
        ),
        types.Tool(
            name="get-customer-list",
            description="Get a list of customers from the Shopify store",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Maximum number of customers to return (default: 10)"}}},
        ),
    ]

def format_product(product):
    return (f"Title: {product.title}\nID: {product.id}\nProduct Type: {product.product_type}\n"
            f"Vendor: {product.vendor}\nStatus: {product.status}\n"
            f"Price: ${product.variants[0].price if product.variants else 'N/A'}\n---")

def format_customer(customer):
    return (f"Name: {customer.first_name} {customer.last_name}\nID: {customer.id}\n"
            f"Email: {customer.email}\nOrders Count: {customer.orders_count}\n"
            f"Total Spent: ${customer.total_spent}\n---")

@server.call_tool()
async def handle_call_tool(name, arguments):
    if not arguments:
        arguments = {}
    try:
        init_shopify()
        if name == "get-product-list":
            limit = int(arguments.get("limit", 10))
            products = shopify.Product.find(limit=limit)
            if not products:
                return [types.TextContent(type="text", text="No products found")]
            return [types.TextContent(type="text", text=f"Products (showing {len(products)}):\n\n" + "\n".join([format_product(p) for p in products]))]
        elif name == "get-customer-list":
            limit = int(arguments.get("limit", 10))
            customers = shopify.Customer.find(limit=limit)
            if not customers:
                return [types.TextContent(type="text", text="No customers found")]
            return [types.TextContent(type="text", text=f"Customers (showing {len(customers)}):\n\n" + "\n".join([format_customer(c) for c in customers]))]
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]

def run_sse_server():
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(
                streams[0], streams[1],
                InitializationOptions(
                    server_name="shopify",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )

    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages/", endpoint=sse.handle_post_message, methods=["POST"]),
    ])

    port = int(os.getenv("PORT", 8000))
    print(f"Starting SSE server on http://0.0.0.0:{port}/sse")
    uvicorn.run(app, host="0.0.0.0", port=port)

async def run_stdio_server():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="shopify",
                server_version="0.1.0",
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
