import asyncio
import aiohttp
import time
import random

URL = "https://shopify-mcp-server-gx4l.onrender.com/sse"

# CONFIG
NUM_USERS = 50
TEST_DURATION = 30  # seconds

class Stats:
    def __init__(self):
        self.connected = 0
        self.failed = 0
        self.messages = 0
        self.disconnects = 0

stats = Stats()

async def connect_user(user_id):
    session_timeout = aiohttp.ClientTimeout(total=None)  # no timeout for SSE
    headers = {
        "Accept": "text/event-stream",
        "User-Agent": f"load-tester-{user_id}",
        # Add auth token here if needed:
        # "Authorization": f"Bearer {token}"
    }

    async with aiohttp.ClientSession(timeout=session_timeout) as session:
        try:
            start = time.time()
            async with session.get(URL, headers=headers) as resp:
                if resp.status != 200:
                    print(f"User {user_id} | Failed with status {resp.status}")
                    stats.failed += 1
                    return

                stats.connected += 1
                print(f"User {user_id} connected")

                async for line in resp.content:
                    decoded = line.decode("utf-8").strip()

                    if decoded.startswith("data:"):
                        stats.messages += 1

                    # Stop after test duration
                    if time.time() - start > TEST_DURATION:
                        break

        except Exception as e:
            print(f"User {user_id} | Error: {e}")
            stats.failed += 1
        finally:
            stats.disconnects += 1

async def main():
    print(f"\nStarting SSE load test with {NUM_USERS} users for {TEST_DURATION}s...\n")

    tasks = [
        connect_user(i) for i in range(1, NUM_USERS + 1)
    ]

    await asyncio.gather(*tasks)

    print("\n=== TEST RESULTS ===")
    print(f"Connected Users: {stats.connected}")
    print(f"Failed Connections: {stats.failed}")
    print(f"Total Messages Received: {stats.messages}")
    print(f"Disconnects: {stats.disconnects}")

if __name__ == "__main__":
    asyncio.run(main())
