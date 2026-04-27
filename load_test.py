import asyncio
import aiohttp
import time
import random
from datetime import datetime

BASE_URL = "https://shopify-mcp-server-gx4l.onrender.com"

USER_PROFILES = [
    {"type": "Marketing Manager", "endpoints": ["/revenue", "/customers", "/customers/top-spenders"]},
    {"type": "Ad Campaign Manager", "endpoints": ["/customers/top-spenders", "/customers/subscribers"]},
    {"type": "Sales Analyst", "endpoints": ["/orders", "/revenue", "/abandoned"]},
    {"type": "Product Manager", "endpoints": ["/products", "/revenue"]},
    {"type": "Re-engagement Bot", "endpoints": ["/abandoned", "/customers/subscribers"]},
    {"type": "Dashboard", "endpoints": ["/revenue", "/customers", "/orders", "/products"]},
]

results = []

async def simulate_user(session, user_id, profile):
    user_results = []
    print(f"[{datetime.now().strftime('%H:%M:%S')}] User {user_id} ({profile['type']}) started")

    for endpoint in profile["endpoints"]:
        start = time.time()
        try:
            async with session.get(
                f"{BASE_URL}{endpoint}",
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                data = await resp.text()
                elapsed = round(time.time() - start, 2)
                status = "✅" if resp.status == 200 else "❌"
                print(f"  {status} User {user_id} ({profile['type']}) | {endpoint} | {resp.status} | {elapsed}s")
                user_results.append({
                    "user_id": user_id,
                    "user_type": profile["type"],
                    "endpoint": endpoint,
                    "status": resp.status,
                    "time": elapsed,
                    "success": resp.status == 200
                })
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            print(f"  ❌ User {user_id} | {endpoint} | Error: {str(e)[:60]} | {elapsed}s")
            user_results.append({
                "user_id": user_id,
                "user_type": profile["type"],
                "endpoint": endpoint,
                "status": 0,
                "time": elapsed,
                "success": False
            })

        await asyncio.sleep(random.uniform(0.1, 0.3))

    return user_results

async def run_load_test(num_users=20):
    print(f"\n{'='*60}")
    print(f"  NEOLOOK API LOAD TEST — {num_users} USERS")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    start_total = time.time()
    all_results = []

    async with aiohttp.ClientSession() as session:
        tasks = []
        for i in range(1, num_users + 1):
            profile = USER_PROFILES[i % len(USER_PROFILES)]
            tasks.append(simulate_user(session, i, profile))

        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            all_results.extend(r)

    total_time = round(time.time() - start_total, 2)
    total_requests = len(all_results)
    successful = sum(1 for r in all_results if r["success"])
    failed = total_requests - successful
    avg_time = round(sum(r["time"] for r in all_results) / total_requests, 2) if total_requests else 0
    max_time = round(max(r["time"] for r in all_results), 2) if total_requests else 0
    min_time = round(min(r["time"] for r in all_results), 2) if total_requests else 0

    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  Total Users:        {num_users}")
    print(f"  Total Requests:     {total_requests}")
    print(f"  Successful:         {successful} ✅")
    print(f"  Failed:             {failed} ❌")
    print(f"  Success Rate:       {round(successful/total_requests*100, 1)}%")
    print(f"  Total Time:         {total_time}s")
    print(f"  Avg Response Time:  {avg_time}s")
    print(f"  Min Response Time:  {min_time}s")
    print(f"  Max Response Time:  {max_time}s")

    print(f"\n  Per endpoint breakdown:")
    endpoint_stats = {}
    for r in all_results:
        ep = r["endpoint"]
        if ep not in endpoint_stats:
            endpoint_stats[ep] = {"total": 0, "success": 0, "times": []}
        endpoint_stats[ep]["total"] += 1
        endpoint_stats[ep]["times"].append(r["time"])
        if r["success"]:
            endpoint_stats[ep]["success"] += 1

    for ep, stats in sorted(endpoint_stats.items()):
        avg = round(sum(stats["times"]) / len(stats["times"]), 2)
        print(f"    {ep}: {stats['success']}/{stats['total']} success | avg {avg}s")

    print(f"{'='*60}\n")

if __name__ == "__main__":
    # Run progressively: 10 → 20 → 50
    for n in [10, 20, 50]:
        print(f"\n>>> Running with {n} users...")
        asyncio.run(run_load_test(num_users=n))
        time.sleep(3)