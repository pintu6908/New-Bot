import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (ApplicationBuilder, CommandHandler, ContextTypes,
                          MessageHandler, filters)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("videobot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "48")) * 1024 * 1024
COOLDOWN = int(os.getenv("COOLDOWN_SEC", "10"))
IG_COOKIES = os.getenv("IG_COOKIES_FILE") or None
TB_URL = os.getenv("TERABOX_RESOLVER_URL") or None
TB_KEY = os.getenv("TERABOX_RESOLVER_KEY") or None
TB_COOKIE = os.getenv("TERABOX_NDUS_COOKIE") or None   # optional, helps with gated shares
SEM = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "3")))
last_request: dict[int, float] = {}

IG_RE = re.compile(r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv|stories)/[^\s]+", re.I)
TB_HOSTS = ("terabox.com", "teraboxapp.com", "1024terabox.com", "terasharelink.com",
            "4funbox.com", "mirrobox.com", "nephobox.com", "freeterabox.com",
            "momerybox.com", "tibibox.com", "terabox.app", "teraboxshare.com",
            "teraboxlink.com", "terafileshare.com", "teraboxurl.com")
TB_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:%s)/[^\s]+" % "|".join(map(re.escape, TB_HOSTS)), re.I)


class UserError(Exception):
    """Error message that is safe to show to the user."""


# ---------- Instagram (yt-dlp) ----------
def _ytdlp_download(url: str, out_dir: Path) -> list[dict]:
    opts = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "format": "b[ext=mp4]/b",          # single muxed file, no ffmpeg required
        "max_filesize": MAX_BYTES,
        "playlistend": 5,                  # carousels: cap items
        "quiet": True, "no_warnings": True, "noprogress": True,
        "socket_timeout": 30, "retries": 3,
    }
    if IG_COOKIES:
        opts["cookiefile"] = IG_COOKIES
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    entries = info.get("entries") or [info]
    results = []
    for e in entries:
        if not e:
            continue
        for d in e.get("requested_downloads") or []:
            p = Path(d["filepath"])
            if p.exists():
                results.append({"path": p, "width": e.get("width"),
                                "height": e.get("height"),
                                "duration": int(e["duration"]) if e.get("duration") else None})
    return results


async def download_instagram(url: str, out_dir: Path) -> list[dict]:
    try:
        res = await asyncio.to_thread(_ytdlp_download, url, out_dir)
    except DownloadError as e:
        msg = str(e).lower()
        if any(k in msg for k in ("login", "private", "cookies", "rate-limit", "not available")):
            raise UserError("Instagram blocked this download (private post or login required).")
        log.warning("yt-dlp failed: %s", e)
        raise UserError("Couldn't download that Instagram link.")
    if not res:
        raise UserError("No video found in that post (photo-only or too large).")
    return res


# ---------- TeraBox ----------
TB_APP_ID = "250528"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _tb_parts(url: str) -> tuple[str, list[str]]:
    """Return (host, candidate share keys). Keys are tried with and without the leading '1'."""
    p = urlparse(url)
    code = (parse_qs(p.query).get("surl") or [""])[0]
    if not code:
        m = re.search(r"/s/([\w-]+)", p.path)
        code = m.group(1) if m else ""
    if not code:
        raise UserError("That doesn't look like a valid TeraBox share link.")
    bare = code[1:] if code.startswith("1") else code
    return p.netloc, list(dict.fromkeys([bare, "1" + bare]))


def _tb_pick(items: list[dict]) -> tuple[str, str, int]:
    files = [i for i in items if str(i.get("isdir", "0")) in ("0", "False", "false")]
    if not files:
        raise UserError("This share is a folder (nested folders aren't supported).")
    vids = [i for i in files if str(i.get("category")) == "1"] or files
    f = vids[0]
    if not f.get("dlink"):
        raise LookupError("no dlink")
    return f["dlink"], f.get("server_filename") or "video.mp4", int(f.get("size") or 0)


def _tb_headers(referer: str) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": referer}
    if TB_COOKIE:
        h["Cookie"] = f"ndus={TB_COOKIE}"
    return h


