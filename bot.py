import os
import re
import json
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from urllib.parse import quote

# جرب API أولًا (إن اشتغل)، وإلا fallback للصفحة
PROFILE_API = "https://api.rec.net/api/players/v1/profiles/{u}"
RECNET_USER_PAGE = "https://rec.net/user/{u}"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Referer": "https://rec.net/",
}

def human_time(ts):
    if not ts:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        h = int(delta.total_seconds() // 3600)
        if h < 1:
            return "Just now"
        if h < 24:
            return f"{h} hours ago"
        return f"{h//24} days ago"
    except Exception:
        return ts

async def get_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            return r.status, None, await r.text()
        return r.status, await r.json(), None

def extract_from_recnet_html(html: str) -> dict:
    """
    نحاول نطلع أي JSON/قيم مفيدة من صفحة rec.net/user/<username>.
    الصفحة تختلف، فنسوي أكثر من محاولة:
    - accountId
    - username / displayName
    - profileImage
    """
    out = {}

    # 1) accountId (غالبًا موجود كرقم)
    m = re.search(r'"accountId"\s*:\s*(\d+)', html)
    if m:
        out["accountId"] = int(m.group(1))

    # 2) username
    m = re.search(r'"username"\s*:\s*"([^"]+)"', html)
    if m:
        out["username"] = m.group(1)

    # 3) displayName
    m = re.search(r'"displayName"\s*:\s*"([^"]+)"', html)
    if m:
        out["displayName"] = m.group(1)

    # 4) profileImage
    m = re.search(r'"profileImage"\s*:\s*"([^"]+)"', html)
    if m:
        out["profileImage"] = m.group(1)

    # إذا ما لقينا شيء، نعتبرها فشلت
    return out

async def fetch_from_recnet_page(session: aiohttp.ClientSession, username: str) -> dict:
    url = RECNET_USER_PAGE.format(u=quote(username))
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            return {}
        html = await r.text()
    return extract_from_recnet_html(html)

class RecRoomBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        # حل "outdated": sync للسيرفر فورًا
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            g = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            print(f"✅ Synced commands to guild {guild_id}")
        else:
            await self.tree.sync()
            print("✅ Synced commands globally (may take time)")

bot = RecRoomBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.tree.command(name="rec", description="Get Rec Room player profile (resilient)")
@app_commands.describe(username="Rec Room username (example: oy.r)")
async def rec(interaction: discord.Interaction, username: str):
    await interaction.response.defer(thinking=True)

    raw = username.strip()
    if not raw:
        return await interaction.followup.send("❌ اكتب يوزرنيم صحيح.")

    try:
        async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
            # 1) جرّب API profile
            status, prof, err_text = await get_json(session, PROFILE_API.format(u=quote(raw)))
            if status == 200 and isinstance(prof, dict) and prof:
                # عندنا بيانات كافية نعرضها
                data = {"username": raw, "profile": prof}
            else:
                # 2) fallback: صفحة rec.net
                page = await fetch_from_recnet_page(session, raw)
                if not page:
                    # اعرض سبب مختصر لو كان موجود
                    if status in (401, 403, 404):
                        return await interaction.followup.send("❌ ما قدرت أجيب بيانات اللاعب (ممكن Rec.net قافل الوصول مؤقتًا).")
                    return await interaction.followup.send("❌ ما لقيت اللاعب.")

                data = {"account": page, "profile": {}}

        # بناء الإيمبد
        acct = data.get("account", {})
        prof = data.get("profile", {})

        shown_username = acct.get("username") or raw
        title_name = acct.get("displayName") or shown_username

        embed = discord.Embed(
            title=f"{title_name}'s Rec Room Profile",
            color=discord.Color.red()
        )

        if acct.get("profileImage"):
            embed.set_thumbnail(url=acct["profileImage"])

        embed.add_field(name="Username", value=shown_username, inline=True)
        if acct.get("accountId"):
            embed.add_field(name="Account ID", value=str(acct["accountId"]), inline=True)

        # حقول الـ API لو توفر
        if prof:
            embed.add_field(name="Level", value=str(prof.get("level", "N/A")), inline=True)
            embed.add_field(name="Platform", value=prof.get("platform", "Unknown"), inline=True)
            embed.add_field(name="Status", value="Online 🟢" if prof.get("isOnline") else "Offline 🔴", inline=True)
            if prof.get("lastOnlineAt"):
                embed.add_field(name="Last Online", value=human_time(prof["lastOnlineAt"]), inline=True)
            embed.set_footer(text="RecCross • api.rec.net + rec.net fallback")
        else:
            embed.set_footer(text="RecCross • rec.net fallback")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ خطأ: {e}")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN env var first.")
    bot.run(token)