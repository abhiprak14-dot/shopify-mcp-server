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
    access_token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    session = shopify.Session(shop_url, '2025-01', access_token)
    shopify.ShopifyResource.activate_session(session)

def format_customer_for_ads(c):
    addr = c.default_address
    return (
        f"ID: {c.id}\n"
        f"Name: {c.first_name} {c.last_name}\n"
        f"Email: {c.email}\n"
        f"Phone: {getattr(c, 'phone', 'N/A')}\n"
        f"City: {addr.city if addr else 'N/A'}\n"
        f"State: {addr.province if addr else 'N/A'}\n"
        f"Country: {addr.country if addr else 'N/A'}\n"
        f"Total Spent: ${c.total_spent}\n"
        f"Orders Count: {c.orders_count}\n"
        f"Accepts Marketing: {c.accepts_marketing}\n"
        f"Tags: {c.tags}\n"
        f"Created At: {c.created_at}\n"
        f"---"
    )

def format_order(o):
    items = ', '.join([i.title for i in o.line_items]) if o.line_items else 'N/A'
    return (
        f"Order ID: {o.id}\n"
        f"Order Number: {o.order_number}\n"
        f"Email: {o.email}\n"
        f"Total: ${o.total_price}\n"
        f"Financial Status: {o.financial_status}\n"
        f"Fulfillment Status: {o.fulfillment_status}\n"
        f"Items: {items}\n"
        f"Source: {getattr(o, 'referring_site', 'N/A')}\n"
        f"Created At: {o.created_at}\n"
        f"---"
    )

def format_abandoned(a):
    items = ', '.join([i.title for i in a.line_items]) if a.line_items else 'N/A'
    return (
        f"Checkout ID: {a.id}\n"
        f"Email: {getattr(a, 'email', 'N/A')}\n"
        f"Phone: {getattr(a, 'phone', 'N/A')}\n"
        f"Total: ${a.total_price}\n"
        f"Items Abandoned: {items}\n"
        f"Abandoned At: {a.created_at}\n"
        f"Recovery URL: {getattr(a, 'abandoned_checkout_url', 'N/A')}\n"
        f"---"
    )

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get-customers-for-ads",
            description="Get full customer profiles optimized for Meta/Google ad targeting",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max customers to return (default: 50)"}}}
        ),
        types.Tool(
            name="get-top-spenders",
            description="Get highest value customers by total spend — ideal for lookalike audiences on Meta and Google",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max customers to return (default: 20)"}}}
        ),
        types.Tool(
            name="get-repeat-buyers",
            description="Get customers with 2+ orders — ideal for loyalty and retention campaigns",
            inputSchema={"type": "object", "properties": {"min_orders": {"type": "number", "description": "Minimum number of orders (default: 2)"}}}
        ),
        types.Tool(
            name="get-new-customers",
            description="Get first-time buyers — ideal for onboarding and welcome campaigns",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max customers to return (default: 20)"}}}
        ),
        types.Tool(
            name="get-abandoned-checkouts",
            description="Get abandoned carts with contact info — for WhatsApp/SMS/email re-engagement",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max abandoned checkouts to return (default: 20)"}}}
        ),
        types.Tool(
            name="get-non-buyers",
            description="Get customers who never placed an order — ideal for TOFU retargeting",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max customers to return (default: 20)"}}}
        ),
        types.Tool(
            name="get-customer-orders",
            description="Get full order history for a specific customer — for BOFU attribution",
            inputSchema={"type": "object", "required": ["customer_id"], "properties": {"customer_id": {"type": "string", "description": "Shopify customer ID"}}}
        ),
        types.Tool(
            name="get-all-orders",
            description="Get all store orders with source, items, and revenue — for campaign attribution",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max orders to return (default: 20)"}}}
        ),
        types.Tool(
            name="get-revenue-summary",
            description="Get overall store revenue metrics — total customers, orders, and revenue",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="get-marketing-subscribers",
            description="Get customers opted in to marketing — safe to target via email/WhatsApp/SMS",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max customers to return (default: 50)"}}}
        ),
        types.Tool(
            name="get-customers-by-location",
            description="Get customers grouped by city or country — for geo-targeted ad campaigns",
            inputSchema={"type": "object", "properties": {"location_type": {"type": "string", "description": "Group by 'city' or 'country' (default: country)"}}}
        ),
        types.Tool(
            name="get-product-list",
            description="Get list of products from the Shopify store",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number", "description": "Max products to return (default: 10)"}}}
        ),
    ]