async def _tb_builtin(s: aiohttp.ClientSession, url: str) -> tuple[str, str, int]:
    """Anonymous TeraBox /share/list lookup (unofficial; may be blocked by TeraBox)."""
    host, keys = _tb_parts(url)
    hosts = list(dict.fromkeys([host, "www.terabox.com", "www.1024terabox.com"]))
    for h in hosts:
        for key in keys:
            params = {"app_id": TB_APP_ID, "web": 1, "channel": "dubox",
                      "clienttype": 0, "shorturl": key, "root": 1}
            try:
                async with s.get(f"https://{h}/share/list", params=params,
                                 headers=_tb_headers(f"https://{h}/sharing/link?surl={key}"),
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                continue
            if isinstance(data, dict) and data.get("errno") == 0 and data.get("list"):
                return _tb_pick(data["list"])
            log.info("terabox list %s key=%s errno=%s", h, key,
                     data.get("errno") if isinstance(data, dict) else "?")
    raise LookupError("builtin resolver failed")


def _tb_parse_external(data) -> tuple[str, str, int]:
    if isinstance(data, dict) and isinstance(data.get("files"), list) and data["files"]:
        data = data["files"][0]
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise LookupError("bad external response")
    dl = next((data[k] for k in ("download_url", "download_link", "dlink", "direct_link")
               if isinstance(data.get(k), str) and data[k]), "")
    if not dl:
        raise LookupError("no link in external response")
    name = data.get("filename") or data.get("file_name") or data.get("name") or "video.mp4"
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return dl, name, size


async def _tb_external(s: aiohttp.ClientSession, url: str) -> tuple[str, str, int]:
    """Optional fallback: GET TERABOX_RESOLVER_URL?url=<share> -> JSON with a direct link."""
    headers = {"Authorization": f"Bearer {TB_KEY}"} if TB_KEY else {}
    async with s.get(TB_URL, params={"url": url}, headers=headers,
                     timeout=aiohttp.ClientTimeout(total=45)) as r:
        if r.status != 200:
            raise LookupError(f"external HTTP {r.status}")
        return _tb_parse_external(await r.json(content_type=None))


async def download_terabox(url: str, out_dir: Path) -> list[dict]:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600, connect=15)) as s:
            resolved = None
            for resolver in (_tb_builtin, _tb_external if TB_URL else None):
                if resolver is None:
                    continue
                try:
                    resolved = await resolver(s, url)
                    break
                except LookupError as e:
                    log.info("terabox resolver %s failed: %s", resolver.__name__, e)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.info("terabox resolver %s network error: %s", resolver.__name__, e)
            if not resolved:
                raise UserError("Couldn't get this TeraBox video. The link may be expired, "
                                "password-protected, or TeraBox is blocking anonymous access.")
            dl, name, size = resolved
            if not dl.startswith("https://"):
                dl = dl.replace("http://", "https://", 1)
            if size > MAX_BYTES:
                raise UserError("Video is larger than the Telegram upload limit "
                                f"({MAX_BYTES // 1048576} MB).")
            name = re.sub(r"[^\w.\- ]", "_", name)[:80] or "video.mp4"
            path, written = out_dir / name, 0
            async with s.get(dl, headers=_tb_headers("https://www.terabox.com/")) as r:
                if r.status != 200:
                    raise UserError(f"TeraBox download failed (HTTP {r.status}).")
                if int(r.headers.get("Content-Length") or 0) > MAX_BYTES:
                    raise UserError("Video is larger than the Telegram upload limit.")
                with open(path, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        written += len(chunk)
                        if written > MAX_BYTES:
                            raise UserError("Video is larger than the Telegram upload limit.")
                        f.write(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise UserError("Network error while contacting TeraBox. Try again.")
    return [{"path": path, "width": None, "height": None, "duration": None}]


# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me an Instagram (post/reel) or TeraBox share link and I'll send the video back.")


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if ALLOWED and user.id not in ALLOWED:
        return
    text = msg.text or ""
    ig, tb = IG_RE.search(text), TB_RE.search(text)
    if not (ig or tb):
        return
    now = time.monotonic()
    if now - last_request.get(user.id, 0) < COOLDOWN:
        await msg.reply_text(f"Slow down — wait {COOLDOWN}s between requests.")
        return
    last_request[user.id] = now

    status = await msg.reply_text("⏳ Working on it…")
    try:
        async with SEM:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                files = await (download_instagram(ig.group(0), out) if ig
                               else download_terabox(tb.group(0), out))
                await ctx.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_VIDEO)
                for f in files:
                    if f["path"].stat().st_size > MAX_BYTES:
                        raise UserError("Video exceeds Telegram's upload limit.")
                    t = dict(read_timeout=120, write_timeout=300, connect_timeout=30)
                    try:
                        with open(f["path"], "rb") as fh:
                            await msg.reply_video(
                                video=fh, supports_streaming=True, width=f["width"],
                                height=f["height"], duration=f["duration"], **t)
                    except BadRequest:   # e.g. unsupported codec/container -> send as file
                        with open(f["path"], "rb") as fh:
                            await msg.reply_document(document=fh, **t)
        await status.delete()
    except UserError as e:
        await status.edit_text(f"❌ {e}")
    except Exception:
        log.exception("Unhandled error for %s", user.id)
        await status.edit_text("❌ Something went wrong. Try again later.")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
