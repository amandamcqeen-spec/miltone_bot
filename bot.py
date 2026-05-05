import json
import os
import logging
import asyncio
import sqlite3
import re
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    LabeledPrice, PreCheckoutQuery, InputMediaPhoto
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder

# ========== 1. ЗАГРУЗКА ТОКЕНА ==========
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
CHANNEL_ID = os.getenv("CHANNEL_ID")
HIGHLIGHT_PRICE = int(os.getenv("HIGHLIGHT_PRICE", 250))
EXTRA_AD_PRICE = int(os.getenv("EXTRA_AD_PRICE", 75))
BUMP_PRICE = int(os.getenv("BUMP_PRICE", 250))

if not TOKEN:
    raise ValueError("Токен не найден! Создайте файл .env")

if not CHANNEL_ID:
    print("⚠️ ВНИМАНИЕ: CHANNEL_ID не указан! Объявления не будут публиковаться в канале.")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ========== 2. RATE LIMITING (ЗАЩИТА ОТ СПАМА) ==========
user_requests = defaultdict(list)

def check_rate_limit(user_id: int, limit: int = 10, period: int = 60) -> bool:
    """Проверяет, не превысил ли пользователь лимит запросов"""
    now = datetime.now()
    user_requests[user_id] = [t for t in user_requests[user_id] if (now - t).seconds < period]
    if len(user_requests[user_id]) >= limit:
        return False
    user_requests[user_id].append(now)
    return True

# ========== 3. КАТЕГОРИИ ==========
CATEGORIES = {
    "electronics": "📱 Электроника",
    "clothing": "👕 Одежда и обувь",  # <-- ДОБАВИТЬ ЗАПЯТУЮ
    "home": "🏠 Дом и сад",
    "auto": "🚗 Авто и мото",
    "realty": "🏢 Недвижимость",
    "jobs": "💼 Работа",
    "services": "🔧 Услуги",
    "animals": "🐾 Животные",
    "hobby": "🎨 Хобби и спорт",
    "other": "📦 Другое"
}

# ========== 4. БАЗА ДАННЫХ ==========

# ---- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С ПРОСРОЧЕННЫМИ ОБЪЯВЛЕНИЯМИ ----

def delete_expired_ads_sync():
    """
    Синхронная версия для удаления просроченных объявлений (без уведомлений).
    Используется при инициализации БД.
    """
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    
    # Проверяем, существует ли таблица ads
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ads'")
    if not cur.fetchone():
        conn.close()
        return 0
    
    # Находим все просроченные объявления
    try:
        cur.execute('''
            SELECT id FROM ads 
            WHERE expires_at IS NOT NULL 
            AND expires_at < datetime('now')
            AND status = 'active'
        ''')
        
        expired_ads = cur.fetchall()
        
        if expired_ads:
            for ad in expired_ads:
                cur.execute('UPDATE ads SET status = "expired" WHERE id = ?', (ad[0],))
                print(f"🗑️ Помечено как expired: объявление #{ad[0]}")
        
        conn.commit()
    except Exception as e:
        print(f"⚠️ Ошибка при проверке просроченных объявлений: {e}")
    finally:
        conn.close()
    
    return len(expired_ads) if 'expired_ads' in dir() else 0


def init_db():
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    
    # ===== ВКЛЮЧАЕМ WAL-РЕЖИМ ДЛЯ НАДЁЖНОСТИ =====
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    
    print("✅ WAL-режим БД активирован")

    cur.execute('''
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            city TEXT NOT NULL,
            address TEXT,
            title TEXT NOT NULL,
            description TEXT,
            price TEXT,
            category TEXT,
            photos TEXT,
            status TEXT DEFAULT 'active',
            is_highlighted INTEGER DEFAULT 0,
            comments_enabled INTEGER DEFAULT 1,
            channel_message_id INTEGER,
            channel_message_ids TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            payment_id TEXT,
            update_count INTEGER DEFAULT 0
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_banned INTEGER DEFAULT 0,
            ads_count INTEGER DEFAULT 0,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute("PRAGMA table_info(ads)")
    columns = [col[1] for col in cur.fetchall()]

    if 'status' not in columns:
        cur.execute('ALTER TABLE ads ADD COLUMN status TEXT DEFAULT "active"')
    if 'is_highlighted' not in columns:
        cur.execute('ALTER TABLE ads ADD COLUMN is_highlighted INTEGER DEFAULT 0')
    if 'comments_enabled' not in columns:
        cur.execute('ALTER TABLE ads ADD COLUMN comments_enabled INTEGER DEFAULT 1')
    if 'channel_message_id' not in columns:
        cur.execute('ALTER TABLE ads ADD COLUMN channel_message_id INTEGER')
    if 'channel_message_ids' not in columns:
        cur.execute('ALTER TABLE ads ADD COLUMN channel_message_ids TEXT')
        print("✅ Добавлена колонка channel_message_ids")
    if 'update_count' not in columns:
        cur.execute('ALTER TABLE ads ADD COLUMN update_count INTEGER DEFAULT 0')
        print("✅ Добавлена колонка update_count")

    cur.execute("PRAGMA table_info(users)")
    user_cols = [col[1] for col in cur.fetchall()]

# ===== ДОБАВЛЯЕМ НЕДОСТАЮЩИЕ КОЛОНКИ В ТАБЛИЦУ ads =====
    cur.execute("PRAGMA table_info(ads)")
    ads_cols = [col[1] for col in cur.fetchall()]
    
    if 'updated_at' not in ads_cols:
        # SQLite не позволяет DEFAULT CURRENT_TIMESTAMP при ALTER TABLE
        cur.execute("ALTER TABLE ads ADD COLUMN updated_at TIMESTAMP")
        print("✅ Добавлена колонка updated_at")
    
    if 'last_bump_at' not in ads_cols:
        cur.execute("ALTER TABLE ads ADD COLUMN last_bump_at TIMESTAMP")
        print("✅ Добавлена колонка last_bump_at")
    
    # Обновляем существующие записи (устанавливаем created_at как updated_at)
    try:
        cur.execute("UPDATE ads SET updated_at = created_at WHERE updated_at IS NULL")
        print("✅ Обновлены значения updated_at")
    except:
        pass

    if 'is_banned' not in user_cols:
        cur.execute('ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0')
    if 'ads_count' not in user_cols:
        cur.execute('ALTER TABLE users ADD COLUMN ads_count INTEGER DEFAULT 0')

    try:
        cur.execute('CREATE INDEX IF NOT EXISTS idx_expires_at ON ads(expires_at)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_category ON ads(category)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_city ON ads(city)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_highlighted ON ads(is_highlighted)')
    except Exception as e:
        print(f"⚠️ Ошибка при создании индексов: {e}")

    # Удаляем физически просроченные объявления (без уведомлений, при инициализации)
    cur.execute('DELETE FROM ads WHERE expires_at IS NOT NULL AND expires_at < datetime("now")')

    # ===== ПОЛНОТЕКСТОВЫЙ ПОИСК FTS5 =====
    try:
        # Создаём виртуальную таблицу для поиска
        cur.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS ads_fts USING fts5(
                title, 
                description, 
                city,
                content=ads,
                content_rowid=id,
                tokenize='unicode61'
            )
        ''')
        
        # Заполняем FTS таблицу существующими данными
        cur.execute('''
            INSERT OR REPLACE INTO ads_fts(rowid, title, description, city)
            SELECT id, title, description, city FROM ads WHERE status = 'active'
        ''')
        
        # Триггеры для автоматического обновления FTS
        cur.execute('''
            CREATE TRIGGER IF NOT EXISTS ads_ai AFTER INSERT ON ads BEGIN
                INSERT INTO ads_fts(rowid, title, description, city)
                VALUES (new.id, new.title, new.description, new.city);
            END
        ''')
        
        cur.execute('''
            CREATE TRIGGER IF NOT EXISTS ads_ad AFTER DELETE ON ads BEGIN
                DELETE FROM ads_fts WHERE rowid = old.id;
            END
        ''')
        
        cur.execute('''
            CREATE TRIGGER IF NOT EXISTS ads_au AFTER UPDATE ON ads BEGIN
                UPDATE ads_fts 
                SET title = new.title, description = new.description, city = new.city
                WHERE rowid = new.id;
            END
        ''')
        
        print("✅ FTS5 полнотекстовый поиск активирован")
    except Exception as e:
        print(f"⚠️ FTS5 не поддерживается: {e}")
    
    conn.commit()
    conn.close()
    
    # Дополнительно: помечаем как expired просроченные (на случай, если были проблемы)
    deleted = delete_expired_ads_sync()
    if deleted > 0:
        print(f"🗑️ Помечено как expired: {deleted} объявлений")
    
    print("✅ База данных готова")

# Вызываем init_db() после её определения
init_db()

# ========== 5. FSM СОСТОЯНИЯ ==========
class AdForm(StatesGroup):
    waiting_for_phone = State()
    waiting_for_city = State()
    waiting_for_address = State()
    waiting_for_category = State()
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_photos = State()
    waiting_for_highlight = State()

class EditForm(StatesGroup):
    waiting_for_ad_id = State()
    waiting_for_field = State()
    waiting_for_new_value = State()
    waiting_for_category_edit = State()
    waiting_for_photos = State()
    adding_photos = State()

class SearchForm(StatesGroup):
    waiting_for_keywords = State()

class ChatForm(StatesGroup):
    waiting_for_message = State()

# ========== 6. КЛАВИАТУРЫ ==========

def get_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="📝 Создать объявление"),
        KeyboardButton(text="📋 Мои объявления")
    )
    builder.row(
        KeyboardButton(text="🔍 Поиск объявлений"),
        KeyboardButton(text="⭐ Платные услуги")
    )
    builder.row(
        KeyboardButton(text="✏️ Редактировать"),
        KeyboardButton(text="👤 Мой профиль")
    )
    builder.row(
        KeyboardButton(text="❓ Помощь"),
        KeyboardButton(text="📢 Канал с объявлениями")
    )
    return builder.as_markup(resize_keyboard=True)

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="❌ Отменить"))
    return builder.as_markup(resize_keyboard=True)

