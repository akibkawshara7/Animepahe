import asyncio
import re
import random
import tempfile
import os
import execjs
from bs4 import BeautifulSoup
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import tls_client

# -------------------- Utility --------------------
def random_user_agent():
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15",
        "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    ]
    return random.choice(agents)


# -------------------- AnimePahe Scraper Class --------------------
class AnimePahe:
    def __init__(self):
        # Master Akib, updating to .com as requested
        self.base = "https://animepahe.com"
        self.headers = {
            "User-Agent": random_user_agent(),
            "Referer": f"{self.base}/",
        }
        # Using chrome_120 to bypass basic TLS fingerprinting
        self.session = tls_client.Session(client_identifier="chrome_120")

    async def get(self, url: str):
        """Run tls-client GET asynchronously to avoid blocking the event loop."""
        def _req():
            return self.session.get(url, headers=self.headers)
        return await asyncio.to_thread(_req)

    async def search(self, query: str):
        url = f"{self.base}/api?m=search&q={query}"
        r = await self.get(url)
        if r.status_code != 200:
            raise Exception(f"Search failed with status {r.status_code}")
        
        data = r.json()
        results = []
        for a in data.get("data", []):
            results.append({
                "id": a["id"],
                "title": a["title"],
                "url": f"{self.base}/anime/{a['session']}",
                "year": a.get("year"),
                "poster": a.get("poster"),
                "type": a.get("type"),
                "session": a.get("session")
            })
        return results

    async def get_episodes(self, anime_session: str, page: int = 1):
        # First, we need the internal ID from the anime page
        html_resp = await self.get(f"{self.base}/anime/{anime_session}")
        soup = BeautifulSoup(html_resp.text, "html.parser")
        
        # The internal ID is often found in the og:url or scripts
        meta = soup.find("meta", {"property": "og:url"})
        if not meta:
            raise Exception("Could not find session ID in meta tags")
            
        internal_id = meta["content"].split("/")[-1]

        # Fetch episodes using the internal ID
        api_url = f"{self.base}/api?m=release&id={internal_id}&sort=episode_asc&page={page}"
        r = await self.get(api_url)
        data = r.json()
        
        episodes = []
        for e in data.get("data", []):
            episodes.append({
                "id": e["id"],
                "number": e["episode"],
                "title": e.get("title") or f"Episode {e['episode']}",
                "snapshot": e.get("snapshot"),
                "session": e["session"],
            })
            
        return {
            "total": data.get("total"),
            "per_page": data.get("per_page"),
            "current_page": data.get("current_page"),
            "last_page": data.get("last_page"),
            "episodes": episodes
        }

    async def get_sources(self, anime_session: str, episode_session: str):
        play_url = f"{self.base}/play/{anime_session}/{episode_session}"
        r = await self.get(play_url)
        html = r.text

        # Regex to find the data-src (Kwik links) in buttons
        buttons = re.findall(
            r'<button[^>]+data-src="([^"]+)"[^>]+data-fansub="([^"]+)"[^>]+data-resolution="([^"]+)"[^>]+data-audio="([^"]+)"[^>]*>',
            html
        )

        sources = []
        for src, fansub, resolution, audio in buttons:
            if "kwik." in src:
                sources.append({
                    "url": src,
                    "quality": f"{resolution}p",
                    "fansub": fansub,
                    "audio": audio
                })

        # Fallback search for kwik links if buttons aren't parsed
        if not sources:
            kwik_links = re.findall(r"https?://kwik\.(si|cx|link)/e/\w+", html)
            sources = [{"url": link, "quality": "Unknown", "fansub": "Unknown", "audio": "Unknown"} for link in kwik_links]

        # Sort by quality (highest first)
        def sort_key(s):
            try: return int(s["quality"].replace("p", ""))
            except: return 0
        
        sources.sort(key=sort_key, reverse=True)
        return sources

    async def resolve_m3u8(self, kwik_url: str):
        """
        Resolves the Kwik link to a direct .m3u8 URL using a Node.js sandbox.
        Requires 'node' to be installed on the system.
        """
        r = await self.get(kwik_url)
        html = r.text
        
        # Check if direct m3u8 exists first
        direct_m = re.search(r"https?://[^'\"\s<>]+\.m3u8", html)
        if direct_m:
            return direct_m.group(0)

        # Look for the packed JS code (eval script)
        scripts = re.findall(r"(<script[^>]*>[\s\S]*?</script>)", html, re.IGNORECASE)
        script_block = None
        for s in scripts:
            if "eval(function(p,a,c,k,e,d)" in s:
                script_block = s
                break
        
        if not script_block:
            raise Exception("Could not find the JS execution block on Kwik page.")

        # Clean script tags
        js_code = re.sub(r"<(/?script[^>]*)>", "", script_block, flags=re.IGNORECASE).strip()

        # Wrap in a capturer for Node.js
        wrapper = """
        const __captured = [];
        const originalEval = eval;
        global.eval = (code) => {
            __captured.push(code);
            return originalEval(code);
        };
        """
        # Execute script and print result
        final_js = f"{wrapper}\n{js_code}\nconsole.log(__captured.join('\\n'));"

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
            tf.write(final_js)
            temp_name = tf.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "node", temp_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode()
            
            # Extract .m3u8 from the unpacked JS
            m3u8_link = re.search(r"https?://[^'\"\s]+\.m3u8[^\s'\"\)]*", output)
            if m3u8_link:
                return m3u8_link.group(0)
            
            raise Exception("Node execution succeeded but no m3u8 link was found in output.")
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


# -------------------- FastAPI Setup --------------------
app = FastAPI(title="AnimePahe Unofficial API")
pahe = AnimePahe()

@app.get("/")
async def root():
    return {"message": "AnimePahe API is running. Master Akib, use /docs for testing."}

@app.get("/search")
async def api_search(q: str = Query(..., description="Anime title to search")):
    try:
        return await pahe.search(q)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/episodes/{anime_session}")
async def api_episodes(anime_session: str, page: int = 1):
    try:
        return await pahe.get_episodes(anime_session, page)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sources")
async def api_sources(anime_session: str, episode_session: str):
    try:
        return await pahe.get_sources(anime_session, episode_session)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/resolve")
async def api_resolve(url: str):
    try:
        m3u8 = await pahe.resolve_m3u8(url)
        return {"m3u8": m3u8}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    
