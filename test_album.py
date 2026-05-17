"""Test: check if SoundCloud track has album info."""
import asyncio
import re
import aiohttp
import json

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
}

async def get_client_id(session):
    async with session.get("https://soundcloud.com/") as resp:
        html = await resp.text()
    scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
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
        print(f"client_id: {cid[:8]}...")

        # Search for a track that's in an album
        url = "https://api-v2.soundcloud.com/search/tracks"
        async with session.get(url, params={"q": "sqwore нло", "client_id": cid, "limit": 3}) as resp:
            data = await resp.json()
            tracks = data.get("collection", [])

        if not tracks:
            print("No tracks found")
            return

        track = tracks[0]
        print(f"\nTrack: {track.get('title')}")
        print(f"  id: {track.get('id')}")
        print(f"  user.id: {track.get('user', {}).get('id')}")
        print(f"  user.username: {track.get('user', {}).get('username')}")
        
        # Check publisher_metadata
        pub = track.get("publisher_metadata") or {}
        print(f"\n  publisher_metadata: {json.dumps(pub, ensure_ascii=False, indent=4)}")
        
        # Check if there's any album/playlist reference
        for key in ["playlist", "album", "set_type", "track_count"]:
            if key in track:
                print(f"  {key}: {track[key]}")

        # Now check user's playlists
        user_id = track.get("user", {}).get("id")
        if user_id:
            pl_url = f"https://api-v2.soundcloud.com/users/{user_id}/playlists"
            async with session.get(pl_url, params={"client_id": cid, "limit": 20}) as resp:
                if resp.status == 200:
                    pl_data = await resp.json()
                    playlists = pl_data.get("collection", [])
                    print(f"\nUser playlists ({len(playlists)}):")
                    for pl in playlists:
                        print(f"  - {pl.get('title')} (id={pl.get('id')}, set_type={pl.get('set_type')}, tracks={pl.get('track_count')})")
                        # Check if our track is in this playlist
                        pl_tracks = pl.get("tracks", [])
                        for t in pl_tracks:
                            if t.get("id") == track.get("id"):
                                print(f"    ^^^ FOUND OUR TRACK HERE!")
                                break

asyncio.run(main())