def get_phone_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Отправить номер", request_contact=True))
    builder.add(KeyboardButton(text="❌ Отменить"))
    return builder.as_markup(resize_keyboard=True)

def get_skip_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="⏭ Пропустить"))
    builder.add(KeyboardButton(text="❌ Отменить"))
    return builder.as_markup(resize_keyboard=True)

def get_photo_skip_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="✅ Готово, больше фото не будет"))
    builder.add(KeyboardButton(text="❌ Отменить"))
    return builder.as_markup(resize_keyboard=True)

def get_highlight_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Да, выделить цветом", callback_data="highlight_yes")],
        [InlineKeyboardButton(text="📄 Нет, обычное объявление", callback_data="highlight_no")]
    ])

def get_category_keyboard() -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for key, name in CATEGORIES.items():
        row.append(InlineKeyboardButton(text=name, callback_data=f"cat_{key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_creation")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_paid_services_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="✨ Выделить объявление цветом"),
        KeyboardButton(text="⬆️ Поднять объявление")  # НОВАЯ КНОПКА
    )
    builder.row(
        KeyboardButton(text="➕ Дополнительное объявление")
    )
    builder.row(KeyboardButton(text="🔙 Назад"))
    return builder.as_markup(resize_keyboard=True)

def get_my_ads_keyboard(ads: list) -> InlineKeyboardMarkup:
    keyboard = []
    if not ads:
        keyboard.append([InlineKeyboardButton(text="📭 Нет объявлений", callback_data="back_to_menu")])
    else:
        for ad in ads:
            if len(ad) > 8:
                is_highlighted = ad[8] if ad[8] else 0
                highlight_icon = "✨" if is_highlighted else "📌"
            else:
                highlight_icon = "📌"
            title = ad[1][:35] if len(ad) > 1 else "Без названия"
            ad_id = ad[0] if len(ad) > 0 else 0
            keyboard.append([InlineKeyboardButton(
                text=f"{highlight_icon} {title}...",
                callback_data=f"my_ad_{ad_id}"
            )])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_ad_edit_keyboard(ad_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Изменить заголовок", callback_data=f"edit_title_{ad_id}")],
        [InlineKeyboardButton(text="📝 Изменить описание", callback_data=f"edit_desc_{ad_id}")],
        [InlineKeyboardButton(text="💰 Изменить цену", callback_data=f"edit_price_{ad_id}")],
        [InlineKeyboardButton(text="📍 Изменить город", callback_data=f"edit_city_{ad_id}")],
        [InlineKeyboardButton(text="🏷️ Изменить категорию", callback_data=f"edit_cat_{ad_id}")],
        [InlineKeyboardButton(text="📸 Изменить фото", callback_data=f"edit_photos_{ad_id}")],
        [InlineKeyboardButton(text="💬 Вкл/Выкл комментарии", callback_data=f"toggle_comments_{ad_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить объявление", callback_data=f"delete_ad_{ad_id}")],
        [InlineKeyboardButton(text="✅ Опубликовать изменения", callback_data=f"publish_edits_{ad_id}")],
        [InlineKeyboardButton(text="❌ Отменить редактирование", callback_data=f"cancel_edits_{ad_id}")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_my_ads")]
    ])

def get_search_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(text="📋 Все категории", callback_data="search_cat_all")]]
    row = []
    for key, name in CATEGORIES.items():
        row.append(InlineKeyboardButton(text=name, callback_data=f"search_cat_{key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    if len(keyboard) == 1:
        keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_payment_keyboard(amount: int, purpose: str, ad_id: int = None) -> InlineKeyboardMarkup:
    payload = f"{purpose}_{ad_id}" if ad_id else purpose
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Оплатить {amount} Stars", pay=True)],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_payment")]
    ])

def get_channel_ad_keyboard(ad_id: int, seller_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Связаться с продавцом", callback_data=f"contact_seller_{ad_id}_{seller_id}")],
        [InlineKeyboardButton(text="🚫 Пожаловаться", callback_data=f"complaint_{ad_id}")]
    ])

