import logging
import os
import sys
import asyncio
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import WebAppInfo
from aiogram.filters import Command
import aiosqlite
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

# --- НАСТРОЙКИ ---
TOKEN = "5916870826:AAF7pPc6IuzdwG50rY7MQz_beYw1nYN5NlY"
WEBAPP_URL = "https://zxc1x1.ru"
PORT = 8080
DB_NAME = "music.db"

COVERS_DIR = "covers"
os.makedirs(COVERS_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# --- DB ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Вернули cover_path
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT,
                unique_id TEXT,
                title TEXT,
                artist TEXT,
                duration INTEGER,
                user_id INTEGER,
                is_favorite BOOLEAN DEFAULT 0,
                cover_path TEXT
            )
        """)
        await db.execute(
            "CREATE TABLE IF NOT EXISTS playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, user_id INTEGER)")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS playlist_tracks (playlist_id INTEGER, track_id INTEGER, UNIQUE(playlist_id, track_id))")
        await db.commit()


# --- ЛОГИКА ОБЛОЖЕК (BITE + FALLBACK) ---
async def extract_smart_cover(audio: types.Audio, unique_id: str):
    cover_filename = f"{unique_id}.jpg"
    final_path = os.path.join(COVERS_DIR, cover_filename)

    if os.path.exists(final_path):
        return final_path

    # СПОСОБ 1: "Откусывание" (Bite) - качаем начало файла
    try:
        file_info = await bot.get_file(audio.file_id)
        tg_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

        # Качаем первые 300КБ (обычно хедер занимает 10-100КБ)
        headers = {"Range": "bytes=0-307200"}

        async with aiohttp.ClientSession() as session:
            async with session.get(tg_url, headers=headers) as resp:
                if resp.status in [200, 206]:
                    partial_data = await resp.read()

                    # Сохраняем временный огрызок
                    temp_mp3 = f"temp_{unique_id}.mp3"
                    with open(temp_mp3, "wb") as f:
                        f.write(partial_data)

                    # Пытаемся достать картинку из огрызка
                    found_data = None
                    try:
                        # Используем ID3 напрямую, так как MP3 может ругаться на обрывок
                        tags = ID3(temp_mp3)
                        for key, tag in tags.items():
                            if key.startswith("APIC") or key.startswith("PIC"):
                                found_data = tag.data
                                break
                    except Exception:
                        pass

                    # Удаляем мусор
                    if os.path.exists(temp_mp3):
                        os.remove(temp_mp3)

                    if found_data:
                        with open(final_path, "wb") as f:
                            f.write(found_data)
                        print(f"✅ Обложка (Bite): {unique_id}")
                        return final_path

    except Exception as e:
        print(f"⚠️ Bite failed: {e}")

    # СПОСОБ 2: Fallback (Миниатюра от Telegram)
    if audio.thumbnail:
        try:
            # Скачиваем thumb (он обычно маленький, но лучше чем ничего)
            await bot.download(audio.thumbnail.file_id, destination=final_path)
            print(f"✅ Обложка (Thumb): {unique_id}")
            return final_path
        except Exception as e:
            print(f"⚠️ Thumb failed: {e}")

    return None


# --- HANDLERS ---
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="🎵 Открыть Плеер", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )
    await message.answer("Плеер готов (Smart Covers).", reply_markup=kb)


@router.message(F.audio)
async def handle_audio(message: types.Message):
    audio = message.audio
    t = audio.title or "Unknown"
    a = audio.performer or "Unknown"

    # Запускаем поиск обложки
    cover_path = await extract_smart_cover(audio, audio.file_unique_id)

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO tracks (file_id, unique_id, title, artist, duration, user_id, cover_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (audio.file_id, audio.file_unique_id, t, a, audio.duration, message.from_user.id, cover_path)
        )
        await db.commit()
    await message.answer(f"✅ Добавлено: {a} - {t}")


# --- WEB ---
async def serve_index(request):
    return web.FileResponse('./static/index.html')


def get_cover_url(path):
    if not path: return None
    return f"/covers/{os.path.basename(path)}"


async def api_get_tracks(request):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tracks ORDER BY id DESC")
        rows = await cursor.fetchall()
        return web.json_response([{
            "id": r["id"], "title": r["title"], "artist": r["artist"],
            "cover_url": get_cover_url(r['cover_path']),
            "is_favorite": bool(r["is_favorite"])
        } for r in rows])


async def api_play_track(request):
    track_id = request.rel_url.query.get('track_id')
    return web.json_response({"url": f"/stream/{track_id}"})


# --- PROXY STREAMING ---
async def stream_proxy_handler(request):
    track_id = request.match_info['track_id']
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT file_id FROM tracks WHERE id=?", (track_id,))).fetchone()
        if not row: return web.Response(status=404, text="Track not found")
        file_id = row['file_id']

    try:
        file_info = await bot.get_file(file_id)
        tg_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

        headers = {}
        range_header = request.headers.get('Range')
        if range_header: headers['Range'] = range_header

        async with aiohttp.ClientSession() as session:
            async with session.get(tg_url, headers=headers) as tg_resp:
                response = web.StreamResponse(status=tg_resp.status, reason=tg_resp.reason)
                for h in ['Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges']:
                    if h in tg_resp.headers: response.headers[h] = tg_resp.headers[h]

                await response.prepare(request)
                async for chunk in tg_resp.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                return response
    except Exception as e:
        print(f"Stream Error: {e}")
        return web.Response(status=500)


# --- PLAYLISTS API ---
async def api_create_playlist(request):
    d = await request.json()
    async with aiosqlite.connect(DB_NAME) as db:
        c = await db.execute("INSERT INTO playlists (title, user_id) VALUES (?, ?)", (d['title'], 0))
        await db.commit()
    return web.json_response({"status": "ok", "id": c.lastrowid})


async def api_get_playlists(request):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM playlists ORDER BY id DESC")).fetchall()
        res = []
        for r in rows:
            c = (await (
                await db.execute("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id=?", (r['id'],))).fetchone())[0]
            res.append({"id": r['id'], "title": r['title'], "count": c})
    return web.json_response(res)


async def api_add_to_playlist(request):
    d = await request.json()
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)",
                             (d['playlist_id'], d['track_id']))
            await db.commit()
            return web.json_response({"status": "ok"})
        except:
            return web.json_response({"status": "exists"})


async def api_get_playlist_tracks(request):
    pl_id = request.rel_url.query.get('playlist_id')
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT t.* FROM tracks t JOIN playlist_tracks pt ON t.id=pt.track_id WHERE pt.playlist_id=?",
            (pl_id,))).fetchall()
        return web.json_response([{"id": r["id"], "title": r["title"], "artist": r["artist"],
                                   "cover_url": get_cover_url(r['cover_path']), "is_favorite": bool(r["is_favorite"])}
                                  for r in rows])


async def api_toggle_favorite(request):
    d = await request.json()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tracks SET is_favorite=? WHERE id=?", (d['is_favorite'], d['track_id']))
        await db.commit()
    return web.json_response({"status": "ok"})


async def api_delete_track(request):
    d = await request.json()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM tracks WHERE id=?", (d.get('track_id'),))
        await db.execute("DELETE FROM playlist_tracks WHERE track_id=?", (d.get('track_id'),))
        await db.commit()
    return web.json_response({"status": "ok"})


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    app = web.Application()

    app.router.add_static('/covers', path=COVERS_DIR)  # Раздаем сохраненные обложки
    app.router.add_get('/', serve_index)
    app.router.add_get('/stream/{track_id}', stream_proxy_handler)

    app.router.add_get('/api/tracks', api_get_tracks)
    app.router.add_get('/api/play', api_play_track)
    app.router.add_post('/api/playlists', api_create_playlist)
    app.router.add_get('/api/playlists', api_get_playlists)
    app.router.add_post('/api/playlists/add', api_add_to_playlist)
    app.router.add_get('/api/playlists/tracks', api_get_playlist_tracks)
    app.router.add_post('/api/favorite', api_toggle_favorite)
    app.router.add_post('/api/delete', api_delete_track)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"🌍 Сервер запущен на {WEBAPP_URL}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
