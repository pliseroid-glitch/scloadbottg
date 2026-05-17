"""Manual test of SoundCloud streaming flow."""
import asyncio
import re
import aiohttp


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


async def get_client_id(session):
    async with session.get("https://soundcloud.com/") as resp:
        html = await resp.text()
    scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
    print(f"Found {len(scripts)} scripts")
    for s in reversed(scripts):
        async with session.get(s) as resp:
            js = await resp.text()
            m = re.search(r'client_id\s*[:=]\s*"([a-zA-Z0-9]{30,})"', js)
            if m:
                return m.group(1)
    return None


async def main():
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        cid = await get_client_id(session)
        print(f"client_id: {cid}")

        # Search
        url = "https://api-v2.soundcloud.com/search/tracks"
        async with session.get(url, params={"q": "brainrot rap", "client_id": cid, "limit": 3}) as resp:
            print(f"Search status: {resp.status}")
            if resp.status != 200:
                print(await resp.text())
                return
            data = await resp.json()
            tracks = data.get("collection", [])

        if not tracks:
            print("No tracks")
            return

        track = tracks[0]
        print(f"\nTrack: {track.get('title')}")
        print(f"  id: {track.get('id')}")
        print(f"  permalink: {track.get('permalink_url')}")
        print(f"  track_authorization: {track.get('track_authorization', '')[:30]}...")

        media = track.get("media", {})
        print(f"\nTranscodings ({len(media.get('transcodings', []))}):")
        for t in media.get("transcodings", []):
            fmt = t.get("format", {})
            print(f"  - protocol={fmt.get('protocol')} mime={fmt.get('mime_type')} preset={t.get('preset')}")
            print(f"    url: {t.get('url')}")

        # Try fetching stream URL for each transcoding
        for t in media.get("transcodings", []):
            stream_api_url = t.get("url")
            params = {"client_id": cid}
            if track.get("track_authorization"):
                params["track_authorization"] = track["track_authorization"]
            print(f"\nFetching: {stream_api_url}")
            async with session.get(stream_api_url, params=params) as resp:
                body = await resp.text()
                print(f"  Status: {resp.status}")
                print(f"  Body: {body[:300]}")


asyncio.run(main())