def get_photos_edit_keyboard(ad_id: int, photos: list) -> InlineKeyboardMarkup:
    keyboard = []
    if photos:
        for i, _ in enumerate(photos[:5]):
            keyboard.append([InlineKeyboardButton(
                text=f"❌ Удалить фото {i+1}", 
                callback_data=f"del_photo_{ad_id}_{i}"
            )])
    else:
        keyboard.append([InlineKeyboardButton(text="📭 Нет фотографий", callback_data="no_action")])
    keyboard.append([InlineKeyboardButton(text="➕ Добавить фото", callback_data=f"add_photo_{ad_id}")])
    keyboard.append([InlineKeyboardButton(text="✅ Завершить редактирование фото", callback_data=f"finish_photos_{ad_id}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к редактированию", callback_data=f"back_to_edit_{ad_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_add_photo_keyboard(ad_id: int) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="✅ Закончить добавление фото"))
    builder.add(KeyboardButton(text="❌ Отменить"))
    return builder.as_markup(resize_keyboard=True)

# ========== 7. ФУНКЦИИ БД ==========

def save_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, ads_count)
        VALUES (?, ?, ?, ?, 0)
    ''', (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()

def is_user_banned(user_id: int) -> bool:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
    result = cur.fetchone()
    conn.close()
    return result[0] == 1 if result else False

def ban_user(user_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def can_post_ads(user_id: int) -> tuple:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('SELECT ads_count FROM users WHERE user_id = ?', (user_id,))
    result = cur.fetchone()
    extra_slots = result[0] if result else 0
    ads_limit = 1 + extra_slots  # Бесплатно 1 объявление, дальше платно
    cur.execute('SELECT COUNT(*) FROM ads WHERE user_id = ? AND status = "active" AND expires_at > datetime("now")', (user_id,))
    active_ads = cur.fetchone()[0]
    conn.close()
    return active_ads < ads_limit, active_ads, ads_limit

def increment_ads_limit(user_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE users SET ads_count = ads_count + 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def save_ad(user_id: int, phone: str, city: str, address: str, category: str,
            title: str, description: str, price: str, photos: str, is_highlighted: int = 0) -> int:
    """Сохраняет объявление в БД с использованием транзакции"""
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    try:
        cur.execute('BEGIN TRANSACTION')
        
        expires_at = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        cur.execute('''
            INSERT INTO ads (user_id, phone, city, address, category, title, description, price, photos, is_highlighted, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, phone, city, address, category, title, description, price, photos, is_highlighted, expires_at, created_at))
        ad_id = cur.lastrowid
        cur.execute('COMMIT')
        print(f"✅ Объявление сохранено в БД: id={ad_id}, created_at={created_at}")
        return ad_id
    except Exception as e:
        cur.execute('ROLLBACK')
        print(f"❌ Ошибка при сохранении объявления: {e}")
        raise e
    finally:
        conn.close()

def update_ad_field(ad_id: int, field: str, value: str):
    """Безопасное обновление поля объявления (защита от SQL-инъекций)"""
    allowed_fields = ['title', 'description', 'price', 'city', 'category', 'photos']
    if field not in allowed_fields:
        raise ValueError(f"Недопустимое поле: {field}")
    
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    # Используем безопасный запрос с проверкой поля через allowed_fields
    cur.execute(f'UPDATE ads SET "{field}" = ? WHERE id = ?', (value, ad_id))
    conn.commit()
    conn.close()

def get_ad_photos(ad_id: int) -> list:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('SELECT photos FROM ads WHERE id = ?', (ad_id,))
    result = cur.fetchone()
    conn.close()
    if result and result[0]:
        return result[0].split(',')
    return []

def update_ad_photos(ad_id: int, photos_list: list) -> str:
    photos_str = ','.join(photos_list)
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE ads SET photos = ? WHERE id = ?', (photos_str, ad_id))
    conn.commit()
    conn.close()
    return photos_str

def increment_update_count(ad_id: int) -> int:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE ads SET update_count = update_count + 1 WHERE id = ?', (ad_id,))
    conn.commit()
    cur.execute('SELECT update_count FROM ads WHERE id = ?', (ad_id,))
    result = cur.fetchone()
    conn.close()
    return result[0] if result else 0

def toggle_comments(ad_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE ads SET comments_enabled = 1 - comments_enabled WHERE id = ?', (ad_id,))
    conn.commit()
    conn.close()

def highlight_ad(ad_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE ads SET is_highlighted = 1 WHERE id = ?', (ad_id,))
    conn.commit()
    conn.close()

def bump_ad(ad_id: int):
    """Обновляет время поднятия объявления (для сортировки в топе)"""
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('UPDATE ads SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (ad_id,))
    conn.commit()
    conn.close()
    print(f"⬆️ Объявление #{ad_id} поднято в топ")

def delete_ad(ad_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('DELETE FROM ads WHERE id = ?', (ad_id,))
    conn.commit()
    conn.close()

def get_user_ads(user_id: int) -> list:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('''
        SELECT id, title, description, price, city, category, status, created_at, 
               COALESCE(is_highlighted, 0) as is_highlighted, 
               COALESCE(comments_enabled, 1) as comments_enabled
        FROM ads WHERE user_id = ? ORDER BY is_highlighted DESC, created_at DESC
    ''', (user_id,))
    ads = cur.fetchall()
    conn.close()
    return ads

def search_ads_by_keywords(keywords: str, limit: int = 10, offset: int = 0) -> tuple:
    """
    Простой поиск объявлений по ключевым словам в заголовке и описании
    Поддерживает поиск по словам и начальным слогам
    """
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    
    # Разбиваем поисковый запрос на отдельные слова
    words = keywords.strip().split()
    
    if not words:
        conn.close()
        return [], 0
    
    # Формируем условия поиска для каждого слова
    conditions = []
    params = []
    
    for word in words:
        # Поиск по началу слова (например "тел" найдёт "телефон")
        conditions.append("(title LIKE ? OR description LIKE ?)")
        params.append(f'{word}%')
        params.append(f'{word}%')
        # Также ищем как часть слова (для полноты)
        conditions.append("(title LIKE ? OR description LIKE ?)")
        params.append(f'%{word}%')
        params.append(f'%{word}%')
    
    # Объединяем условия через OR
    where_clause = " OR ".join(conditions)
    
    # Базовые условия (только активные объявления)
    base_condition = "status = 'active' AND expires_at > datetime('now')"
    
    full_where = f"{base_condition} AND ({where_clause})"
    
    # Подсчёт общего количества
    cur.execute(f'SELECT COUNT(*) FROM ads WHERE {full_where}', params)
    total = cur.fetchone()[0]
    
    # Получение результатов (сортировка по дате создания - самые свежие первые)
    cur.execute(f'''
        SELECT id, title, description, price, city, created_at, is_highlighted, channel_message_ids
        FROM ads 
        WHERE {full_where}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    ''', params + [limit, offset])
    
    ads = cur.fetchall()
    conn.close()
    return ads, total

def get_ad_by_id(ad_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('SELECT * FROM ads WHERE id = ?', (ad_id,))
    ad = cur.fetchone()
    
    if ad and (len(ad) <= 14 or not ad[14]):
        current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur.execute('UPDATE ads SET created_at = ? WHERE id = ?', (current_date, ad_id))
        conn.commit()
        cur.execute('SELECT * FROM ads WHERE id = ?', (ad_id,))
        ad = cur.fetchone()
        print(f"✅ Обновлена дата для объявления {ad_id}: {current_date}")
    
    conn.close()
    return ad

def add_chat(ad_id: int, buyer_id: int, seller_id: int):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO chats (ad_id, buyer_id, seller_id) VALUES (?, ?, ?)', (ad_id, buyer_id, seller_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id: int) -> dict:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('SELECT ads_count FROM users WHERE user_id = ?', (user_id,))
    extra_ads = cur.fetchone()
    cur.execute('SELECT COUNT(*) FROM ads WHERE user_id = ? AND status = "active" AND expires_at > datetime("now")', (user_id,))
    active_ads = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM ads WHERE user_id = ? AND is_highlighted = 1', (user_id,))
    highlighted_ads = cur.fetchone()[0]
    conn.close()
    return {
        "extra_ads_limit": extra_ads[0] if extra_ads else 0,
        "active_ads": active_ads,
        "highlighted_ads": highlighted_ads
    }

def get_admin_stats() -> dict:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM ads WHERE status = "active" AND expires_at > datetime("now")')
    active_ads = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM users WHERE is_banned = 0')
    active_users = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM users WHERE is_banned = 1')
    banned_users = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM comments')
    total_comments = cur.fetchone()[0]
    conn.close()
    return {
        "active_ads": active_ads,
        "active_users": active_users,
        "banned_users": banned_users,
        "total_comments": total_comments
    }

def validate_price(price: str) -> bool:
    if price is None:
        return True
    return bool(re.match(r'^[\d\s.,₽$€£¥+-]*$', price))

# ========== 8. ПУБЛИКАЦИЯ В КАНАЛ ==========

def set_channel_message_ids(ad_id: int, message_ids: list):
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute(
        'UPDATE ads SET channel_message_ids = ? WHERE id = ?',
        (json.dumps(message_ids), ad_id)
    )
    conn.commit()
    conn.close()

def get_channel_message_ids(ad_id: int) -> list:
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute('SELECT channel_message_ids FROM ads WHERE id = ?', (ad_id,))
    row = cur.fetchone()
    conn.close()

    if not row or not row[0]:
        return []

    try:
        ids = json.loads(row[0])
        if isinstance(ids, list):
            return ids
    except:
        pass

    return []

async def get_ad_channel_link(ad_id: int) -> str:
    ad = get_ad_by_id(ad_id)
    
    if not ad or not CHANNEL_ID:
        return None
    
    channel_message_ids = get_channel_message_ids(ad_id)
    if not channel_message_ids:
        channel_message_id = ad[13] if len(ad) > 13 else None
        if not channel_message_id:
            return None
        message_id = channel_message_id
    else:
        message_id = channel_message_ids[0]
    
    channel_id_str = str(CHANNEL_ID)
    if channel_id_str.startswith('-100'):
        channel_id_str = channel_id_str[4:]
    
    return f"https://t.me/c/{channel_id_str}/{message_id}"

async def delete_ad_messages_from_channel(ad_id: int):
    if not CHANNEL_ID:
        return

    message_ids = get_channel_message_ids(ad_id)
    if not message_ids:
        return

    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id)
            print(f"🗑️ Удалено сообщение из канала: {message_id}")
        except Exception as e:
            print(f"⚠️ Не удалось удалить сообщение {message_id}: {e}")

    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    cur.execute(
        'UPDATE ads SET channel_message_ids = NULL WHERE id = ?',
        (ad_id,)
    )
    conn.commit()
    conn.close()

async def publish_ad_to_channel(ad_id: int):
    ad = get_ad_by_id(ad_id)
    
    if not ad or not CHANNEL_ID:
        return False
    
# ===== ДОБАВИТЬ ЭТУ ПРОВЕРКУ =====
    try:
        # Проверяем, может ли бот отправлять сообщения
        me = await bot.get_me()
        my_member = await bot.get_chat_member(CHANNEL_ID, me.id)
        
        # Для aiogram 3.x используем статус вместо can_send_messages
        if my_member.status not in ['administrator', 'creator']:
            print(f"❌ Бот не является администратором канала {CHANNEL_ID}")
            logging.error(f"Бот не является администратором канала {CHANNEL_ID}")
            return False
        
        # Дополнительная проверка прав для администратора
        if my_member.status == 'administrator':
            # Проверяем, есть ли право отправлять сообщения
            if hasattr(my_member, 'can_post_messages'):
                if not my_member.can_post_messages:
                    print(f"❌ Бот не может публиковать сообщения в канале {CHANNEL_ID}")
                    logging.error(f"Бот не может публиковать сообщения в канале {CHANNEL_ID}")
                    return False
    except Exception as e:
        print(f"❌ Ошибка доступа к каналу: {e}")
        logging.error(f"Ошибка доступа к каналу: {e}")
        return False
    
    highlight_icon = "✨ " if ad_dict['is_highlighted'] else ""
    
    created_raw = ad_dict['created_at']
    if created_raw and str(created_raw) != "None":
        try:
            if isinstance(created_raw, str):
                dt = datetime.strptime(created_raw, '%Y-%m-%d %H:%M:%S')
            else:
                dt = created_raw
            formatted_date = dt.strftime('%d.%m.%Y в %H:%M')
        except:
            formatted_date = datetime.now().strftime('%d.%m.%Y в %H:%M')
    else:
        formatted_date = datetime.now().strftime('%d.%m.%Y в %H:%M')
        conn = sqlite3.connect('ads.db')
        cur = conn.cursor()
        cur.execute('UPDATE ads SET created_at = ? WHERE id = ?', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), ad_id))
        conn.commit()
        conn.close()
    
    # Вычисляем дату удаления
    from datetime import timedelta
    expiry_date = (datetime.now() + timedelta(days=30)).strftime('%d.%m.%Y')
    
    caption = (
        f"{highlight_icon}<b>{ad_dict['title']}</b>\n\n"
        f"🏷️ <b>Категория:</b> {CATEGORIES.get(ad_dict['category'], 'Другое')}\n"
        f"💰 <b>Цена:</b> {ad_dict['price'] if ad_dict['price'] else 'Договорная'}\n"
        f"📍 <b>Город:</b> {ad_dict['city']}\n"
        f"🏠 <b>Адрес:</b> {ad_dict['address'] if ad_dict['address'] else 'Не указан'}\n"
        f"🕒 <b>Размещено:</b> {formatted_date}\n"
        f"⏰ <b>Удаляется:</b> {expiry_date}\n\n"
        f"📝 <b>Описание:</b>\n{ad_dict['description'][:500]}\n\n"
        f"📞 <b>Телефон:</b> {ad_dict['phone']}"
    )
    
    photo_ids = ad_dict['photos'].split(',') if ad_dict['photos'] else []
    
    try:
        if photo_ids:
            # Проверяем валидность file_id перед отправкой
            valid_photos = []
            for photo_id in photo_ids:
                try:
                    await bot.get_file(photo_id)
                    valid_photos.append(photo_id)
                except Exception as e:
                    print(f"⚠️ Невалидный file_id: {photo_id}, пропускаем")
            
            if valid_photos:
                media_group = []
                for i, photo_id in enumerate(valid_photos):
                    if i == 0:
                        media_group.append(
                            InputMediaPhoto(media=photo_id, caption=caption, parse_mode="HTML")
                        )
                    else:
                        media_group.append(InputMediaPhoto(media=photo_id))
                
                messages = await bot.send_media_group(chat_id=CHANNEL_ID, media=media_group)
                if messages:
                    message_ids = [m.message_id for m in messages]
                    set_channel_message_ids(ad_id, message_ids)
                    print(f"✅ Альбом из {len(valid_photos)} фото отправлен в канал")
            else:
                raise Exception("Нет валидных фотографий для отправки")
        else:
            message = await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                reply_markup=get_channel_ad_keyboard(ad_id, ad_dict['user_id'])
            )
            set_channel_message_ids(ad_id, [message.message_id])
            print("✅ Текст отправлен в канал")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка публикации: {e}")
        logging.error(f"Ошибка публикации: {e}")
        return False

# ========== 8.1 АВТОМАТИЧЕСКОЕ УДАЛЕНИЕ ПРОСРОЧЕННЫХ ОБЪЯВЛЕНИЙ ==========

async def delete_expired_ads_from_channel():
    """
    Асинхронное удаление просроченных объявлений из канала и БД.
    Возвращает количество удалённых объявлений.
    """
    conn = sqlite3.connect('ads.db')
    cur = conn.cursor()
    
    # Находим все просроченные активные объявления
    cur.execute('''
        SELECT id, channel_message_ids, user_id, title 
        FROM ads 
        WHERE expires_at IS NOT NULL 
        AND expires_at < datetime('now')
        AND status = 'active'
    ''')
    
    expired_ads = cur.fetchall()
    
    if not expired_ads:
        conn.close()
        return 0
    
    deleted_count = 0
    for ad in expired_ads:
        ad_id = ad[0]
        user_id = ad[2]
        ad_title = ad[3] if len(ad) > 3 else "Объявление"
        
        # 1. Удаляем сообщения из канала
        await delete_ad_messages_from_channel(ad_id)
        
        # 2. Обновляем статус в БД
        cur.execute('UPDATE ads SET status = "expired" WHERE id = ?', (ad_id,))
        
        # 3. Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"📅 <b>Объявление истекло</b>\n\n"
                f"\"{ad_title[:50]}\"\n\n"
                f"⏰ С момента публикации прошло 30 дней.\n"
                f"Объявление автоматически удалено из канала.\n\n"
                f"Вы можете создать новое: /new",
                parse_mode="HTML"
            )
            print(f"📧 Уведомление отправлено пользователю {user_id} об истечении объявления #{ad_id}")
        except Exception as e:
            print(f"⚠️ Не удалось уведомить пользователя {user_id}: {e}")
        
        deleted_count += 1
        print(f"🗑️ Удалено просроченное объявление #{ad_id}")
    
    conn.commit()
    conn.close()
    return deleted_count

# ========== 9. ОБРАБОТЧИКИ КОМАНД ==========

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите минуту.")
        return
    
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    # Ссылка на картинку с Pinterest (ЗАМЕНИ НА СВОЮ)
    photo_url = "https://i.pinimg.com/736x/85/69/d3/8569d3b93adfb8bc9ab53a94ff35193c.jpg"
    
    caption = (
        "✨ <b>Привет, Я MILTONE и я помогу продать и найти что угодно!</b>\n\n"
        "• Заполни форму и объявление появится в канале\n"
        "• Добавляй до 5 фото\n"
        "• Редактируй в любой момент\n"
        "• Выделяй цветом за 250⭐\n"
        "• Поднимай в топ за 250⭐\n\n"
        "🚀 <b>ПЛАТНЫЕ УСЛУГИ:</b>\n"
        f"   ✨ Выделение — {HIGHLIGHT_PRICE} ⭐\n"
        f"   ⬆️ Поднятие — {BUMP_PRICE} ⭐\n"
        f"   ➕ Доп. место — {EXTRA_AD_PRICE} ⭐\n\n"
        "👇 <b>НАЖМИ «📝 СОЗДАТЬ ОБЪЯВЛЕНИЕ»</b>"
    )
    
    await message.answer_photo(
        photo=photo_url,
        caption=caption,
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("new"))
async def cmd_new(message: types.Message, state: FSMContext):
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите минуту.")
        return
    
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    can_post, active_ads, ads_limit = can_post_ads(message.from_user.id)
    if not can_post:
        await message.answer(f"❌ Лимит: {ads_limit}. Купите доп. место в '⭐ Платные услуги'.")
        return
    await state.clear()
    await message.answer("📝 Укажите номер телефона:", reply_markup=get_phone_keyboard())
    await state.set_state(AdForm.waiting_for_phone)

@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите минуту.")
        return
    
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    ads = get_user_ads(message.from_user.id)
    if not ads:
        await message.answer("📭 У вас нет объявлений.", reply_markup=get_main_keyboard())
        return
    
    # Пагинация: показываем по 5 объявлений
    page = 0
    per_page = 5
    total_pages = (len(ads) + per_page - 1) // per_page
    
    text = f"📋 <b>Ваши объявления</b> (страница {page + 1}/{total_pages if total_pages > 0 else 1}):\n\n"
    
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(ads))
    
    for i, ad in enumerate(ads[start_idx:end_idx], start_idx + 1):
        status = ad[6] if len(ad) > 6 else 'active'
        status_icon = "✅" if status == 'active' else "⛔"
        is_highlighted = ad[8] if len(ad) > 8 else 0
        highlight_icon = "✨" if is_highlighted else "📌"
        title = ad[1][:35] if len(ad) > 1 else "Без названия"
        price = ad[3] if len(ad) > 3 and ad[3] else "Договорная"
        city = ad[4] if len(ad) > 4 else "Не указан"
        text += f"{i}. {highlight_icon} <b>{title}</b> {status_icon}\n   💰 {price} | 📍 {city}\n\n"
    
    # Клавиатура пагинации
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    if page > 0:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"my_ads_page_{page-1}")])
    if page + 1 < total_pages:
        if keyboard.inline_keyboard:
            keyboard.inline_keyboard[0].append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"my_ads_page_{page+1}"))
        else:
            keyboard.inline_keyboard.append([InlineKeyboardButton(text="Вперед ▶", callback_data=f"my_ads_page_{page+1}")])
    
    if not keyboard.inline_keyboard:
        keyboard = get_my_ads_keyboard(ads)
    
    await message.answer(text[:4000], parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("my_ads_page_"))