@server.call_tool()
async def handle_call_tool(name, arguments):
    if not arguments:
        arguments = {}
    try:
        init_shopify()

        if name == "get-customers-for-ads":
            limit = int(arguments.get("limit", 50))
            customers = shopify.Customer.find(limit=limit)
            if not customers:
                return [types.TextContent(type="text", text="No customers found")]
            result = f"Customer Ad Profiles ({len(customers)}):\n\n" + "\n".join([format_customer_for_ads(c) for c in customers])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-top-spenders":
            limit = int(arguments.get("limit", 20))
            customers = shopify.Customer.find(limit=250)
            sorted_customers = sorted(customers, key=lambda c: float(c.total_spent), reverse=True)[:limit]
            if not sorted_customers:
                return [types.TextContent(type="text", text="No customers found")]
            result = f"Top {len(sorted_customers)} Spenders (Lookalike Audience):\n\n" + "\n".join([format_customer_for_ads(c) for c in sorted_customers])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-repeat-buyers":
            min_orders = int(arguments.get("min_orders", 2))
            customers = shopify.Customer.find(limit=250)
            repeat = [c for c in customers if int(c.orders_count) >= min_orders]
            if not repeat:
                return [types.TextContent(type="text", text="No repeat buyers found")]
            result = f"Repeat Buyers ({len(repeat)} customers with {min_orders}+ orders):\n\n" + "\n".join([format_customer_for_ads(c) for c in repeat])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-new-customers":
            limit = int(arguments.get("limit", 20))
            customers = shopify.Customer.find(limit=limit)
            new = [c for c in customers if int(c.orders_count) == 1]
            if not new:
                return [types.TextContent(type="text", text="No first-time buyers found")]
            result = f"First-Time Buyers ({len(new)}):\n\n" + "\n".join([format_customer_for_ads(c) for c in new])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-abandoned-checkouts":
            limit = int(arguments.get("limit", 20))
            abandoned = shopify.Checkout.find(limit=limit)
            if not abandoned:
                return [types.TextContent(type="text", text="No abandoned checkouts found")]
            result = f"Abandoned Checkouts ({len(abandoned)}) — Re-engagement Targets:\n\n" + "\n".join([format_abandoned(a) for a in abandoned])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-non-buyers":
            limit = int(arguments.get("limit", 20))
            customers = shopify.Customer.find(limit=limit)
            non_buyers = [c for c in customers if int(c.orders_count) == 0]
            if not non_buyers:
                return [types.TextContent(type="text", text="No non-buyers found")]
            result = f"Non-Buyers ({len(non_buyers)}) — TOFU Retargeting:\n\n" + "\n".join([format_customer_for_ads(c) for c in non_buyers])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-customer-orders":
            customer_id = arguments.get("customer_id")
            orders = shopify.Order.find(customer_id=customer_id, limit=50)
            if not orders:
                return [types.TextContent(type="text", text=f"No orders found for customer {customer_id}")]
            result = f"Orders for Customer {customer_id} ({len(orders)} orders):\n\n" + "\n".join([format_order(o) for o in orders])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-all-orders":
            limit = int(arguments.get("limit", 20))
            orders = shopify.Order.find(limit=limit, status="any")
            if not orders:
                return [types.TextContent(type="text", text="No orders found")]
            result = f"All Orders ({len(orders)}):\n\n" + "\n".join([format_order(o) for o in orders])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-revenue-summary":
            customers = shopify.Customer.find(limit=250)
            orders = shopify.Order.find(limit=250, status="any")
            total_revenue = sum(float(o.total_price) for o in orders)
            total_customers = len(customers)
            total_orders = len(orders)
            buyers = [c for c in customers if int(c.orders_count) > 0]
            repeat_buyers = [c for c in customers if int(c.orders_count) >= 2]
            marketing_opted = [c for c in customers if c.accepts_marketing]
            summary = (
                f"=== Neolook Store Revenue Summary ===\n\n"
                f"Total Customers: {total_customers}\n"
                f"Total Orders: {total_orders}\n"
                f"Total Revenue: ${total_revenue:.2f}\n"
                f"Average Order Value: ${total_revenue/total_orders:.2f}\n\n"
                f"Buyers: {len(buyers)}\n"
                f"Repeat Buyers: {len(repeat_buyers)}\n"
                f"Non-Buyers: {total_customers - len(buyers)}\n"
                f"Marketing Subscribers: {len(marketing_opted)}\n"
            )
            return [types.TextContent(type="text", text=summary)]

        elif name == "get-marketing-subscribers":
            limit = int(arguments.get("limit", 50))
            customers = shopify.Customer.find(limit=limit)
            subscribers = [c for c in customers if c.accepts_marketing]
            if not subscribers:
                return [types.TextContent(type="text", text="No marketing subscribers found")]
            result = f"Marketing Subscribers ({len(subscribers)}) — Safe to Target:\n\n" + "\n".join([format_customer_for_ads(c) for c in subscribers])
            return [types.TextContent(type="text", text=result)]

        elif name == "get-customers-by-location":
            location_type = arguments.get("location_type", "country")
            customers = shopify.Customer.find(limit=250)
            groups = {}
            for c in customers:
                if c.default_address:
                    key = c.default_address.country if location_type == "country" else c.default_address.city
                    if key not in groups:
                        groups[key] = []
                    groups[key].append(f"{c.first_name} {c.last_name} ({c.email})")
            if not groups:
                return [types.TextContent(type="text", text="No location data found")]
            result = f"Customers by {location_type.title()} (Geo-targeting):\n\n"
            for location, names in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True):
                result += f"{location}: {len(names)} customers\n"
                result += "\n".join([f"  - {n}" for n in names]) + "\n\n"
            return [types.TextContent(type="text", text=result)]

        elif name == "get-product-list":
            limit = int(arguments.get("limit", 10))
            products = shopify.Product.find(limit=limit)
            if not products:
                return [types.TextContent(type="text", text="No products found")]
            result = "\n".join([f"Title: {p.title}\nID: {p.id}\nPrice: ${p.variants[0].price if p.variants else 'N/A'}\nStatus: {p.status}\n---" for p in products])
            return [types.TextContent(type="text", text=f"Products ({len(products)}):\n\n{result}")]

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
    print(f"Starting Neolook Shopify MCP Server on http://0.0.0.0:{port}/sse")
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