async def my_ads_pagination(callback: types.CallbackQuery):
    """Пагинация для списка объявлений"""
    page = int(callback.data.split("_")[3])
    
    ads = get_user_ads(callback.from_user.id)
    per_page = 5
    total_pages = (len(ads) + per_page - 1) // per_page
    
    text = f"📋 <b>Ваши объявления</b> (страница {page + 1}/{total_pages if total_pages > 0 else 1}):\n\n"
    
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(ads))
    
    for i, ad in enumerate(ads[start_idx:end_idx], start_idx + 1):
        status = ad[6] if len(ad) > 6 else 'active'
        status_icon = "✅" if status == 'active' else "⛔"
        is_highlighted = ad[8] if len(ad) > 8 else 0
        highlight_icon = "✨" if is_highlighted else "📌"
        title = ad[1][:35] if len(ad) > 1 else "Без названия"
        price = ad[3] if len(ad) > 3 and ad[3] else "Договорная"
        city = ad[4] if len(ad) > 4 else "Не указан"
        text += f"{i}. {highlight_icon} <b>{title}</b> {status_icon}\n   💰 {price} | 📍 {city}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    if page > 0:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"my_ads_page_{page-1}")])
    if page + 1 < total_pages:
        if keyboard.inline_keyboard:
            keyboard.inline_keyboard[0].append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"my_ads_page_{page+1}"))
        else:
            keyboard.inline_keyboard.append([InlineKeyboardButton(text="Вперед ▶", callback_data=f"my_ads_page_{page+1}")])
    
    if not keyboard.inline_keyboard:
        keyboard = get_my_ads_keyboard(ads)
    
    await callback.message.edit_text(text[:4000], parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@dp.message(Command("search"))
@dp.message(F.text == "🔍 Поиск объявлений")
async def cmd_search(message: types.Message, state: FSMContext):
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите минуту.")
        return
    
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    await state.clear()
    await message.answer(
        "🔍 <b>Поиск объявлений</b>\n\n"
        "Введите ключевое слово или несколько слов для поиска.\n"
        "🔹 Поддерживается поиск по началу слова (например: «тел» найдёт «телефон»)\n"
        "🔹 Поиск происходит в заголовках и описаниях\n\n"
        "👇 Введите ваш запрос:",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(SearchForm.waiting_for_keywords)

@dp.message(SearchForm.waiting_for_keywords)
async def process_search_keywords(message: types.Message, state: FSMContext):
    """Обработчик ввода ключевых слов от пользователя"""
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("🔍 Поиск отменён.", reply_markup=get_main_keyboard())
        return
    
    keywords = message.text.strip()
    if len(keywords) < 2:
        await message.answer(
            "❌ Введите хотя бы 2 символа для поиска.\n"
            "Попробуйте снова или нажмите «❌ Отменить»:",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    # Сохраняем запрос в состояние
    await state.update_data(search_keywords=keywords, search_page=0)
    
    # Выполняем поиск (вызов вспомогательной функции)
    await perform_search(message, state, keywords, page=0)

async def perform_search(message: types.Message, state: FSMContext, keywords: str, page: int = 0):
    """Выполняет поиск и отображает результаты (вспомогательная функция)"""
    limit = 10
    offset = page * limit
    
    ads, total = search_ads_by_keywords(keywords, limit=limit, offset=offset)
    
    if not ads:
        await message.answer(
            "🔍 <b>Ничего не найдено</b>\n\n"
            "Попробуйте:\n"
            "• Изменить ключевые слова\n"
            "• Использовать более короткие слова\n"
            "• Проверить правильность написания",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    total_pages = (total + limit - 1) // limit
    text = f"🔍 <b>Результаты поиска</b> «{keywords}»\n"
    text += f"📊 <b>Найдено:</b> {total} объявлений\n"
    text += f"📄 <b>Страница:</b> {page + 1} из {total_pages if total_pages > 0 else 1}\n\n"
    
    for i, ad in enumerate(ads, start=offset + 1):
        ad_id = ad[0]
        title = ad[1][:50] if ad[1] else "Без названия"
        price = ad[3] if ad[3] else "Договорная"
        city = ad[4] if ad[4] else "Не указан"
        created = ad[5][:10] if ad[5] else "Не указано"
        highlight = "✨ " if ad[6] else ""
        
        channel_link = await get_ad_channel_link(ad_id)
        link_text = "🔗 Перейти" if channel_link else "❌ Ссылка недоступна"
        link_url = channel_link if channel_link else "#"
        
        text += (
            f"{i}. {highlight}<b>{title}</b>\n"
            f"   💰 {price} | 📍 {city} | 📅 {created}\n"
            f"   <a href='{link_url}'>{link_text}</a>\n\n"
        )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"search_page_{page-1}"))
    if offset + limit < total:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"search_page_{page+1}"))
    
    if nav_buttons:
        keyboard.inline_keyboard.append(nav_buttons)
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🔄 Новый поиск", callback_data="search_new")])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main_menu")])
    
    await message.answer(text[:4000], parse_mode="HTML", reply_markup=keyboard)
    await state.update_data(search_keywords=keywords, search_page=page)

# ===== ЗАТЕМ ИДУТ ЭТИ ФУНКЦИИ =====

@dp.callback_query(F.data.startswith("search_page_"))
async def search_pagination(callback: types.CallbackQuery, state: FSMContext):
    """Обработчик пагинации результатов поиска"""
    page = int(callback.data.split("_")[2])
    
    data = await state.get_data()
    keywords = data.get('search_keywords')
    
    if not keywords:
        await callback.answer("Поисковый запрос не найден. Начните новый поиск.", show_alert=True)
        await callback.message.delete()
        await cmd_search(callback.message, state)
        await callback.answer()
        return
    
    await callback.message.delete()
    await perform_search(callback.message, state, keywords, page)
    await callback.answer()


@dp.callback_query(F.data == "search_new")
async def search_new(callback: types.CallbackQuery, state: FSMContext):
    """Начать новый поиск"""
    await callback.message.delete()
    await cmd_search(callback.message, state)
    await callback.answer()

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "❓ <b>Помощь</b>\n\n"
        "/new - создать объявление\n"
        "/my - мои объявления\n"
        "/search - поиск\n\n"
        f"💰 Выделение: {HIGHLIGHT_PRICE} Stars\n"
        f"💰 Доп. объявление: {EXTRA_AD_PRICE} Stars",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("rules"))
async def rules_command(message: types.Message):
    await message.answer(
        "📜 <b>Правила сервиса</b>\n\n"
        "1️⃣ <b>Запрещено:</b>\n"
        "   • Размещение запрещённых товаров\n"
        "   • Спам и мошенничество\n"
        "   • Оскорбления в комментариях\n\n"
        "2️⃣ <b>Объявления удаляются:</b>\n"
        "   • Через 30 дней автоматически\n"
        "   • По жалобе пользователей\n"
        "   • При нарушении правил\n\n"
        "3️⃣ <b>Возврат Stars:</b>\n"
        "   • При технических сбоях\n"
        "   • Обращайтесь через /feedback\n\n"
        "⚠️ <b>Бета-тест:</b> возможны доработки!\n"
        "🙏 Спасибо за понимание!",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("balance"))
async def get_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return
    
    try:
        balance = await bot.get_my_star_balance()
        await message.answer(
            f"⭐ <b>Баланс Telegram Stars</b>\n\n"
            f"💰 Текущий баланс: <b>{balance} Stars</b>\n\n"
            f"📌 <i>Доход от платных услуг бота</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при получении баланса: {e}")

# ========== 10. FSM СОЗДАНИЯ ==========

@dp.message(AdForm.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите минуту.")
        return
    
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_keyboard())
        return
    phone = None
    if message.contact:
        phone = message.contact.phone_number
    elif message.text and message.text.replace('+', '').replace('-', '').replace(' ', '').isdigit():
        phone = message.text
    if not phone:
        await message.answer("❌ Отправьте номер через кнопку.", reply_markup=get_phone_keyboard())
        return
    await state.update_data(phone=phone)
    await message.answer("📍 Город:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdForm.waiting_for_city)

@dp.message(AdForm.waiting_for_city)
async def process_city(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_keyboard())
        return
    if len(message.text.strip()) < 2:
        await message.answer("❌ Введите корректный город.")
        return
    await state.update_data(city=message.text.strip())
    await message.answer("📍 Адрес (по желанию):", reply_markup=get_skip_keyboard())
    await state.set_state(AdForm.waiting_for_address)

@dp.message(AdForm.waiting_for_address)
async def process_address(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_keyboard())
        return
    address = None if message.text == "⏭ Пропустить" else message.text.strip()
    await state.update_data(address=address)
    await message.answer("🏷️ Выберите категорию:", reply_markup=get_category_keyboard())
    await state.set_state(AdForm.waiting_for_category)

@dp.callback_query(AdForm.waiting_for_category, F.data.startswith("cat_"))
async def process_category(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "cancel_creation":
        await state.clear()
        await callback.message.edit_text("Отменено.")
        await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    category_key = callback.data.replace("cat_", "")
    await state.update_data(category=category_key)
    await callback.message.edit_text(f"✅ {CATEGORIES[category_key]}\n\n📌 Введите заголовок:")
    await state.set_state(AdForm.waiting_for_title)
    await callback.answer()

@dp.message(AdForm.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_keyboard())
        return
    if len(message.text.strip()) < 5:
        await message.answer("❌ Минимум 5 символов.")
        return
    await state.update_data(title=message.text.strip())
    await message.answer("📝 Описание:", reply_markup=get_cancel_keyboard())
    await state.set_state(AdForm.waiting_for_description)

@dp.message(AdForm.waiting_for_description)
async def process_description(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_keyboard())
        return
    if len(message.text.strip()) < 10:
        await message.answer("❌ Минимум 10 символов.")
        return
    await state.update_data(description=message.text.strip())
    await message.answer("💰 Цена (по желанию):", reply_markup=get_skip_keyboard())
    await state.set_state(AdForm.waiting_for_price)

@dp.message(AdForm.waiting_for_price)
async def process_price(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_keyboard())
        return
    price = None if message.text == "⏭ Пропустить" else message.text.strip()
    if price and not validate_price(price):
        await message.answer("❌ Некорректный формат цены. Используйте цифры и знаки валют.")
        return
    await state.update_data(price=price)
    await message.answer("📸 Фото (до 5 шт.):\nОтправляйте по одному, затем нажмите '✅ Готово'.", reply_markup=get_photo_skip_keyboard())
    await state.update_data(photos=[])
    await state.set_state(AdForm.waiting_for_photos)

@dp.message(AdForm.waiting_for_photos, F.photo)
async def process_photos(message: types.Message, state: FSMContext):
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите минуту.")
        return
    
    data = await state.get_data()
    photos = data.get('photos', [])
    if len(photos) >= 5:
        await message.answer("❌ Максимум 5 фото.")
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(f"✅ Фото добавлено! Осталось: {5 - len(photos)}")

@dp.message(AdForm.waiting_for_photos)
async def process_photos_done(message: types.Message, state: FSMContext):
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_keyboard())
        return
    if message.text == "✅ Готово, больше фото не будет":
        data = await state.get_data()
        photos_str = ','.join(data.get('photos', []))
        await state.update_data(photos_str=photos_str)
        preview = (
            f"📝 <b>Предпросмотр</b>\n\n"
            f"📞 {data['phone']}\n📍 {data['city']}\n🏷️ {CATEGORIES.get(data['category'], 'Другое')}\n"
            f"📌 {data['title']}\n💰 {data.get('price', 'Договорная')}\n📸 {len(data.get('photos', []))}/5\n\n"
            f"<b>Выделить? {HIGHLIGHT_PRICE} Stars</b>"
        )
        await message.answer(preview, parse_mode="HTML", reply_markup=get_highlight_choice_keyboard())
        await state.set_state(AdForm.waiting_for_highlight)

@dp.callback_query(AdForm.waiting_for_highlight, F.data.in_(["highlight_yes", "highlight_no"]))
async def process_highlight_choice(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if callback.data == "highlight_yes":
        await callback.message.edit_text(f"✨ Выделение: {HIGHLIGHT_PRICE} Stars\n\nНажмите для оплаты:")
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title="✨ Выделение объявления",
            description=f"Выделение: {data['title'][:50]}",
            payload=f"highlight_new_{callback.from_user.id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Выделение", amount=HIGHLIGHT_PRICE)],
            reply_markup=get_payment_keyboard(HIGHLIGHT_PRICE, "highlight_new")
        )
        await state.update_data(is_highlighted=1)
    else:
        ad_id = save_ad(
            user_id=callback.from_user.id,
            phone=data['phone'], city=data['city'], address=data.get('address'),
            category=data['category'], title=data['title'], description=data['description'],
            price=data.get('price'), photos=data.get('photos_str', ''), is_highlighted=0
        )
        result = await publish_ad_to_channel(ad_id)
        if result:
            await callback.message.edit_text("✅ Объявление опубликовано в канале!")
        else:
            await callback.message.edit_text("⚠️ Объявление сохранено, но не опубликовано в канале.")
        await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
        await state.clear()
    await callback.answer()

# ========== 12. РЕДАКТИРОВАНИЕ ОБЪЯВЛЕНИЙ ==========

# Временное хранилище для изменений при редактировании
edit_drafts = {}

def save_edit_draft(ad_id: int, field: str, new_value: str):
    if ad_id not in edit_drafts:
        # ← ИСПРАВЛЕНИЕ: Сохраняем существующие фото при создании черновика
        current_photos = get_ad_photos(ad_id)
        edit_drafts[ad_id] = {'changes': {}, 'photos': current_photos, 'timestamp': datetime.now()}
    edit_drafts[ad_id]['changes'][field] = new_value
    edit_drafts[ad_id]['timestamp'] = datetime.now()

def get_edit_draft(ad_id: int) -> dict:
    return edit_drafts.get(ad_id, {'changes': {}, 'photos': None})

def clear_edit_draft(ad_id: int):
    if ad_id in edit_drafts:
        del edit_drafts[ad_id]

def save_photos_draft(ad_id: int, photos: list):
    """Сохраняет черновик фото"""
    if ad_id not in edit_drafts:
        edit_drafts[ad_id] = {'changes': {}, 'photos': None, 'timestamp': datetime.now()}
    edit_drafts[ad_id]['photos'] = photos.copy()
    edit_drafts[ad_id]['timestamp'] = datetime.now()

def get_photos_draft(ad_id: int) -> list:
    draft = edit_drafts.get(ad_id, {})
    if draft.get('photos') is not None:
        return draft['photos'].copy()
    return get_ad_photos(ad_id)

def cleanup_old_drafts():
    """Очищает черновики старше 1 часа"""
    now = datetime.now()
    to_delete = []
    for ad_id, draft in edit_drafts.items():
        draft_time = draft.get('timestamp')
        if draft_time and (now - draft_time).seconds > 3600:
            to_delete.append(ad_id)
    for ad_id in to_delete:
        del edit_drafts[ad_id]
        print(f"🗑️ Удалён старый черновик для объявления {ad_id}")

@dp.message(F.text == "✏️ Редактировать")
async def edit_start(message: types.Message, state: FSMContext):
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    ads = get_user_ads(message.from_user.id)
    if not ads:
        await message.answer("Нет объявлений для редактирования.", reply_markup=get_main_keyboard())
        return
    await state.clear()
    await message.answer("📋 Выберите объявление для редактирования:", reply_markup=get_my_ads_keyboard(ads))
    await state.set_state(EditForm.waiting_for_ad_id)

@dp.callback_query(EditForm.waiting_for_ad_id, F.data.startswith("my_ad_"))
async def select_ad_for_edit(callback: types.CallbackQuery, state: FSMContext):
    ad_id = int(callback.data.replace("my_ad_", ""))
    
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    clear_edit_draft(ad_id)
    
    await state.update_data(edit_ad_id=ad_id)
    await callback.message.edit_text(
        "✏️ <b>Редактирование объявления</b>\n\n"
        "Выберите, что хотите изменить:\n\n"
        "⚠️ <i>Все изменения будут применены только после нажатия кнопки '✅ Опубликовать изменения'</i>",
        parse_mode="HTML",
        reply_markup=get_ad_edit_keyboard(ad_id)
    )
    await callback.answer()

# ========== 12.1 ОБРАБОТЧИКИ РЕДАКТИРОВАНИЯ ==========

@dp.callback_query(F.data.startswith("edit_title_"))
async def edit_title_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало редактирования заголовка"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    await state.update_data(edit_ad_id=ad_id, edit_field="title")
    await callback.message.answer(
        f"✏️ Текущий заголовок:\n\n{ad[5]}\n\nВведите новый заголовок:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(EditForm.waiting_for_new_value)
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_desc_"))
async def edit_desc_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало редактирования описания"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    await state.update_data(edit_ad_id=ad_id, edit_field="description")
    await callback.message.answer(
        f"✏️ Текущее описание:\n\n{ad[6] if ad[6] else 'Не указано'}\n\nВведите новое описание:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(EditForm.waiting_for_new_value)
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_price_"))
async def edit_price_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало редактирования цены"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    await state.update_data(edit_ad_id=ad_id, edit_field="price")
    await callback.message.answer(
        f"✏️ Текущая цена:\n\n{ad[7] if ad[7] else 'Договорная'}\n\nВведите новую цену (или 'Пропустить'):",
        reply_markup=get_skip_keyboard()
    )
    await state.set_state(EditForm.waiting_for_new_value)
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_city_"))
async def edit_city_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало редактирования города"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    await state.update_data(edit_ad_id=ad_id, edit_field="city")
    await callback.message.answer(
        f"✏️ Текущий город:\n\n{ad[3]}\n\nВведите новый город:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(EditForm.waiting_for_new_value)
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_cat_"))
async def edit_category_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало редактирования категории"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    await state.update_data(edit_ad_id=ad_id, edit_field="category")
    await callback.message.answer(
        f"🏷️ Текущая категория: {CATEGORIES.get(ad[8], 'Другое')}\n\nВыберите новую категорию:",
        reply_markup=get_category_keyboard()
    )
    await state.set_state(EditForm.waiting_for_category_edit)
    await callback.answer()

@dp.callback_query(EditForm.waiting_for_category_edit, F.data.startswith("cat_"))
async def process_edit_category(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора категории при редактировании"""
    if callback.data == "cancel_creation":
        await state.clear()
        await callback.message.edit_text("Редактирование отменено.")
        await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    
    category_key = callback.data.replace("cat_", "")
    data = await state.get_data()
    ad_id = data.get('edit_ad_id')
    
    save_edit_draft(ad_id, "category", category_key)
    
    await callback.message.edit_text(
        f"✅ Категория изменена на: {CATEGORIES[category_key]}\n\n"
        f"Не забудьте нажать '✅ Опубликовать изменения' для применения."
    )
    await callback.message.answer(
        "Продолжить редактирование:",
        reply_markup=get_ad_edit_keyboard(ad_id)
    )
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_photos_"))
async def edit_photos_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало редактирования фото"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    current_photos = get_ad_photos(ad_id)
    save_photos_draft(ad_id, current_photos)
    
    await state.update_data(edit_ad_id=ad_id)
    await callback.message.edit_text(
        "📸 Редактирование фотографий\n\n"
        f"Текущее количество фото: {len(current_photos)}/5\n\n"
        "Выберите действие:",
        reply_markup=get_photos_edit_keyboard(ad_id, current_photos)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("del_photo_"))
async def delete_photo(callback: types.CallbackQuery):
    """Удаление фото из черновика"""
    parts = callback.data.split("_")
    ad_id = int(parts[2])
    photo_index = int(parts[3])
    
    current_photos = get_photos_draft(ad_id)
    
    if photo_index < len(current_photos):
        deleted_photo = current_photos.pop(photo_index)
        save_photos_draft(ad_id, current_photos)
        await callback.answer(f"✅ Фото {photo_index + 1} удалено")
    
    await callback.message.edit_text(
        "📸 Редактирование фотографий\n\n"
        f"Текущее количество фото: {len(current_photos)}/5\n\n"
        "Выберите действие:",
        reply_markup=get_photos_edit_keyboard(ad_id, current_photos)
    )

@dp.callback_query(F.data.startswith("add_photo_"))
async def add_photo_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало добавления фото"""
    ad_id = int(callback.data.split("_")[2])
    current_photos = get_photos_draft(ad_id)
    
    if len(current_photos) >= 5:
        await callback.answer("❌ Максимум 5 фотографий", show_alert=True)
        return
    
    await state.update_data(edit_ad_id=ad_id)
    await state.set_state(EditForm.adding_photos)  # ← ДОБАВЛЕНО
    
    await callback.message.answer(
        f"📸 Отправьте фото ({len(current_photos)}/5)\n\n"
        "После отправки всех фото нажмите '✅ Закончить добавление фото'",
        reply_markup=get_add_photo_keyboard(ad_id)
    )
    await callback.answer()

@dp.message(EditForm.adding_photos, F.photo)  # ← ИЗМЕНЕНО: waiting_for_photos -> adding_photos
async def process_add_photo(message: types.Message, state: FSMContext):
    """Добавление фото при редактировании"""
    data = await state.get_data()
    ad_id = data.get('edit_ad_id')
    
    if not ad_id:
        await state.clear()
        return
    
    current_photos = get_photos_draft(ad_id)
    
    if len(current_photos) >= 5:
        await message.answer("❌ Максимум 5 фотографий")
        return
    
    current_photos.append(message.photo[-1].file_id)
    save_photos_draft(ad_id, current_photos)
    await message.answer(f"✅ Фото добавлено! ({len(current_photos)}/5)")

@dp.message(F.text == "✅ Закончить добавление фото")
async def finish_adding_photos(message: types.Message, state: FSMContext):
    """Завершение добавления фото"""
    data = await state.get_data()
    ad_id = data.get('edit_ad_id')
    
    if ad_id:
        current_photos = get_photos_draft(ad_id)
        await message.answer(
            f"✅ Добавление фото завершено. Всего фото: {len(current_photos)}/5\n\n"
            "Продолжить редактирование:",
            reply_markup=get_ad_edit_keyboard(ad_id)
        )
        await state.set_state(None)  # ← ДОБАВЛЕНО: сбрасываем состояние

@dp.callback_query(F.data.startswith("finish_photos_"))
async def finish_photos_edit(callback: types.CallbackQuery, state: FSMContext):
    """Завершение редактирования фото и сохранение изменений"""
    ad_id = int(callback.data.split("_")[2])
    new_photos = get_photos_draft(ad_id)
    
    # ← ИСПРАВЛЕНИЕ: Сохраняем фото в черновик (НЕ в changes)
    if ad_id in edit_drafts:
        edit_drafts[ad_id]['photos'] = new_photos.copy()
        edit_drafts[ad_id]['timestamp'] = datetime.now()
    else:
        edit_drafts[ad_id] = {'changes': {}, 'photos': new_photos.copy(), 'timestamp': datetime.now()}
    
    await callback.message.edit_text(
        f"✅ Изменения фото сохранены в черновик. ({len(new_photos)}/5 фото)\n\n"
        "Не забудьте нажать '✅ Опубликовать изменения' для применения.",
        reply_markup=get_ad_edit_keyboard(ad_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("back_to_edit_"))
async def back_to_edit(callback: types.CallbackQuery):
    """Возврат к редактированию"""
    ad_id = int(callback.data.split("_")[3])
    await callback.message.edit_text(
        "✏️ <b>Редактирование объявления</b>\n\n"
        "Выберите, что хотите изменить:\n\n"
        "⚠️ <i>Все изменения будут применены только после нажатия кнопки '✅ Опубликовать изменения'</i>",
        parse_mode="HTML",
        reply_markup=get_ad_edit_keyboard(ad_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_comments_"))
async def toggle_comments_handler(callback: types.CallbackQuery):
    """Включение/выключение комментариев"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    toggle_comments(ad_id)
    comments_enabled = not bool(ad[12] if len(ad) > 12 else 1)
    status = "включены" if comments_enabled else "выключены"
    
    await callback.answer(f"💬 Комментарии {status}")
    await callback.message.edit_text(
        f"✅ Комментарии {status}\n\n"
        "Продолжить редактирование:",
        reply_markup=get_ad_edit_keyboard(ad_id)
    )

@dp.callback_query(F.data.startswith("delete_ad_"))
async def delete_ad_handler(callback: types.CallbackQuery):
    """Удаление объявления"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    # Удаляем из канала
    await delete_ad_messages_from_channel(ad_id)
    
    # Удаляем из БД
    delete_ad(ad_id)
    
    await callback.message.edit_text("✅ Объявление удалено!")
    await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("publish_edits_"))
async def publish_edits(callback: types.CallbackQuery):
    """Публикация всех изменений"""
    ad_id = int(callback.data.split("_")[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    draft = get_edit_draft(ad_id)
    changes = draft.get('changes', {})
    photos_draft = draft.get('photos')  # ← ПОЛУЧАЕМ ФОТО ИЗ ЧЕРНОВИКА
    
    if not changes and not photos_draft:
        await callback.answer("Нет изменений для публикации", show_alert=True)
        return
    
    # Применяем изменения полей
    for field, value in changes.items():
        update_ad_field(ad_id, field, value)
    
    # ← ИСПРАВЛЕНИЕ: Применяем изменения фото
    if photos_draft is not None:
        update_ad_photos(ad_id, photos_draft)
        print(f"📸 Обновлены фото для объявления #{ad_id}: {len(photos_draft)} фото")
    
    increment_update_count(ad_id)
    
    # Обновляем сообщение в канале
    await delete_ad_messages_from_channel(ad_id)
    await asyncio.sleep(0.5)  # Небольшая задержка
    success = await publish_ad_to_channel(ad_id)
    
    clear_edit_draft(ad_id)
    
    if success:
        await callback.message.edit_text("✅ Изменения опубликованы! Объявление обновлено в канале.")
    else:
        await callback.message.edit_text("⚠️ Изменения сохранены, но не опубликованы в канале. Проверьте настройки канала.")
    
    await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("cancel_edits_"))
async def cancel_edits(callback: types.CallbackQuery):
    """Отмена редактирования"""
    ad_id = int(callback.data.split("_")[2])
    clear_edit_draft(ad_id)
    
    await callback.message.edit_text("❌ Редактирование отменено. Изменения не сохранены.")
    await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.message(EditForm.waiting_for_new_value)
async def process_edit_value(message: types.Message, state: FSMContext):
    """Обработка нового значения при редактировании"""
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("Редактирование отменено.", reply_markup=get_main_keyboard())
        return
    
    data = await state.get_data()
    ad_id = data.get('edit_ad_id')
    field = data.get('edit_field')
    
    if not ad_id or not field:
        await state.clear()
        await message.answer("Ошибка. Попробуйте снова.", reply_markup=get_main_keyboard())
        return
    
    if field == "price" and message.text == "⏭ Пропустить":
        new_value = None
    elif field == "price" and message.text:
        if not validate_price(message.text):
            await message.answer("❌ Некорректный формат цены. Попробуйте снова:")
            return
        new_value = message.text
    elif field == "title" and len(message.text.strip()) < 5:
        await message.answer("❌ Минимум 5 символов. Попробуйте снова:")
        return
    elif field == "description" and len(message.text.strip()) < 10:
        await message.answer("❌ Минимум 10 символов. Попробуйте снова:")
        return
    else:
        new_value = message.text.strip()
    
    # Сохраняем изменение в черновик
    save_edit_draft(ad_id, field, new_value)
    
    # Показываем сообщение об успехе
    field_names = {
        "title": "Заголовок",
        "description": "Описание",
        "price": "Цена",
        "city": "Город"
    }
    
    await message.answer(
        f"✅ {field_names.get(field, field)} изменён в черновике.\n\n"
        f"Не забудьте нажать '✅ Опубликовать изменения' для применения.",
        reply_markup=get_ad_edit_keyboard(ad_id)
    )
    await state.clear()

# ========== 13. ПЛАТНЫЕ УСЛУГИ ==========
@dp.message(F.text == "⭐ Платные услуги")
async def paid_services(message: types.Message):
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    stats = get_user_stats(message.from_user.id)
    _, active_ads, ads_limit = can_post_ads(message.from_user.id)
    await message.answer(
        f"⭐ <b>Платные услуги</b>\n\n✨ Выделение: {HIGHLIGHT_PRICE} Stars\n➕ Доп. объявление: {EXTRA_AD_PRICE} Stars\n\n"
        f"Лимит: {active_ads}/{ads_limit}\nКуплено мест: {stats['extra_ads_limit']}",
        parse_mode="HTML",
        reply_markup=get_paid_services_keyboard()
    )

@dp.message(F.text == "✨ Выделить объявление цветом")
async def highlight_ad_start(message: types.Message):
    ads = get_user_ads(message.from_user.id)
    active_ads = []
    for ad in ads:
        if len(ad) > 6 and ad[6] == 'active':
            is_highlighted = ad[8] if len(ad) > 8 else 0
            if not is_highlighted:
                active_ads.append(ad)
    if not active_ads:
        await message.answer("Нет активных объявлений для выделения.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ad[1][:30] if len(ad) > 1 else "Без названия", callback_data=f"hl_ad_{ad[0]}")] 
        for ad in active_ads
    ])
    await message.answer("Выберите объявление:", reply_markup=keyboard)

@dp.message(F.text == "⬆️ Поднять объявление")
async def bump_ad_start(message: types.Message):
    """Показывает список объявлений для поднятия"""
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    ads = get_user_ads(message.from_user.id)
    # Показываем только активные объявления
    active_ads = []
    for ad in ads:
        if len(ad) > 6 and ad[6] == 'active':
            active_ads.append(ad)
    
    if not active_ads:
        await message.answer("📭 У вас нет активных объявлений для поднятия.")
        return
    
    # Создаём клавиатуру со списком объявлений
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=ad[1][:30] if len(ad) > 1 else "Без названия", 
            callback_data=f"bump_ad_{ad[0]}"
        )] for ad in active_ads
    ])
    await message.answer(
        f"⬆️ <b>Поднятие объявления</b>\n\n"
        f"💰 Стоимость: {BUMP_PRICE} Stars\n"
        f"📅 Объявление поднимется в топ канала на 3 дня\n\n"
        f"Выберите объявление:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("bump_ad_"))
async def process_bump_selection(callback: types.CallbackQuery):
    """Обработка выбора объявления для поднятия"""
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return
    
    ad_id = int(parts[2])
    ad = get_ad_by_id(ad_id)
    
    if not ad or ad[1] != callback.from_user.id:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    await callback.message.delete()
    await callback.message.answer(
        f"⬆️ <b>Поднятие объявления #{ad_id}</b>\n\n"
        f"📌 {ad[5][:50]}\n"
        f"💰 Цена: {BUMP_PRICE} Stars\n"
        f"📅 Эффект: объявление поднимется в топ на 3 дня\n\n"
        f"Нажмите для оплаты:",
        parse_mode="HTML"
    )
    
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="⬆️ Поднятие объявления",
        description=f"Поднятие в топ на 3 дня: {ad[5][:50]}",
        payload=f"bump_{ad_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Поднятие", amount=BUMP_PRICE)],
        reply_markup=get_payment_keyboard(BUMP_PRICE, "bump", ad_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("hl_ad_"))
async def process_highlight(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return
    ad_id = int(parts[2])
    await callback.message.edit_text(f"✨ Выделение: {HIGHLIGHT_PRICE} Stars\n\nНажмите для оплаты:")
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="✨ Выделение объявления",
        description=f"Выделение #{ad_id}",
        payload=f"highlight_{ad_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Выделение", amount=HIGHLIGHT_PRICE)],
        reply_markup=get_payment_keyboard(HIGHLIGHT_PRICE, "highlight", ad_id)
    )
    await callback.answer()

@dp.message(F.text == "➕ Дополнительное объявление")
async def buy_extra_ad(message: types.Message):
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="➕ Дополнительное объявление",
        description="Увеличение лимита на 1",
        payload=f"extra_ad_{message.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Доп. место", amount=EXTRA_AD_PRICE)],
        reply_markup=get_payment_keyboard(EXTRA_AD_PRICE, "extra_ad")
    )

@dp.callback_query(F.data == "cancel_payment")
async def cancel_payment(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except:
        pass
    await callback.message.answer("❌ Оплата отменена.")
    await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_payment(message: types.Message, state: FSMContext):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("extra_ad"):
        increment_ads_limit(message.from_user.id)
        await message.answer("✅ Лимит увеличен!", reply_markup=get_main_keyboard())
    elif payload.startswith("highlight_new"):
        data = await state.get_data()
        ad_id = save_ad(
            user_id=message.from_user.id, phone=data['phone'], city=data['city'], address=data.get('address'),
            category=data['category'], title=data['title'], description=data['description'],
            price=data.get('price'), photos=data.get('photos_str', ''), is_highlighted=data.get('is_highlighted', 1)
        )
        await publish_ad_to_channel(ad_id)
        await message.answer("✅ Объявление опубликовано с выделением!", reply_markup=get_main_keyboard())
        await state.clear()
    elif payload.startswith("highlight"):
        parts = payload.split("_")
        if len(parts) >= 2:
            try:
                ad_id = int(parts[1])
                highlight_ad(ad_id)
                ad = get_ad_by_id(ad_id)
                if ad and len(ad) > 14 and ad[14]:
                    try:
                        await bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=ad[14], reply_markup=get_channel_ad_keyboard(ad_id, ad[1]))
                    except:
                        pass
                await message.answer("✨ Объявление выделено!", reply_markup=get_main_keyboard())
            except ValueError:
                await message.answer("❌ Ошибка при обработке платежа.", reply_markup=get_main_keyboard())
    elif payload.startswith("bump"):
        parts = payload.split("_")
        if len(parts) >= 2:
            try:
                ad_id = int(parts[1])
                
                # Обновляем время поднятия
                bump_ad(ad_id)
                
                # Обновляем объявление в канале (перепубликация)
                await delete_ad_messages_from_channel(ad_id)
                await asyncio.sleep(0.5)
                success = await publish_ad_to_channel(ad_id)
                
                if success:
                    await message.answer(
                        f"✅ <b>Объявление поднято в топ!</b>\n\n"
                        f"📌 Оно будет выше в канале в течение 3 дней.\n"
                        f"🕒 Поднятие действует до: {(datetime.now() + timedelta(days=3)).strftime('%d.%m.%Y')}",
                        parse_mode="HTML",
                        reply_markup=get_main_keyboard()
                    )
                else:
                    await message.answer(
                        "⚠️ Объявление поднято, но не обновлено в канале.\n"
                        "Проверьте права бота.",
                        reply_markup=get_main_keyboard()
                    )
            except ValueError:
                await message.answer("❌ Ошибка при обработке платежа.", reply_markup=get_main_keyboard())

    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"💰 Платеж: {message.successful_payment.total_amount} Stars")

# ========== 14. КОНТАКТ С ПРОДАВЦОМ ==========
@dp.callback_query(F.data.startswith("contact_seller_"))
async def contact_seller(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return
    ad_id, seller_id = int(parts[2]), int(parts[3])
    if callback.from_user.id == seller_id:
        await callback.answer("Это ваше объявление!", show_alert=True)
        return
    add_chat(ad_id, callback.from_user.id, seller_id)
    ad = get_ad_by_id(ad_id)
    ad_title = ad[5] if ad and len(ad) > 5 else "Объявление"
    await bot.send_message(
        seller_id,
        f"🆕 Запрос по: {ad_title}\n👤 {callback.from_user.first_name}\n📞 @{callback.from_user.username}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_to_{callback.from_user.id}_{ad_id}")]])
    )
    await callback.message.answer("✅ Запрос отправлен продавцу!")
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_to_"))
async def reply_to_buyer(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return
    await state.update_data(reply_buyer_id=int(parts[2]), reply_ad_id=int(parts[3]))
    await callback.message.answer("✏️ Введите сообщение:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_reply")]]))
    await state.set_state(ChatForm.waiting_for_message)
    await callback.answer()

@dp.message(ChatForm.waiting_for_message)
async def send_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ad = get_ad_by_id(data.get('reply_ad_id'))
    ad_title = ad[5] if ad and len(ad) > 5 else "Объявление"
    await bot.send_message(data.get('reply_buyer_id'), f"📩 Ответ продавца по объявлению '{ad_title}':\n\n{message.text}")
    await message.answer("✅ Отправлено!")
    await state.clear()

@dp.callback_query(F.data == "cancel_reply")
async def cancel_reply(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()

# ========== 15. КНОПКИ МЕНЮ ==========
@dp.message(F.text == "📢 Канал с объявлениями")
async def show_channel(message: types.Message):
    if CHANNEL_ID:
        try:
            chat = await bot.get_chat(CHANNEL_ID)
            channel_link = f"https://t.me/{chat.username}" if chat.username else f"https://t.me/c/{str(CHANNEL_ID)[4:]}"
            await message.answer(f"📢 <a href='{channel_link}'>Перейти в канал</a>", parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Ошибка получения канала: {e}")
            await message.answer("❌ Не удалось получить ссылку на канал. Проверьте CHANNEL_ID в .env файле.")
    else:
        await message.answer("Канал не настроен. Добавьте CHANNEL_ID в файл .env")

@dp.message(F.text == "👤 Мой профиль")
async def profile(message: types.Message):
    stats = get_user_stats(message.from_user.id)
    _, active_ads, ads_limit = can_post_ads(message.from_user.id)
    await message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"Активных: {active_ads}/{ads_limit}\n"
        f"Выделенных: {stats['highlighted_ads']}\n"
        f"Доп. мест: {stats['extra_ads_limit']}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "🔙 Назад")
async def back_to_main(message: types.Message):
    await message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "back_to_menu")
async def callback_back_to_menu(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "back_to_my_ads")
async def back_to_my_ads(callback: types.CallbackQuery):
    await cmd_my(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("my_ad_"))
async def go_to_my_ad_in_channel(callback: types.CallbackQuery):
    """Переход к объявлению в канале"""
    ad_id = int(callback.data.replace("my_ad_", ""))
    
    ad = get_ad_by_id(ad_id)
    if not ad:
        await callback.answer("❌ Объявление не найдено", show_alert=True)
        return
    
    if ad[1] != callback.from_user.id:
        await callback.answer("⛔ Это не ваше объявление", show_alert=True)
        return
    
    channel_link = await get_ad_channel_link(ad_id)
    
    if not channel_link:
        await callback.answer(
            "❌ Не удалось получить ссылку на объявление.\n"
            "Возможно, объявление ещё не опубликовано в канале.",
            show_alert=True
        )
        return
    
    await callback.message.answer(
        f"🔗 <b>Ваше объявление в канале:</b>\n\n"
        f"<a href='{channel_link}'>👉 Нажмите, чтобы открыть</a>\n\n"
        f"💡 <i>Вы можете поделиться этой ссылкой с друзьями!</i>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()

@dp.message(F.text == "🔍 Поиск объявлений")
async def search_button(message: types.Message, state: FSMContext):
    await cmd_search(message, state)

@dp.message(F.text == "❓ Помощь")
async def help_button(message: types.Message):
    await cmd_help(message)

@dp.message(F.text == "📝 Создать объявление")
async def new_button(message: types.Message, state: FSMContext):
    await cmd_new(message, state)

@dp.message(F.text == "📋 Мои объявления")
async def my_button(message: types.Message):
    await cmd_my(message)

@dp.callback_query(F.data.startswith("complaint_"))
async def complaint(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 2:
        await callback.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return
    ad_id = int(parts[1])
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"🚨 Жалоба на объявление #{ad_id}\nОт: {callback.from_user.first_name}")
    await callback.answer("Жалоба отправлена", show_alert=True)

# ========== 16. УНИВЕРСАЛЬНЫЕ ОБРАБОТЧИКИ ==========

@dp.callback_query(F.data == "back_main_menu")
async def back_main_menu(callback: types.CallbackQuery):
    """Возврат в главное меню"""
    await callback.message.delete()
    await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query()
async def handle_unknown_callback(callback: types.CallbackQuery):
    logging.warning(f"Неизвестный callback: {callback.data}")
    await callback.answer("⚠️ Эта функция временно недоступна", show_alert=False)

@dp.message()
async def handle_unknown_message(message: types.Message):
    if message.text and not message.text.startswith('/') and message.text not in [
        "📝 Создать объявление", "📋 Мои объявления", "🔍 Поиск объявлений",
        "⭐ Платные услуги", "✏️ Редактировать", "👤 Мой профиль",
        "❓ Помощь", "📢 Канал с объявлениями", "🔙 Назад",
        "✅ Закончить добавление фото", "❌ Отменить",
        "⬆️ Поднять объявление"
    ]:
        await message.answer(
            "❓ Я не понял команду.\n\n"
            "Используйте кнопки меню или команды:\n"
            "/new - создать объявление\n"
            "/my - мои объявления\n"
            "/search - поиск\n"
            "/help - помощь",
            reply_markup=get_main_keyboard()
        )

# ========== 17. АДМИН-ПАНЕЛЬ ==========
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return
    stats = get_admin_stats()
    await message.answer(
        f"👑 <b>Админ-панель</b>\n\n"
        f"📊 Активных: {stats['active_ads']}\n"
        f"👥 Пользователей: {stats['active_users']}\n"
        f"🚫 Заблокированных: {stats['banned_users']}\n"
        f"💬 Комментариев: {stats['total_comments']}\n\n"
        "Команды:\n/ban <id> - заблокировать\n/unban <id> - разблокировать\n/del <ad_id> - удалить объявление",
        parse_mode="HTML"
    )

@dp.message(Command("ban"))
async def admin_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
        ban_user(user_id)
        await message.answer(f"✅ Пользователь {user_id} заблокирован.")
        await bot.send_message(user_id, "⛔ Вы заблокированы администратором.")
    except (IndexError, ValueError):
        await message.answer("❌ Использование: /ban user_id")

@dp.message(Command("unban"))
async def admin_unban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
        unban_user(user_id)
        await message.answer(f"✅ Пользователь {user_id} разблокирован.")
        await bot.send_message(user_id, "✅ Вы разблокированы администратором.")
    except (IndexError, ValueError):
        await message.answer("❌ Использование: /unban user_id")

@dp.message(Command("del"))
async def admin_del(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        ad_id = int(message.text.split()[1])
        delete_ad(ad_id)
        await message.answer(f"✅ Объявление {ad_id} удалено.")
    except (IndexError, ValueError):
        await message.answer("❌ Использование: /del ad_id")

# ========== 18. ЗАПУСК БОТА ==========
async def main():
    """Главная функция запуска бота"""
    print("🚀 Бот запускается...")
    
    # Информация о боте
    try:
        bot_info = await bot.get_me()
        print(f"✅ Бот запущен: @{bot_info.username} (ID: {bot_info.id})")
    except Exception as e:
        print(f"⚠️ Не удалось получить информацию о боте: {e}")
    
    # Очистка старых черновиков при запуске
    cleanup_old_drafts()
    
    # Удаление просроченных объявлений при запуске
    print("🔍 Проверка просроченных объявлений...")
    deleted = await delete_expired_ads_from_channel()
    if deleted > 0:
        print(f"🗑️ Удалено просроченных объявлений: {deleted}")
    else:
        print("✅ Просроченных объявлений не найдено")
    
    # Периодическая очистка черновиков (каждый час)
    async def periodic_cleanup():
        while True:
            await asyncio.sleep(3600)  # 1 час
            cleanup_old_drafts()
            print("🧹 Выполнена очистка старых черновиков")
    
    # Периодическая проверка просроченных объявлений (каждые 6 часов)
    async def periodic_expiration_check():
        while True:
            await asyncio.sleep(21600)  # 6 часов
            print("🔍 Запущена плановая проверка просроченных объявлений...")
            deleted = await delete_expired_ads_from_channel()
            if deleted > 0:
                print(f"🗑️ Удалено просроченных объявлений: {deleted}")
            else:
                print("✅ Просроченных объявлений не найдено")
    
    # Запускаем фоновые задачи
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(periodic_expiration_check())
    
    print("✅ Все фоновые задачи запущены")
    print("🚀 Бот готов к работе!")
    
    # Запускаем бота
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Бот остановлен пользователем")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")