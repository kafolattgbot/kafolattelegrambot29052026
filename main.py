import os
from dotenv import load_dotenv

load_dotenv()
# ==================== ВАЛИДАЦИЯ ENV ====================
REQUIRED_ENV = [
    "API_TOKEN",
    "SUPER_ADMIN_ID",
    "ADMIN_CHAT_ID",
    "WEBAPP_URL",
    "WEBAPP_CATALOGUE_URL",
    "HOSTING_FTP_HOST",
    "HOSTING_FTP_USER",
    "HOSTING_FTP_PASS",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASS",
    "GOOGLE_SHEETS_URL",  # ✅ НОВОЕ: URL для получения товаров
]
for key in REQUIRED_ENV:
    if not os.getenv(key):
        raise RuntimeError(f"❌ Переменная окружения {key} не найдена (.env)")

import json
import logging
import asyncio
import io
import pymysql
from pymysql.cursors import DictCursor
import csv
import re
from datetime import datetime, timedelta
from ftplib import FTP
from collections import defaultdict
from contextlib import contextmanager
from typing import Optional, Dict, Any, List
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
import aiohttp  # ✅ НОВОЕ: для асинхронных запросов к Google Sheets
from concurrent.futures import ThreadPoolExecutor

# Создаем пул потоков для параллельной загрузки изображений
image_download_executor = ThreadPoolExecutor(max_workers=10)

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    ContentType,
    ReplyKeyboardRemove,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    TelegramObject,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import BaseMiddleware
from typing import Callable, Awaitable

# ==== PDF / QR ====
import qrcode
import textwrap
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image

# ==================== GOOGLE SHEETS INTEGRATION ====================
GOOGLE_SHEETS_URL = os.getenv("GOOGLE_SHEETS_URL")
products_cache = {}  # Кеш товаров
cache_timestamp = None
CACHE_LIFETIME = 3600  # 5 минут

# Кеш изображений товаров
image_cache = {}  # {url: PIL.Image}
image_cache_timestamp = {}  # {url: datetime}
IMAGE_CACHE_LIFETIME = 3600  # 1 час


async def fetch_products_from_sheets():
    """Асинхронная загрузка товаров из Google Sheets"""
    global products_cache, cache_timestamp
    
    # Проверяем кеш
    if cache_timestamp and (datetime.now() - cache_timestamp).total_seconds() < CACHE_LIFETIME:
        return products_cache
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GOOGLE_SHEETS_URL, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Преобразуем в словарь {id: product}
                    products_cache = {}
                    for category_products in data.values():
                        for product in category_products:
                            product_id = int(product.get('id', 0))
                            if product_id:
                                products_cache[product_id] = product
                    
                    cache_timestamp = datetime.now()
                    logger.info(f"✅ Loaded {len(products_cache)} products from Google Sheets")
                    return products_cache
                else:
                    logger.error(f"❌ Failed to fetch products: HTTP {response.status}")
                    return products_cache
    except Exception as e:
        logger.exception(f"❌ Error fetching products from Google Sheets: {e}")
        return products_cache


async def get_product_info(product_id: int) -> Optional[Dict]:
    """Получить информацию о товаре по ID"""
    products = await fetch_products_from_sheets()
    return products.get(product_id)


class ValidationError(Exception):
    """Кастомное исключение для ошибок валидации"""
    pass

# ==================== КОНФИГУРАЦИЯ ТАЙМЕРА WEBAPP ====================
# Время активности кнопки "Сделать заказ" в секундах
WEBAPP_BUTTON_TIMEOUT = int(os.getenv("WEBAPP_BUTTON_TIMEOUT", "300"))


# Словарь для хранения времени последнего /start для каждого пользователя
user_start_times: Dict[int, datetime] = {}


def is_webapp_button_active(user_id: int) -> bool:
    return True


def update_user_start_time(user_id: int):
    """Обновляет время последнего /start для пользователя"""
    user_start_times[user_id] = datetime.now()
    logger.info(f"[TIMER] User {user_id} timer STARTED at {user_start_times[user_id]}")


def get_remaining_time(user_id: int) -> int:
    """Возвращает оставшееся время в секундах"""
    if user_id not in user_start_times:
        return 0

    elapsed = (datetime.now() - user_start_times[user_id]).total_seconds()
    remaining = WEBAPP_BUTTON_TIMEOUT - elapsed
    return max(0, int(remaining))

# 🔄 Принудительное обновление главного меню (для скрытия WebApp)
async def refresh_main_menu(user_id: int, state: FSMContext):
    data = await state.get_data()
    old_message_id = data.get("menu_message_id")

    lang = get_user_lang(user_id)
    kb = get_main_menu_keyboard(user_id, lang)

    try:
        # ❌ удаляем старое меню
        if old_message_id:
            await bot.delete_message(
                chat_id=user_id,
                message_id=old_message_id
            )

        # ✅ отправляем новое меню
        menu_text = (
            "Пожалуйста, вернитесь в главное меню\n\n"
            "Нажмите кнопку «🏠 Главный меню»"
            if lang == "ru"
            else
            "Iltimos, bosh menyuga qayting.\n\n"
            "«🏠 Bosh menyu» tugmasini bosing"
        )

        sent = await bot.send_message(
            chat_id=user_id,
            text=menu_text,
            reply_markup=kb
        )

        # сохраняем новый message_id
        await state.update_data(menu_message_id=sent.message_id)

    except Exception as e:
        logger.warning(f"Failed to refresh menu for {user_id}: {e}")

# ==================== НАСТРОЙКИ АДМИНИСТРАТОРОВ ====================

class AdminRole:
    """Роли администраторов"""
    SUPER_ADMIN = "super_admin"
    SALES = "sales"
    PRODUCTION = "production"
    WAREHOUSE = "warehouse"


# Загрузка ID администраторов из .env
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID"))
SALES_ADMIN_IDS = [int(x.strip()) for x in os.getenv("SALES_ADMIN_IDS", "").split(",") if x.strip()]

# Производственные цеха (по категориям)
PRODUCTION_CLEANING_IDS = [int(x.strip()) for x in os.getenv("PRODUCTION_CLEANING_IDS", "").split(",") if x.strip()]
PRODUCTION_PLASTICPE_IDS = [int(x.strip()) for x in os.getenv("PRODUCTION_PLASTICPE_IDS", "").split(",") if x.strip()]
PRODUCTION_PLASTICPET_IDS = [int(x.strip()) for x in os.getenv("PRODUCTION_PLASTICPET_IDS", "").split(",") if x.strip()]
PRODUCTION_PLASTICPP_IDS = [int(x.strip()) for x in os.getenv("PRODUCTION_PLASTICPP_IDS", "").split(",") if x.strip()]
PRODUCTION_PLASTICTD_IDS = [int(x.strip()) for x in os.getenv("PRODUCTION_PLASTICTD_IDS", "").split(",") if x.strip()]
PRODUCTION_CLEANING2_IDS = [int(x.strip()) for x in os.getenv("PRODUCTION_CLEANING2_IDS", "").split(",") if x.strip()]
PRODUCTION_FRAGRANCES_IDS = [int(x.strip()) for x in os.getenv("PRODUCTION_FRAGRANCES_IDS", "").split(",") if x.strip()]

# Объединенный список производственных админов (все цеха вместе)
PRODUCTION_ADMIN_IDS = (PRODUCTION_CLEANING_IDS + PRODUCTION_PLASTICPE_IDS +
                        PRODUCTION_PLASTICPET_IDS + PRODUCTION_PLASTICPP_IDS +
                        PRODUCTION_PLASTICTD_IDS + PRODUCTION_CLEANING2_IDS +
                        PRODUCTION_FRAGRANCES_IDS)

WAREHOUSE_ADMIN_IDS = [int(x.strip()) for x in os.getenv("WAREHOUSE_ADMIN_IDS", "").split(",") if x.strip()]

# Объединенный список всех админов для rate limiting
ALL_ADMIN_IDS = [SUPER_ADMIN_ID] + SALES_ADMIN_IDS + PRODUCTION_ADMIN_IDS + WAREHOUSE_ADMIN_IDS

# Маппинг категорий товаров на цеха
CATEGORY_TO_PRODUCTION_IDS = {
    "cleaning": PRODUCTION_CLEANING_IDS,
    "plasticpe": PRODUCTION_PLASTICPE_IDS,
    "plasticpet": PRODUCTION_PLASTICPET_IDS,
    "plasticpp": PRODUCTION_PLASTICPP_IDS,
    "plastictd": PRODUCTION_PLASTICTD_IDS,
    "cleaning2": PRODUCTION_CLEANING2_IDS,
    "fragrances": PRODUCTION_FRAGRANCES_IDS,
}

# Названия категорий для отображения
CATEGORY_NAMES = {
    "cleaning": "Моющие средства",
    "plasticpe": "Вдувные ПЭ",
    "plasticpet": "ПЭТ",
    "plasticpp": "ПП",
    "plastictd": "Распылители & Дозаторы",
    "cleaning2": "Моющие средства 2",
    "fragrances": "Отдушки",
    # Дополнительные варианты написания (на случай разных форматов в Google Sheets)
    "Моющие средства": "Моющие средства",
    "Моющие средства 2": "Моющие средства 2",
    "Пластик": "Пластик",
    "Отдушки": "Отдушки",
}


# Функция проверки прав доступа
def has_permission(user_id: int, required_role: str, order_category: str = None) -> bool:
    """Проверяет, есть ли у пользователя права для выполнения действия"""
    # Супер-админ имеет доступ ко всему
    if user_id == SUPER_ADMIN_ID:
        return True

    if required_role == AdminRole.SALES:
        return user_id in SALES_ADMIN_IDS
    elif required_role == AdminRole.PRODUCTION:
        # Если указана категория, проверяем права для конкретного цеха
        if order_category:
            production_ids = get_production_ids_for_category(order_category)
            return user_id in production_ids
        # Если категория не указана, проверяем общий доступ к производству
        return user_id in PRODUCTION_ADMIN_IDS
    elif required_role == AdminRole.WAREHOUSE:
        return user_id in WAREHOUSE_ADMIN_IDS

    return False


def get_admin_name(user_id: int) -> str:
    """Возвращает роль администратора"""
    if user_id == SUPER_ADMIN_ID:
        return "Супер-админ"
    elif user_id in SALES_ADMIN_IDS:
        return "Отдел продаж"
    elif user_id in PRODUCTION_ADMIN_IDS:
        return "Отдел производства"
    elif user_id in WAREHOUSE_ADMIN_IDS:
        return "Склад"
    return f"Админ {user_id}"


def get_order_category(order_items: list) -> str:
    """Определяет категорию заказа на основе товаров (первого товара)"""
    if not order_items:
        return None

    # Получаем ID первого товара
    first_item_id = order_items[0].get("id", 0)

    # Определяем категорию по диапазону ID
    if 10000 <= first_item_id < 20000:
        return "cleaning"
    elif 20000 <= first_item_id < 30000:
        return "plasticpe"
    elif 30000 <= first_item_id < 40000:
        return "plasticpet"
    elif 40000 <= first_item_id < 50000:
        return "plasticpp"
    elif 50000 <= first_item_id < 60000:
        return "plastictd"
    elif 60000 <= first_item_id < 70000:
        return "cleaning2"
    elif 70000 <= first_item_id < 80000:
        return "fragrances"

    return None


def get_category_by_item_id(item_id: int) -> str:
    """Определяет категорию по ID товара"""
    if 10000 <= item_id < 20000:
        return "cleaning"
    elif 20000 <= item_id < 30000:
        return "plasticpe"
    elif 30000 <= item_id < 40000:
        return "plasticpet"
    elif 40000 <= item_id < 50000:
        return "plasticpp"
    elif 50000 <= item_id < 60000:
        return "plastictd"
    elif 60000 <= item_id < 70000:
        return "cleaning2"
    elif 70000 <= item_id < 80000:
        return "fragrances"
    return None


def group_items_by_category(order_items: list) -> dict:
    """Группирует товары по категориям
    
    Использует поле 'category' из Google Sheets
    Возвращает словарь: {category: [items]}
    """
    grouped = {}
    for item in order_items:
        # ✅ ИСПОЛЬЗУЕМ КАТЕГОРИЮ ИЗ GOOGLE SHEETS
        category = item.get("category")
        
        if not category or category == "unknown":
            # Fallback: пытаемся определить по ID (для старых заказов)
            item_id = item.get("id", 0)
            category = get_category_by_item_id(item_id)
        
        if category:
            if category not in grouped:
                grouped[category] = []
            grouped[category].append(item)
    return grouped


def get_production_ids_for_category(category: str) -> list:
    """Возвращает список ID производственных админов для категории"""
    return CATEGORY_TO_PRODUCTION_IDS.get(category, [])


def get_category_name(category: str) -> str:
    """Возвращает название категории
    
    Если категория в словаре CATEGORY_NAMES - возвращает русское название
    Иначе - возвращает саму категорию (для гибкости с Google Sheets)
    """
    if not category:
        return "Неизвестная категория"
    
    # Если есть в словаре - берем оттуда
    if category in CATEGORY_NAMES:
        return CATEGORY_NAMES[category]
    
    # Иначе возвращаем как есть (для новых категорий из Google Sheets)
    return category.capitalize()


# Эмодзи для категорий
CATEGORY_EMOJIS = {
    "cleaning": "🧴",
    "plasticpe": "🔵",
    "plasticpet": "♻️",
    "plasticpp": "🟣",
    "plastictd": "💧",
    "cleaning2": "🧪",
    "fragrances": "🌸",
    # Дополнительные варианты
    "Моющие средства": "🧴",
    "Моющие средства 2": "🧪",
    "Пластик": "🔵",
    "Отдушки": "🌸",
}


def get_category_emoji(category: str) -> str:
    """Возвращает эмодзи категории"""
    return CATEGORY_EMOJIS.get(category, "📦")


# ==================== СТАТУСЫ ЗАКАЗОВ ====================

class OrderStatus:
    """Статусы заказов"""
    PENDING = "pending"  # Ожидает одобрения
    APPROVED = "approved"  # Одобрен отделом продаж
    PRODUCTION_RECEIVED = "production_received"  # Отдел производства получил
    PRODUCTION_STARTED = "production_started"  # Производство начато
    SENT_TO_WAREHOUSE = "sent_to_warehouse"  # Отправлено на склад
    WAREHOUSE_RECEIVED = "warehouse_received"  # Склад получил
    REJECTED = "rejected"  # Отклонен


STATUS_MESSAGES = {
    OrderStatus.APPROVED: {
        "ru": "✅ Ваш заказ #{order_id} одобрен отделом продаж!",
        "uz": "✅ Sizning buyurtmangiz #{order_id} savdo bo'limi tomonidan tasdiqlandi!"
    },
    OrderStatus.PRODUCTION_RECEIVED: {
        "ru": "📋 Ваш заказ #{order_id} получен отделом производства.",
        "uz": "📋 Sizning buyurtmangiz #{order_id} ishlab chiqarish bo'limi tomonidan qabul qilindi."
    },
    OrderStatus.PRODUCTION_STARTED: {
        "ru": "🏭 Ваш заказ #{order_id} начал готовиться на производстве!",
        "uz": "🏭 Sizning buyurtmangiz #{order_id} ishlab chiqarilmoqda!"
    },
    OrderStatus.SENT_TO_WAREHOUSE: {
        "ru": "📦 Ваш заказ #{order_id} отправлен на склад.",
        "uz": "📦 Sizning buyurtmangiz #{order_id} omborga yuborildi."
    },
    OrderStatus.WAREHOUSE_RECEIVED: {
        "ru": "✅ Ваш заказ #{order_id} получен складом и готов к отправке!",
        "uz": "✅ Sizning buyurtmangiz #{order_id} ombor tomonidan qabul qilindi va jo'natishga tayyor!"
    },
    OrderStatus.REJECTED: {
        "ru": "❌ Ваш заказ #{order_id} отклонён.\n\nДля уточнения деталей свяжитесь с администратором.",
        "uz": "❌ Sizning buyurtmangiz #{order_id} rad etildi.\n\nTafsilotlarni bilish uchun administrator bilan bog'laning."
    }
}

# Названия статусов для отображения
STATUS_NAMES_RU = {
    OrderStatus.PENDING: "⏳ Ожидает",
    OrderStatus.APPROVED: "✅ Одобрено",
    OrderStatus.PRODUCTION_RECEIVED: "📋 Получено производством",
    OrderStatus.PRODUCTION_STARTED: "🏭 В производстве",
    OrderStatus.SENT_TO_WAREHOUSE: "📦 На складе",
    OrderStatus.WAREHOUSE_RECEIVED: "✅ Готово",
    OrderStatus.REJECTED: "❌ Отклонено"
}

STATUS_NAMES_UZ = {
    OrderStatus.PENDING: "⏳ Kutilmoqda",
    OrderStatus.APPROVED: "✅ Tasdiqlandi",
    OrderStatus.PRODUCTION_RECEIVED: "📋 Ishlab chiqarish qabul qildi",
    OrderStatus.PRODUCTION_STARTED: "🏭 Ishlab chiqarilmoqda",
    OrderStatus.SENT_TO_WAREHOUSE: "📦 Omborga yuborildi",
    OrderStatus.WAREHOUSE_RECEIVED: "✅ Tayyor",
    OrderStatus.REJECTED: "❌ Rad etildi"
}

# ==================== НАСТРОЙКИ ====================
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "")
DEALER_CHECK_INTERVAL = 10  # 10 сек

dealer_cache = {}
dealer_block_time = {}

API_TOKEN = os.getenv("API_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
ADMIN_NAME = os.getenv("ADMIN_NAME", "Administrator")
WEBAPP_URL = os.getenv("WEBAPP_URL")
WEBAPP_CATALOGUE_URL = os.getenv("WEBAPP_CATALOGUE_URL")
URL_ANALYSIS = os.getenv("URL_ANALYSIS")

# Файлы
USERS_FILE = "users.txt"
LANG_FILE = "user_lang.json"
PROFILE_FILE = "user_profile.json"

# MySQL настройки
DB_CONFIG = {
    'host': os.getenv("DB_HOST"),
    'port': int(os.getenv("DB_PORT", "3306")),
    'user': os.getenv("DB_USER"),
    'password': os.getenv("DB_PASS"),
    'database': os.getenv("DB_NAME"),
    'charset': 'utf8mb4',
    'cursorclass': DictCursor,
    'autocommit': False
}


# FTP настройки
HOSTING_BASE_URL = os.getenv("HOSTING_BASE_URL", "")
HOSTING_FTP_HOST = os.getenv("HOSTING_FTP_HOST")
HOSTING_FTP_USER = os.getenv("HOSTING_FTP_USER")
HOSTING_FTP_PASS = os.getenv("HOSTING_FTP_PASS")
HOSTING_FTP_DIR = os.getenv("HOSTING_FTP_DIR", "")

# Новые настройки
ORDER_COOLDOWN_SECONDS = int(os.getenv("ORDER_COOLDOWN_SECONDS", "60"))
PDF_MAX_SIZE_MB = int(os.getenv("PDF_MAX_SIZE_MB", "10"))
FTP_TIMEOUT = int(os.getenv("FTP_TIMEOUT", "30"))


# ==================== БЕЗОПАСНОЕ ЛОГИРОВАНИЕ ====================

class SecretFilter(logging.Filter):
    """Фильтр для удаления секретных данных из логов"""

    def __init__(self, secrets: List[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) > 4]

    def filter(self, record: logging.LogRecord) -> bool:
        """Заменяет секретные данные на звездочки"""
        if isinstance(record.msg, str):
            for secret in self.secrets:
                record.msg = record.msg.replace(secret, "***SECRET***")

        if record.args:
            filtered_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    for secret in self.secrets:
                        arg = arg.replace(secret, "***SECRET***")
                filtered_args.append(arg)
            record.args = tuple(filtered_args)

        return True


# Настройка логирования с фильтром секретов
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

secret_filter = SecretFilter([API_TOKEN, HOSTING_FTP_PASS])
for handler in logging.root.handlers:
    handler.addFilter(secret_filter)

logger = logging.getLogger(__name__)


# ==================== MYSQL CONNECTION POOL ====================

from contextlib import contextmanager

@contextmanager
def get_db_connection():
    """Контекстный менеджер для MySQL соединения"""
    connection = None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        yield connection
        connection.commit()
    except Exception as e:
        if connection:
            connection.rollback()
        logger.exception(f"Database error: {e}")
        raise
    finally:
        if connection:
            connection.close()




# ==================== RATE LIMITING MIDDLEWARE ====================

class RateLimitMiddleware(BaseMiddleware):
    """Middleware для ограничения частоты запросов"""

    def __init__(
            self,
            message_limit: int = 20,
            message_window: int = 60,
            order_cooldown: int = 60,
            admin_ids: List[int] = None
    ):
        super().__init__()
        self.message_limit = message_limit
        self.message_window = timedelta(seconds=message_window)
        self.order_cooldown = timedelta(seconds=order_cooldown)
        self.admin_ids = admin_ids or []

        self.message_timestamps: Dict[int, list] = defaultdict(list)
        self.last_order_time: Dict[int, datetime] = {}

    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        message: Message = event
        user_id = message.from_user.id

        # Админы пропускаются
        if user_id in self.admin_ids:
            return await handler(event, data)

        now = datetime.now()

        # Очистка старых меток
        self._cleanup_old_timestamps(user_id, now)

        # Проверка лимита
        if not self._check_message_rate(user_id, now):
            logger.warning(f"Rate limit exceeded for user {user_id}")
            await message.answer("⚠️ Слишком много запросов. Пожалуйста, подождите немного.")
            return

        self.message_timestamps[user_id].append(now)
        return await handler(event, data)

    def _cleanup_old_timestamps(self, user_id: int, now: datetime):
        cutoff = now - self.message_window
        self.message_timestamps[user_id] = [
            ts for ts in self.message_timestamps[user_id] if ts > cutoff
        ]

    def _check_message_rate(self, user_id: int, now: datetime) -> bool:
        return len(self.message_timestamps[user_id]) < self.message_limit

    def check_order_cooldown(self, user_id: int) -> tuple[bool, int]:
        """Проверяет, можно ли пользователю создать заказ"""
        now = datetime.now()
        last_order = self.last_order_time.get(user_id)

        if last_order is None:
            return True, 0

        time_passed = now - last_order
        if time_passed >= self.order_cooldown:
            return True, 0

        remaining = (self.order_cooldown - time_passed).total_seconds()
        return False, int(remaining)

    def register_order(self, user_id: int):
        """Регистрирует новый заказ"""
        self.last_order_time[user_id] = datetime.now()


# Глобальный экземпляр rate limiter
rate_limiter = RateLimitMiddleware(
    message_limit=20,
    message_window=60,
    order_cooldown=ORDER_COOLDOWN_SECONDS,
    admin_ids=ALL_ADMIN_IDS
)

# ==================== WEBAPP TIMER MIDDLEWARE ====================

class WebAppTimerMiddleware(BaseMiddleware):
    """Middleware для проверки активности кнопки WebApp"""

    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any]
    ) -> Any:
        # Проверяем, является ли событие сообщением с web_app_data
        if isinstance(event, Message) and event.web_app_data:
            user_id = event.from_user.id
            logger.info(f"[TIMER MIDDLEWARE] WebApp data received from user {user_id}")

            # Проверяем, активна ли кнопка
            if not is_webapp_button_active(user_id):
                logger.warning(f"[TIMER MIDDLEWARE] BLOCKING WebApp for user {user_id} - timer expired!")

                # Отправляем сообщение без определения языка (или используем русский по умолчанию)
                await event.answer(
                    "⏰ Время действия кнопки истекло.\n"
                    "Пожалуйста, нажмите /start для создания нового заказа.\n\n"
                    "⏰ Tugma faolligi tugadi.\n"
                    "Iltimos, yangi buyurtma yaratish uchun /start ni bosing.",
                    reply_markup=ReplyKeyboardRemove()
                )

                return  # Прерываем обработку

            logger.info(f"[TIMER MIDDLEWARE] ALLOWING WebApp for user {user_id} - timer active")

        # Продолжаем обработку
        return await handler(event, data)



# ==================== ВАЛИДАЦИЯ ДАННЫХ ====================

class ValidationError(Exception):
    """Ошибка валидации"""
    pass


class OrderDataValidator:
    """Валидатор данных заказа от WebApp"""

    @staticmethod
    def validate_order_data(data: Any) -> Dict[str, Any]:
        """Валидирует данные заказа"""
        if not isinstance(data, dict):
            raise ValidationError("Данные должны быть объектом")

        if "items" not in data:
            raise ValidationError("Отсутствует поле items")

        if "total" not in data:
            raise ValidationError("Отсутствует поле total")

        items = data["items"]
        if not isinstance(items, list):
            raise ValidationError("items должен быть массивом")

        if len(items) == 0:
            raise ValidationError("Заказ не может быть пустым")

        if len(items) > 200:
            raise ValidationError("Слишком много товаров (максимум 200)")

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"Товар {idx + 1} должен быть объектом")

            if "name" not in item:
                raise ValidationError(f"Товар {idx + 1}: отсутствует название")

            if "price" not in item:
                raise ValidationError(f"Товар {idx + 1}: отсутствует цена")

            # Поддержка разных полей для количества: quantity, count, amount, qty
            qty_field = None
            if "quantity" in item:
                qty_field = "quantity"
            elif "count" in item:
                qty_field = "count"
            elif "amount" in item:
                qty_field = "amount"
            elif "qty" in item:
                qty_field = "qty"
            else:
                raise ValidationError(f"Товар {idx + 1}: отсутствует количество (quantity/count/amount/qty)")

            try:
                price = float(item["price"])
                if price < 0 or price > 10000000000:
                    raise ValidationError(f"Товар {idx + 1}: некорректная цена")
            except (ValueError, TypeError):
                raise ValidationError(f"Товар {idx + 1}: цена должна быть числом")

            try:
                qty = int(item[qty_field])
                if qty <= 0 or qty > 1000000000:
                    raise ValidationError(f"Товар {idx + 1}: некорректное количество")
                # Нормализуем поле к "quantity" для единообразия
                item["quantity"] = qty
            except (ValueError, TypeError):
                raise ValidationError(f"Товар {idx + 1}: количество должно быть целым числом")

        try:
            total = float(data["total"])
            if total < 0 or total > 1000000000000:
                raise ValidationError("Некорректная общая сумма")
        except (ValueError, TypeError):
            raise ValidationError("Общая сумма должна быть числом")

        return data


# ==================== БАЗА ДАННЫХ ====================

def init_db():
    """Инициализация базы данных MySQL с новыми статусами"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Создаем таблицу пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                language VARCHAR(10) DEFAULT 'ru',
                phone VARCHAR(50),
                city VARCHAR(255),
                full_name VARCHAR(255),
                latitude DECIMAL(10, 7),
                longitude DECIMAL(10, 7),
                created_at DATETIME NOT NULL,
                last_activity DATETIME,
                INDEX idx_phone (phone),
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        
        # Создаем таблицу заказов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id VARCHAR(50) PRIMARY KEY,
                client_name VARCHAR(255) NOT NULL,
                user_id BIGINT NOT NULL,
                total DECIMAL(15, 2) NOT NULL,
                created_at DATETIME NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                pdf_draft LONGBLOB,
                pdf_final LONGBLOB,
                order_json TEXT,
                approved_by BIGINT,
                production_received_by BIGINT,
                production_started_by BIGINT,
                sent_to_warehouse_by BIGINT,
                warehouse_received_by BIGINT,
                category VARCHAR(50),
                base_order_id VARCHAR(50),
                INDEX idx_user_id (user_id),
                INDEX idx_status (status),
                INDEX idx_created_at (created_at),
                INDEX idx_base_order_id (base_order_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # Создаем таблицу уведомлений клиентов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS client_notifications (
                base_order_id VARCHAR(50) PRIMARY KEY,
                user_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                created_at DATETIME NOT NULL,
                INDEX idx_user_id (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        # Таблица дилеров
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dealers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                telegram_id BIGINT UNIQUE,
                phone VARCHAR(50),
                name VARCHAR(255),
                status ENUM('active', 'inactive') DEFAULT 'active',
                added_at DATETIME NOT NULL,
                INDEX idx_telegram_id (telegram_id),
                INDEX idx_phone (phone),
                INDEX idx_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        conn.commit()
        logger.info("✅ Database tables created/verified")


def migrate_users_from_files():
    """Миграция данных пользователей из локальных файлов в базу данных"""
    try:
        migrated_count = 0
        
        # Миграция user IDs из users.txt
        if os.path.exists(USERS_FILE):
            logger.info("Migrating users from users.txt...")
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                user_ids = [int(line.strip()) for line in f if line.strip()]
            
            for user_id in user_ids:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO users (user_id, created_at, last_activity, language)
                            VALUES (%s, %s, %s, %s)
                        """, (user_id, datetime.now(), datetime.now(), 'ru'))
                        conn.commit()
                        migrated_count += 1
            
            logger.info(f"Migrated {migrated_count} users from users.txt")
        
        # Миграция языков из user_lang.json
        if os.path.exists(LANG_FILE):
            logger.info("Migrating languages from user_lang.json...")
            with open(LANG_FILE, "r", encoding="utf-8") as f:
                lang_data = json.load(f)
            
            for user_id_str, lang in lang_data.items():
                user_id = int(user_id_str)
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE users SET language = %s WHERE user_id = %s
                    """, (lang, user_id))
                    conn.commit()
            
            logger.info(f"Migrated languages for {len(lang_data)} users")
        
        # Миграция профилей из user_profile.json
        if os.path.exists(PROFILE_FILE):
            logger.info("Migrating profiles from user_profile.json...")
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                profile_data = json.load(f)
            
            for user_id_str, profile in profile_data.items():
                user_id = int(user_id_str)
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE users 
                        SET phone = %s, city = %s, full_name = %s, latitude = %s, longitude = %s
                        WHERE user_id = %s
                    """, (
                        profile.get('phone'),
                        profile.get('city'),
                        profile.get('full_name'),
                        profile.get('latitude'),
                        profile.get('longitude'),
                        user_id
                    ))
                    conn.commit()
            
            logger.info(f"Migrated profiles for {len(profile_data)} users")
        
        logger.info("✅ User data migration completed successfully")
        
    except Exception as e:
        logger.exception("❌ Error during user data migration")


def save_order(order_id: str, client_name: str, user_id: int, total: float,
               pdf_draft: bytes, order_json: dict, category: str = None, base_order_id: str = None):
    """Сохранение нового заказа"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO orders 
            (order_id, client_name, user_id, total, created_at, status, pdf_draft, order_json, category, base_order_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id,
            client_name,
            user_id,
            total,
            datetime.now(),
            OrderStatus.PENDING,
            pdf_draft,
            json.dumps(order_json, ensure_ascii=False),
            category,
            base_order_id
        ))
        conn.commit()


def update_order_status(order_id: str, new_status: str, pdf_final: bytes = None, updated_by: int = None):
    """Обновление статуса заказа"""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Определяем, какое поле обновлять
        field_map = {
            OrderStatus.APPROVED: "approved_by",
            OrderStatus.PRODUCTION_RECEIVED: "production_received_by",
            OrderStatus.PRODUCTION_STARTED: "production_started_by",
            OrderStatus.SENT_TO_WAREHOUSE: "sent_to_warehouse_by",
            OrderStatus.WAREHOUSE_RECEIVED: "warehouse_received_by"
        }

        if pdf_final:
            cursor.execute("""
                UPDATE orders 
                SET status = %s, pdf_final = %s
                WHERE order_id = %s
            """, (new_status, pdf_final, order_id))
        else:
            cursor.execute("""
                UPDATE orders 
                SET status = %s
                WHERE order_id = %s
            """, (new_status, order_id))

        # Обновляем поле с ID администратора
        if updated_by and new_status in field_map:
            field_name = field_map[new_status]
            cursor.execute(f"""
                UPDATE orders 
                SET {field_name} = %s
                WHERE order_id = %s
            """, (updated_by, order_id))

        conn.commit()


def get_order_raw(order_id: str) -> Optional[Dict[str, Any]]:
    """Получение сырых данных заказа"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def get_order_for_user(order_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    """Получение заказа для конкретного пользователя"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE order_id = %s AND user_id = %s", (order_id, user_id))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def get_all_orders(limit: int = 100) -> List[Dict[str, Any]]:
    """Получение всех заказов"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT %s", (limit,))
        return [dict(row) for row in cursor.fetchall()]


def get_user_orders(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Получение заказов пользователя"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM orders 
            WHERE user_id = %s 
            ORDER BY created_at DESC 
            LIMIT %s
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_orders_by_base_id(base_order_id: str) -> List[Dict[str, Any]]:
    """Получение всех под-заказов по базовому ID"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM orders 
            WHERE base_order_id = %s OR order_id = %s
            ORDER BY order_id
        """, (base_order_id, base_order_id))
        return [dict(row) for row in cursor.fetchall()]


def save_client_notification(base_order_id: str, user_id: int, message_id: int):
    """Сохранение ID сообщения клиенту"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO client_notifications 
            (base_order_id, user_id, message_id, created_at)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                message_id = VALUES(message_id),
                created_at = VALUES(created_at)
        """, (base_order_id, user_id, message_id, datetime.now()))
        conn.commit()


def get_client_notification(base_order_id: str) -> Optional[Dict[str, Any]]:
    """Получение ID сообщения клиента"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM client_notifications 
            WHERE base_order_id = %s
        """, (base_order_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def build_grouped_status_message(base_order_id: str, lang: str = "ru") -> str:
    """Создает сводное сообщение о статусе всех категорий заказа"""

    # Получаем все под-заказы
    sub_orders = get_orders_by_base_id(base_order_id)

    if not sub_orders:
        return ""

    # Группируем по категориям
    categories_info = {}
    total_sum = 0
    total_items = 0

    for order in sub_orders:
        category = order.get("category")
        status = order.get("status", OrderStatus.PENDING)
        total_sum += order.get("total", 0)

        # Получаем количество товаров из order_json
        order_json_str = order.get("order_json", "{}")
        try:
            order_json = json.loads(order_json_str)
            items = order_json.get("items", [])
            item_count = len(items)
            total_items += item_count
        except:
            item_count = 0

        if category:
            categories_info[category] = {
                "status": status,
                "item_count": item_count,
                "sum": order.get("total", 0)
            }

    # Строим сообщение
    if lang == "ru":
        text = f"📦 Заказ №{base_order_id}\n\n"
        text += "📊 Статус по категориям:\n\n"

        for category, info in sorted(categories_info.items()):
            emoji = get_category_emoji(category)
            cat_name = get_category_name(category)
            status_name = STATUS_NAMES_RU.get(info["status"], info["status"])
            item_count = info["item_count"]
            text += f"{emoji} {cat_name}\n"
            text += f"{status_name}\n"
            text += f"Товаров: {item_count} | Сумма: {format_currency(info['sum'])}\n\n"

        text += f"━━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"📦 Всего товаров: {total_items}\n"
        text += f"💰 Общая сумма: {format_currency(total_sum)}"
    else:
        text = f"📦 Buyurtma №{base_order_id}\n\n"
        text += "📊 Kategoriyalar bo'yicha holat:\n\n"

        for category, info in sorted(categories_info.items()):
            emoji = get_category_emoji(category)
            cat_name = get_category_name(category)
            status_name = STATUS_NAMES_UZ.get(info["status"], info["status"])
            item_count = info["item_count"]
            text += f"{emoji} {cat_name}\n"
            text += f"{status_name}\n"
            text += f"Mahsulotlar: {item_count} | Summa: {format_currency(info['sum'])}\n\n"

        text += f"━━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"📦 Jami mahsulotlar: {total_items}\n"
        text += f"💰 Umumiy summa: {format_currency(total_sum)}"

    return text


async def send_or_update_client_notification(base_order_id: str, user_id: int, lang: str = "ru"):
    """Отправляет или обновляет сводное сообщение клиенту"""

    # Получаем текст сообщения
    message_text = build_grouped_status_message(base_order_id, lang)

    if not message_text:
        return

    # Проверяем, есть ли уже сообщение
    notification = get_client_notification(base_order_id)

    try:
        if notification:
            # Обновляем существующее сообщение
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=notification["message_id"],
                text=message_text
            )
            logger.info(f"Updated client notification for order {base_order_id}")
        else:
            # Отправляем новое сообщение
            sent_message = await bot.send_message(
                chat_id=user_id,
                text=message_text
            )
            # Сохраняем message_id
            save_client_notification(base_order_id, user_id, sent_message.message_id)
            logger.info(f"Sent new client notification for order {base_order_id}")

    except Exception as e:
        logger.exception(f"Failed to send/update client notification for order {base_order_id}")


async def send_category_completion_notification(order_id: str, category: str, user_id: int, lang: str = "ru"):
    """Отправляет отдельное уведомление о готовности конкретной категории"""

    order_data = get_order_raw(order_id)
    if not order_data:
        return

    emoji = get_category_emoji(category)
    cat_name = get_category_name(category)

    # Получаем информацию о товарах
    order_json_str = order_data.get("order_json", "{}")
    try:
        order_json = json.loads(order_json_str)
        items = order_json.get("items", [])
        item_count = len(items)
    except:
        item_count = 0

    if lang == "ru":
        text = (
            f"✅ Отличные новости!\n\n"
            f"{emoji} <b>{cat_name}</b>\n"
            f"Заказ №{order_id}\n\n"
            f"🎉 Полностью готов и ожидает на складе!\n\n"
            f"📦 Товаров: {item_count}\n"
            f"💰 Сумма: {format_currency(order_data.get('total', 0))}\n\n"

        )
    else:
        text = (
            f"✅ Ajoyib yangilik!\n\n"
            f"{emoji} <b>{cat_name}</b>\n"
            f"Buyurtma №{order_id}\n\n"
            f"🎉 To'liq tayyor va omborda kutmoqda!\n\n"
            f"📦 Mahsulotlar: {item_count}\n"
            f"💰 Summa: {format_currency(order_data.get('total', 0))}\n\n"

        )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML"
        )
        logger.info(f"Sent category completion notification for order {order_id}, category {category}")
    except Exception as e:
        logger.exception(f"Failed to send category completion notification for order {order_id}")


# ==================== ПОЛЬЗОВАТЕЛИ ====================

def add_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    """Добавление/обновление пользователя в базе данных"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Проверяем, существует ли пользователь
            cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
            exists = cursor.fetchone()
            
            if exists:
                # Обновляем last_activity
                cursor.execute("""
                    UPDATE users 
                    SET last_activity = %s, username = %s, first_name = %s, last_name = %s
                    WHERE user_id = %s
                """, (datetime.now(), username, first_name, last_name, user_id))
            else:
                # Создаем нового пользователя
                cursor.execute("""
                    INSERT INTO users (user_id, username, first_name, last_name, created_at, last_activity, language)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (user_id, username, first_name, last_name, datetime.now(), datetime.now(), 'ru'))
            
            conn.commit()
            logger.info(f"User {user_id} added/updated in database")
    except Exception as e:
        logger.exception(f"Error adding user {user_id} to database")


def get_all_user_ids() -> List[int]:
    """Получение всех ID пользователей из базы данных"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users ORDER BY created_at DESC")
            return [row['user_id'] for row in cursor.fetchall()]
    except Exception as e:
        logger.exception("Error reading users from database")
        return []


# ==================== ЯЗЫК ====================

def get_user_lang(user_id: int) -> str:
    """Получение языка пользователя из базы данных"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT language FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row:
                return row['language'] or 'ru'
            return 'ru'
    except Exception as e:
        logger.exception(f"Error getting language for user {user_id}")
        return 'ru'


def set_user_lang(user_id: int, lang: str):
    """Установка языка пользователя в базе данных"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET language = %s 
                WHERE user_id = %s
            """, (lang, user_id))
            conn.commit()
            logger.info(f"Language for user {user_id} set to {lang}")
    except Exception as e:
        logger.exception(f"Error saving language for user {user_id}")


# ==================== ПРОФИЛЬ ====================
async def check_dealer_status(user_id: int, phone: str, force_check: bool = False) -> dict:
    """Проверка статуса дилера через базу данных (не Google Sheets)"""
    if not force_check and user_id in dealer_cache:
        cached = dealer_cache[user_id]
        if (datetime.now() - cached.get("last_check", datetime.min)).total_seconds() < DEALER_CHECK_INTERVAL:
            return cached

    result = check_dealer_in_db(user_id, phone)
    dealer_cache[user_id] = result

    if not result.get("is_active"):
        dealer_block_time[user_id] = datetime.now()

    return result

def is_dealer_active(user_id: int) -> bool:
    # если ещё не проверяли дилера — считаем активным
    if user_id not in dealer_cache:
        return True
    return dealer_cache[user_id].get("is_active", True)


def is_known_dealer(user_id: int) -> bool:
    # если ещё не проверяли дилера — не меняем прежнее поведение меню
    if user_id not in dealer_cache:
        return True
    return dealer_cache[user_id].get("is_dealer", False)



def get_main_menu_keyboard(user_id: int, lang: str):
    if not is_known_dealer(user_id):
        catalogue_text = "📖 Каталог" if lang == "ru" else "📖 Katalog"
        settings_text = "⚙️ Настройки" if lang == "ru" else "⚙️ Sozlamalar"

        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text=catalogue_text,
                        web_app=WebAppInfo(url=WEBAPP_CATALOGUE_URL)
                    )
                ],
                [KeyboardButton(text=settings_text)]
            ],
            resize_keyboard=True
        )

    # ❌ дилер не активен — без WebApp
    if not is_dealer_active(user_id):
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⚙️ Настройки" if lang == "ru" else "⚙️ Sozlamalar")]],
            resize_keyboard=True
        )

    # ⏳ проверка таймера WebApp
    if is_webapp_button_active(user_id):

        order_text = "🛒 Сделать заказ" if lang == "ru" else "🛒 Buyurtma berish"

        buttons = [
            [
                KeyboardButton(
                    text=order_text,
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ]

        # ✅ Кнопка анализа только для супер-админа
        if user_id == SUPER_ADMIN_ID:
            buttons.append([
                KeyboardButton(
                    text="📊 Анализ" if lang == "ru" else "📊 Tahlil",
                    web_app=WebAppInfo(url=URL_ANALYSIS)
                )
            ])

        return ReplyKeyboardMarkup(
            keyboard=buttons + [
                [
                    KeyboardButton(
                        text="📋 Мои заказы" if lang == "ru" else "📋 Mening buyurtmalarim"
                    ),
                    KeyboardButton(
                        text="⚙️ Настройки" if lang == "ru" else "⚙️ Sozlamalar"
                    )
                ]
            ],
            resize_keyboard=True
        )

    else:
        menu_text = "🏠 Главный меню" if lang == "ru" else "🏠 Bosh menyu"

        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=menu_text)],
                [
                    KeyboardButton(
                        text="📋 Мои заказы" if lang == "ru" else "📋 Mening buyurtmalarim"
                    ),
                    KeyboardButton(
                        text="⚙️ Настройки" if lang == "ru" else "⚙️ Sozlamalar"
                    )
                ]
            ],
            resize_keyboard=True
        )


def get_user_profile(user_id: int) -> Dict[str, str]:
    """Получение профиля пользователя из базы данных"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT phone, city, full_name, latitude, longitude 
                FROM users 
                WHERE user_id = %s
            """, (user_id,))
            row = cursor.fetchone()
            
            if row:
                profile = {}
                if row['phone']:
                    profile['phone'] = row['phone']
                if row['city']:
                    profile['city'] = row['city']
                if row['full_name']:
                    profile['full_name'] = row['full_name']
                if row['latitude']:
                    profile['latitude'] = float(row['latitude'])
                if row['longitude']:
                    profile['longitude'] = float(row['longitude'])
                return profile
            return {}
    except Exception as e:
        logger.exception(f"Error getting profile for user {user_id}")
        return {}


def set_user_profile(user_id: int, profile: Dict[str, str]):
    """Сохранение профиля пользователя в базе данных"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Получаем данные из профиля
            phone = profile.get('phone')
            city = profile.get('city')
            full_name = profile.get('full_name')
            latitude = profile.get('latitude')
            longitude = profile.get('longitude')
            
            # Обновляем профиль пользователя
            cursor.execute("""
                UPDATE users 
                SET phone = %s, city = %s, full_name = %s, latitude = %s, longitude = %s
                WHERE user_id = %s
            """, (phone, city, full_name, latitude, longitude, user_id))
            
            conn.commit()
            logger.info(f"Profile for user {user_id} updated in database")
    except Exception as e:
        logger.exception(f"Error saving profile for user {user_id}")


def get_user_full_name(user_id: int) -> Optional[str]:
    """Получение полного имени пользователя из профиля"""
    profile = get_user_profile(user_id)
    return profile.get("full_name")


def get_user_info(user_id: int) -> Optional[Dict[str, Any]]:
    """Получение полной информации о пользователе"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, username, first_name, last_name, language, 
                       phone, city, full_name, latitude, longitude, 
                       created_at, last_activity
                FROM users 
                WHERE user_id = %s
            """, (user_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    except Exception as e:
        logger.exception(f"Error getting user info for {user_id}")
        return None


def get_users_stats() -> Dict[str, Any]:
    """Получение статистики пользователей"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Общее количество пользователей
            cursor.execute("SELECT COUNT(*) as total FROM users")
            total = cursor.fetchone()['total']
            
            # Активные пользователи (за последние 30 дней)
            cursor.execute("""
                SELECT COUNT(*) as active 
                FROM users 
                WHERE last_activity >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            """)
            active = cursor.fetchone()['active']
            
            # Новые пользователи (за последние 7 дней)
            cursor.execute("""
                SELECT COUNT(*) as new_users 
                FROM users 
                WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
            """)
            new_users = cursor.fetchone()['new_users']
            
            return {
                'total': total,
                'active_30d': active,
                'new_7d': new_users
            }
    except Exception as e:
        logger.exception("Error getting users stats")
        return {'total': 0, 'active_30d': 0, 'new_7d': 0}

# ==================== ДИЛЕРЫ (DATABASE) ====================

def check_dealer_in_db(user_id: int, phone: str) -> dict:
    """Проверка дилера по telegram_id или номеру телефона в БД"""
    try:
        clean_phone = re.sub(r'\D', '', phone) if phone else ''
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Ищем по telegram_id или телефону
            cursor.execute("""
                SELECT telegram_id, phone, name, status
                FROM dealers
                WHERE telegram_id = %s OR phone = %s OR phone = %s
                LIMIT 1
            """, (user_id, phone, clean_phone))
            row = cursor.fetchone()
            if row:
                is_active = row['status'] == 'active'
                return {
                    "is_dealer": True,
                    "is_active": is_active,
                    "status": row['status'],
                    "name": row['name'],
                    "last_check": datetime.now()
                }
            return {
                "is_dealer": False,
                "is_active": False,
                "status": "not_found",
                "last_check": datetime.now()
            }
    except Exception as e:
        logger.exception(f"Error checking dealer in DB for user {user_id}")
        return {"is_dealer": False, "is_active": False, "status": "error"}


def add_dealer_to_db(telegram_id: int, phone: str, name: str, status: str = 'active') -> bool:
    """Добавить или обновить дилера в БД"""
    try:
        clean_phone = re.sub(r'\D', '', phone) if phone else phone
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO dealers (telegram_id, phone, name, status, added_at)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    phone = VALUES(phone),
                    name = VALUES(name),
                    status = VALUES(status)
            """, (telegram_id or None, clean_phone, name, status, datetime.now()))
            conn.commit()
            return True
    except Exception as e:
        logger.exception(f"Error adding dealer to DB")
        return False


def remove_dealer_from_db(telegram_id: int) -> bool:
    """Удалить дилера из БД по telegram_id"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM dealers WHERE telegram_id = %s", (telegram_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.exception(f"Error removing dealer from DB")
        return False


def set_dealer_status_in_db(telegram_id: int, status: str) -> bool:
    """Изменить статус дилера (active/inactive)"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE dealers SET status = %s WHERE telegram_id = %s",
                (status, telegram_id)
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.exception(f"Error updating dealer status in DB")
        return False


def get_all_dealers_from_db() -> list:
    """Получить список всех дилеров из БД"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT telegram_id, phone, name, status, added_at
                FROM dealers
                ORDER BY added_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.exception("Error getting dealers from DB")
        return []

# ==================== FTP ====================

try:
    import aioftp

    AIOFTP_AVAILABLE = True
except ImportError:
    AIOFTP_AVAILABLE = False
    logger.warning("aioftp not available, using sync FTP")


async def upload_pdf_to_hosting_async(order_id: str, pdf_bytes: bytes) -> tuple[bool, str]:
    """Асинхронная загрузка PDF на хостинг"""
    filename = f"order_{order_id}.pdf"

    if not HOSTING_FTP_HOST:
        logger.warning("FTP host not configured")
        return False, ""

    if AIOFTP_AVAILABLE:
        try:
            async with aioftp.Client.context(
                    HOSTING_FTP_HOST,
                    user=HOSTING_FTP_USER,
                    password=HOSTING_FTP_PASS,
                    socket_timeout=FTP_TIMEOUT
            ) as client:
                if HOSTING_FTP_DIR:
                    await client.change_directory(HOSTING_FTP_DIR)

                await client.upload(
                    io.BytesIO(pdf_bytes),
                    filename,
                    write_into=True
                )

                url = f"{HOSTING_BASE_URL}/{filename}"
                logger.info(f"PDF uploaded successfully: {url}")
                return True, url

        except Exception as e:
            logger.exception(f"Error uploading PDF to FTP")
            return False, ""
    else:
        return await asyncio.to_thread(_upload_pdf_sync, order_id, pdf_bytes)


def _upload_pdf_sync(order_id: str, pdf_bytes: bytes) -> tuple[bool, str]:
    """Синхронная загрузка PDF на хостинг"""
    filename = f"order_{order_id}.pdf"

    try:
        ftp = FTP(timeout=FTP_TIMEOUT)
        ftp.connect(HOSTING_FTP_HOST)
        ftp.login(HOSTING_FTP_USER, HOSTING_FTP_PASS)

        if HOSTING_FTP_DIR:
            ftp.cwd(HOSTING_FTP_DIR)

        ftp.storbinary(f"STOR {filename}", io.BytesIO(pdf_bytes))
        ftp.quit()

        url = f"{HOSTING_BASE_URL}/{filename}"
        logger.info(f"PDF uploaded successfully (sync): {url}")
        return True, url

    except Exception as e:
        logger.exception(f"Error uploading PDF to FTP (sync)")
        return False, ""


# ==================== PDF ГЕНЕРАЦИЯ ====================

def format_currency(value: int) -> str:
    """Форматирует сумму с пробелами"""
    try:
        s = str(int(value))
    except:
        s = "0"
    parts = []
    while s:
        parts.insert(0, s[-3:])
        s = s[:-3]
    return " ".join(parts) + " so'm"


def wrap_text(text: str, max_chars: int):
    """Переносит длинный текст"""
    if not text:
        return [""]
    wrapper = textwrap.TextWrapper(
        width=max_chars,
        break_long_words=True,
        replace_whitespace=False
    )
    return wrapper.wrap(text)


async def download_image_async(url: str, timeout: int = 10) -> Optional[Image.Image]:
    """Асинхронная загрузка изображения с кешированием"""
    global image_cache, image_cache_timestamp
    
    # Проверяем кеш
    if url in image_cache:
        cache_age = (datetime.now() - image_cache_timestamp.get(url, datetime.now())).total_seconds()
        if cache_age < IMAGE_CACHE_LIFETIME:
            logger.debug(f"Image cache HIT: {url}")
            return image_cache[url]
    
    try:
        loop = asyncio.get_event_loop()
        
        def _download():
            try:
                response = urlopen(url, timeout=timeout)
                image_data = response.read()
                img = Image.open(io.BytesIO(image_data))
                img.thumbnail((150, 150), Image.LANCZOS)
                img = img.convert("RGB")
                return img
            except Exception as e:
                logger.warning(f"Failed to download image from {url}: {e}")
                return None
        
        image = await loop.run_in_executor(image_download_executor, _download)
        
        if image:
            image_cache[url] = image
            image_cache_timestamp[url] = datetime.now()
            logger.debug(f"Image downloaded and cached: {url}")
        
        return image
    except Exception as e:
        logger.warning(f"Error downloading image async: {e}")
        return None


# Оставляем старую функцию для совместимости
def download_image(url: str, timeout: int = 10) -> Optional[Image.Image]:
    """Синхронная версия (deprecated)"""
    try:
        response = urlopen(url, timeout=timeout)
        image_data = response.read()
        img = Image.open(io.BytesIO(image_data))
        img.thumbnail((150, 150), Image.LANCZOS)
        img = img.convert("RGB")
        return img
    except (URLError, HTTPError, Exception) as e:
        logger.warning(f"Failed to download image from {url}: {e}")
        return None


async def preload_order_images(order_items: list) -> Dict[str, Image.Image]:
    image_urls = []
    for item in order_items:
        image_url = item.get("image", "")
        if image_url and image_url not in image_urls:
            image_urls.append(image_url)

    if not image_urls:
        return {}

    logger.info(f"⚡ Preloading {len(image_urls)} unique images in batches...")
    result = {}
    batch_size = 10

    for i in range(0, len(image_urls), batch_size):
        batch = image_urls[i:i+batch_size]
        tasks = [download_image_async(url, timeout=5) for url in batch]
        images = await asyncio.gather(*tasks, return_exceptions=True)
        for url, image in zip(batch, images):
            if image and not isinstance(image, Exception):
                result[url] = image

    logger.info(f"✅ Preloaded {len(result)} images successfully")
    return result

def generate_order_pdf(
    order_items: list,
    total: int,
    client_name: str,
    admin_name: str,
    order_id: str,
    approved: bool = False,
    category: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    preloaded_images: Optional[Dict[str, Image.Image]] = None  # ✅ НОВЫЙ ПАРАМЕТР
) -> bytes:
    """Генерирует PDF заказа с фотографиями товаров"""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    left_margin = 15 * mm
    right_margin = 15 * mm
    top_margin = 18 * mm
    bottom_margin = 18 * mm
    usable_width = width - left_margin - right_margin

    # ✅ ОБНОВЛЁННЫЕ КОЛОНКИ: №, Фото, ID, Наименование, Кол-во, Вес, Куб, Цена, Сумма
    col_num_w = usable_width * 0.04  # № (номер)
    col_image_w = 20 * mm  # Фото (уменьшено)
    col_id_w = usable_width * 0.07  # ID
    col_name_w = usable_width * 0.22  # Наименование (уменьшено)
    col_qty_w = usable_width * 0.08  # Кол-во
    col_weight_w = usable_width * 0.09  # Вес
    col_cube_w = usable_width * 0.09  # Куб
    col_price_w = usable_width * 0.13  # Цена
    col_sum_w = usable_width * 0.14  # Сумма

    header_font = "DejaVu" if "DejaVu" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    main_font = header_font
    signature_font = "Betmo" if "Betmo" in pdfmetrics.getRegisteredFontNames() else header_font

    y = height - top_margin
    page_number = 1

    # QR код
    pdf_url = f"{HOSTING_BASE_URL}/{order_id}.pdf"
    try:
        qr = qrcode.QRCode(version=2, box_size=6, border=2)
        qr.add_data(pdf_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_buf = io.BytesIO()
        qr_img.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        qr_reader = ImageReader(qr_buf)
        qr_size = 28 * mm
    except:
        qr_reader = None
        qr_size = 0

    def draw_header():
        nonlocal y
        try:
            if os.path.exists("logo.png"):
                logo = ImageReader("logo.png")
                logo_h = 12 * mm
                c.drawImage(
                    logo,
                    left_margin,
                    height - top_margin - logo_h + 6 * mm,
                    width=logo_h,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto"
                )
        except:
            pass

        c.setFont(header_font, 14)
        c.drawString(left_margin + 18 * mm, height - top_margin - 2 * mm, "Buyurtma / Заказ")
        c.setFont(header_font, 9)
        c.drawRightString(width - right_margin, height - top_margin + 4 * mm, f"№ {order_id}")
        c.setFont(main_font, 9)
        c.drawString(left_margin, height - top_margin - 10 * mm, f"Клиент: {client_name}")

        # Добавляем категорию если она указана
        current_y_offset = 16 * mm
        if category:
            category_name = get_category_name(category)
            c.setFont(main_font, 9)
            c.setFillColor(colors.Color(0 / 255, 88 / 255, 204 / 255))
            c.drawString(left_margin, height - top_margin - current_y_offset, f"Категория: {category_name}")
            c.setFillColor(colors.black)
            current_y_offset += 6 * mm

        # Добавляем координаты если они указаны
        if latitude is not None and longitude is not None:
            c.setFont(main_font, 9)
            c.setFillColor(colors.Color(100 / 255, 100 / 255, 100 / 255))
            c.drawString(left_margin, height - top_margin - current_y_offset,
                         f"📍 Координаты: {latitude:.6f}, {longitude:.6f}")
            c.setFillColor(colors.black)
            current_y_offset += 6 * mm

        y = height - top_margin - current_y_offset

        c.drawRightString(width - right_margin, height - top_margin - 10 * mm,
                          datetime.now().strftime("%d.%m.%Y %H:%M"))

    def draw_footer():
        c.setFont(main_font, 8)
        footer_text = f" "
        x = left_margin
        y_footer = bottom_margin - 6 * mm
        c.drawString(x, y_footer, footer_text)
        try:
            c.linkURL(pdf_url, (x, y_footer - 1 * mm, x + c.stringWidth(footer_text, main_font, 8), y_footer + 6),
                      relative=0)
        except:
            pass
        c.drawRightString(width - right_margin - (qr_size + 4 * mm if qr_reader else 0), y_footer,
                          f"Страница {page_number}")

        if qr_reader:
            qr_x = width - right_margin - qr_size
            qr_y = bottom_margin
            try:
                c.drawImage(qr_reader, qr_x, qr_y, width=qr_size, height=qr_size, preserveAspectRatio=True, mask="auto")
            except:
                pass

    def new_page():
        nonlocal y, page_number
        draw_footer()
        c.showPage()
        page_number += 1
        draw_header()

    # Первая страница
    draw_header()

    # Таблица
    c.setFont(main_font, 10)
    table_x = left_margin
    c.setFillColor(colors.black)
    c.drawString(table_x, y, "Товары / Mahsulotlar")
    y -= 6 * mm

    # ✅ НОВЫЕ ЗАГОЛОВКИ: №, Фото, ID, Наименование, Кол-во, Вес, Куб, Цена, Сумма
    c.setFont(main_font, 7)  # Уменьшенный шрифт для заголовков
    header_y = y

    c.drawString(table_x, header_y, "№")
    c.drawString(table_x + col_num_w, header_y, "Фото")
    c.drawString(table_x + col_num_w + col_image_w, header_y, "ID")
    c.drawString(table_x + col_num_w + col_image_w + col_id_w, header_y, "Наименование")
    c.drawRightString(table_x + col_num_w + col_image_w + col_id_w + col_name_w + col_qty_w, header_y, "Кол-во")
    c.drawRightString(table_x + col_num_w + col_image_w + col_id_w + col_name_w + col_qty_w + col_weight_w, header_y,
                      "Вес")
    c.drawRightString(table_x + col_num_w + col_image_w + col_id_w + col_name_w + col_qty_w + col_weight_w + col_cube_w,
                      header_y, "Куб")
    c.drawRightString(
        table_x + col_num_w + col_image_w + col_id_w + col_name_w + col_qty_w + col_weight_w + col_cube_w + col_price_w,
        header_y, "Цена")
    c.drawRightString(
        table_x + col_num_w + col_image_w + col_id_w + col_name_w + col_qty_w + col_weight_w + col_cube_w + col_price_w + col_sum_w,
        header_y, "Сумма")


    y -= 5 * mm
    c.line(table_x, y + 3 * mm, width - right_margin, y + 3 * mm)
    y -= 4 * mm

    c.setFont(main_font, 7)  # Уменьшенный шрифт для содержимого
    line_height = 5.5 * mm
    max_name_chars = 18  # Уменьшено из-за дополнительных колонок

    # ✅ ПЕРЕМЕННЫЕ ДЛЯ ИТОГОВ
    total_weight = 0.0
    total_cube = 0.0
    item_number = 1  # Счётчик для нумерации товаров


    for item in order_items:
        name_raw = str(item.get("name", "Неизвестно"))
        qty = int(item.get("qty", 0) or 0)
        price = int(item.get("price", 0) or 0)
        image_url = item.get("image", "")  # НОВОЕ: Получаем URL изображения
        product_id = str(item.get("id", ""))  # ДОБАВЛЕНО: Получаем ID продукта
        weight = float(item.get("weight", 0) or 0)  # вес одной единицы
        cube = float(item.get("cube", 0) or 0)  # куб одной единицы

        if qty <= 0 and price == 0:
            continue

        sum_item = qty * price

        # ✅ ВЫЧИСЛЯЕМ ИТОГОВЫЕ ВЕС И КУБ ДЛЯ ЭТОЙ ПОЗИЦИИ
        item_total_weight = weight * qty
        item_total_cube = cube * qty

        # ✅ НАКАПЛИВАЕМ ОБЩИЕ ИТОГИ
        total_weight += item_total_weight
        total_cube += item_total_cube

        name_lines = wrap_text(name_raw, max_name_chars)

        # НОВОЕ: Определяем высоту с учетом изображения
        image_height = 18 * mm if image_url else 0
        text_height = line_height * max(1, len(name_lines))
        needed_height = max(image_height, text_height)

        if y - needed_height < bottom_margin + 30 * mm:
            new_page()

        # Центр строки
        row_center_y = y - (needed_height / 2)

        # ✅ РИСУЕМ НОМЕР СТРОКИ
        c.drawString(table_x, row_center_y - 1 * mm, str(item_number))
        item_number += 1

        # ✅ РИСУЕМ ИЗОБРАЖЕНИЕ ТОВАРА
               # ✅ РИСУЕМ ИЗОБРАЖЕНИЕ ТОВАРА
        if image_url:
            try:
                product_image = None

                if preloaded_images and image_url in preloaded_images:
                    product_image = preloaded_images[image_url]
                    logger.debug("Using preloaded image")
                else:
                    product_image = download_image(image_url, timeout=5)

                if product_image:
                    # Конвертируем в RGB если необходимо
                    if product_image.mode != "RGB":
                        product_image = product_image.convert("RGB")

                    # Создаем ImageReader из PIL Image
                    img_buffer = io.BytesIO()
                    product_image.save(img_buffer, format="JPEG")
                    img_buffer.seek(0)
                    img_reader = ImageReader(img_buffer)

                    # Рисуем изображение с центрированием по вертикали
                    img_size = 16 * mm
                    img_x = table_x + col_num_w + 1 * mm
                    img_y = row_center_y - (img_size / 2)

                    c.drawImage(
                        img_reader,
                        img_x,
                        img_y,
                        width=img_size,
                        height=img_size,
                        preserveAspectRatio=True,
                        mask="auto"
                    )

            except Exception as e:
                logger.warning(f"Could not add image to PDF: {e}")


        # ✅ РИСУЕМ ID ПРОДУКТА
        if product_id:
            id_x = table_x + col_num_w + col_image_w
            c.setFont(main_font, 7)
            c.drawString(id_x, row_center_y - 1 * mm, product_id)

        # ✅ РИСУЕМ НАЗВАНИЕ ТОВАРА
        name_x = table_x + col_num_w + col_image_w + col_id_w
        total_text_height = line_height * len(name_lines)
        text_start_y = row_center_y + (total_text_height / 2) - (line_height / 2)

        cur_y = text_start_y
        for ln in name_lines:
            c.drawString(name_x, cur_y, ln)
            cur_y -= line_height

        # ✅ РИСУЕМ КОЛИЧЕСТВО, ВЕС, КУБ, ЦЕНУ И СУММУ
        qty_x = table_x + col_num_w + col_image_w + col_id_w + col_name_w
        weight_x = qty_x + col_qty_w
        cube_x = weight_x + col_weight_w
        price_x = cube_x + col_cube_w
        sum_x = price_x + col_price_w

        numbers_y = row_center_y - 1 * mm
        c.drawRightString(qty_x + col_qty_w - 2 * mm, numbers_y, str(qty))
        c.drawRightString(weight_x + col_weight_w - 2 * mm, numbers_y, f"{item_total_weight:.2f}")
        c.drawRightString(cube_x + col_cube_w - 2 * mm, numbers_y, f"{item_total_cube:.4f}")
        c.drawRightString(price_x + col_price_w - 2 * mm, numbers_y, format_currency(price))
        c.drawRightString(sum_x + col_sum_w - 2 * mm, numbers_y, format_currency(sum_item))

        y = y - needed_height - (2 * mm)

    # Итог
    # ✅ ИТОГИ: Общий вес, общий куб и общая сумма
    if y - 25 * mm < bottom_margin:
        new_page()

    y -= 4 * mm
    c.line(table_x, y + 3 * mm, width - right_margin, y + 3 * mm)
    y -= 6 * mm

    c.setFont(main_font, 10)

    # Выводим общий вес
    c.drawRightString(width - right_margin - (qr_size + 4 * mm if qr_reader else 0), y,
                      f"Общий вес: {total_weight:.2f} кг")
    y -= 6 * mm

    # Выводим общий куб
    c.drawRightString(width - right_margin - (qr_size + 4 * mm if qr_reader else 0), y,
                      f"Общий куб: {total_cube:.4f} м³")
    y -= 6 * mm

    # Выводим общую сумму
    c.drawRightString(width - right_margin - (qr_size + 4 * mm if qr_reader else 0), y,
                      f"Итого: {format_currency(total)}")
    y -= 12 * mm

    # Подпись клиента
    c.setFont(main_font, 9)
    c.drawString(left_margin, y, "Подпись клиента / Mijoz imzosi :")
    y -= 10 * mm
    sig_line_x1 = left_margin
    sig_line_x2 = left_margin + 65 * mm
    c.line(sig_line_x1, y, sig_line_x2, y)

    sig_font_size = 26
    max_sig_width = sig_line_x2 - sig_line_x1
    while sig_font_size > 8 and c.stringWidth(client_name, signature_font, sig_font_size) > max_sig_width:
        sig_font_size -= 2
    c.setFont(signature_font, sig_font_size)
    c.setFillColor(colors.Color(0 / 255, 88 / 255, 204 / 255))
    name_width = c.stringWidth(client_name, signature_font, sig_font_size)
    name_x = sig_line_x1 + (max_sig_width - name_width) / 2 if name_width < max_sig_width else sig_line_x1
    c.drawString(name_x, y + 3 * mm, client_name)
    c.setFillColor(colors.black)
    c.setFont(main_font, 9)
    y -= 24 * mm



    # Штамп
    if approved:
        try:
            if os.path.exists("stamp.png"):
                stamp = ImageReader("stamp.png")
                stamp_w = 30 * mm
                stamp_h = 30 * mm
                c.drawImage(stamp, width - right_margin - stamp_w, y - 6 * mm, width=stamp_w, height=stamp_h,
                            preserveAspectRatio=True, mask="auto")
        except:
            pass

        c.setFont(main_font, 11)
        c.setFillColor(colors.green)
        c.drawString(left_margin, bottom_margin + 20 * mm, "☑")
        c.setFillColor(colors.black)
    else:
        # DRAFT watermark
        c.saveState()
        c.setFont(main_font, 48)
        c.setFillColor(colors.Color(0.8, 0.8, 0.8, alpha=0.35))
        c.translate(width / 2, height / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, "")
        c.restoreState()

    draw_footer()
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


# ==================== FSM СОСТОЯНИЯ ====================

class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_city = State()
    waiting_for_location = State()
    waiting_for_full_name = State()


class OrderSign(StatesGroup):
    waiting_name = State()


# ==================== РЕГИСТРАЦИЯ ШРИФТОВ ====================

try:
    pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
except Exception as e:
    logging.warning(f"Cannot register DejaVu font: {e}")

try:
    pdfmetrics.registerFont(TTFont("Betmo", "Betmo Cyr.otf"))
except Exception as e:
    logging.warning(f"Cannot register Betmo font: {e}")

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================

from aiogram.client.session.aiohttp import AiohttpSession
session = AiohttpSession(timeout=180)
bot = Bot(token=API_TOKEN, session=session)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# Добавляем middleware
dp.message.middleware(rate_limiter)
dp.message.middleware(WebAppTimerMiddleware())

# Регистрируем роутер
dp.include_router(router)


# ==================== КОМАНДЫ ====================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start с перепроверкой дилера"""

    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    # Регистрируем пользователя с полной информацией из Telegram
    add_user(user_id, username, first_name, last_name)

    # ===== ОБНОВЛЕНИЕ ТАЙМЕРА WEBAPP =====
    update_user_start_time(user_id)

    # ⏳ Авто-скрытие WebApp кнопки
    async def expire_webapp_keyboard():
        await asyncio.sleep(WEBAPP_BUTTON_TIMEOUT)
        await refresh_main_menu(user_id, state)

    asyncio.create_task(expire_webapp_keyboard())

    lang = get_user_lang(user_id)
    profile = get_user_profile(user_id)

    # ===== 1. ЕСЛИ ПОЛЬЗОВАТЕЛЬ НЕ ЗАРЕГИСТРИРОВАН =====
    if not profile or not all(k in profile for k in ["phone", "city", "full_name"]):
        if lang == "ru":
            text = "👋 Добро пожаловать!\n\nДля начала работы необходимо зарегистрироваться."
        else:
            text = "👋 Xush kelibsiz!\n\nIshni boshlash uchun ro'yxatdan o'tish kerak."

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📝 Регистрация" if lang == "ru" else "📝 Ro'yxatdan o'tish",
                        callback_data="register"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🇷🇺 Русский" if lang == "uz" else "🇺🇿 O'zbekcha",
                        callback_data="toggle_lang"
                    )
                ]
            ]
        )

        await message.answer(text, reply_markup=kb)
        return

    # ===== 2. ПЕРЕПРОВЕРКА СТАТУСА ДИЛЕРА (Google Sheets) =====
    dealer_status = await check_dealer_status(
        user_id=user_id,
        phone=profile.get("phone", "")
    )

    # ===== 3. ТЕКСТ ПРОФИЛЯ =====
    if lang == "ru":
        text = (
            f"Привет {profile['full_name']}!\n\n"
            f"Для оформления заказа нажмите\n"
            f"«🛒 Сделать заказ»\n"

        )
    else:
        text = (
            f"Salom {profile['full_name']}!\n\n"
            f"Buyurtma berish uchun «🛒 Buyurtma berish» tugmasini bosing\n"
            f"«🛒 Buyurtma berish» tugmasini bosing\n"

        )

    # ===== 4. ЕСЛИ НЕ АКТИВНЫЙ ДИЛЕР — ДОП. ПРЕДУПРЕЖДЕНИЕ =====
    if not dealer_status.get("is_active"):
        if dealer_status.get("is_dealer"):
            # Есть в списке, но статус не active
            if lang == "ru":
                text += (
                    "\n\n⚠️ ВНИМАНИЕ!\n"
                    f"Ваш статус: {dealer_status.get('status', 'неизвестно')}\n"
                    "Функция создания заказов временно недоступна."
                )
            else:
                text += (
                    "\n\n⚠️ DIQQAT!\n"
                    f"Sizning holatingiz: {dealer_status.get('status', 'nomaʼlum')}\n"
                    "Buyurtma yaratish funksiyasi vaqtincha mavjud emas."
                )
        else:
            # Вообще не дилер
            if lang == "ru":
                text += (
                    "\n\n⚠️ ВНИМАНИЕ!\n"
                    "Вы не найдены в списке дилеров.\n"
                    "Функция создания заказов недоступна.\n"
                    "Нажав кнопку «📖 Каталог», вы можете ознакомиться с нашей продукцией."
                )
            else:
                text += (
                    "\n\n⚠️ DIQQAT!\n"
                    "Siz dilerlar ro'yxatida topilmadingiz.\n"
                    "Buyurtma yaratish funksiyasi mavjud emas.\n\n "
                    "📖 Katalog tugmasini bosib, bizning mahsulotlarimiz bilan tanishishingiz mumkin."
                )

    # ===== 5. КЛАВИАТУРА В ЗАВИСИМОСТИ ОТ СТАТУСА =====
    kb = get_main_menu_keyboard(user_id, lang)

    sent = await message.answer(
        text,
        reply_markup=kb
    )

    # ✅ сохраняем message_id меню
    await state.update_data(menu_message_id=sent.message_id)


# ⛔ БЛОКИРОВКА УСТАРЕВШЕЙ КНОПКИ WEBAPP
@router.message(F.text == "🛒 Сделать заказ")
async def block_expired_webapp(message: Message):
    if not is_webapp_button_active(message.from_user.id):
        await message.answer(
            "⏰ Время для создания заказа истекло.\n"
            "Нажмите /start.",
            reply_markup=ReplyKeyboardRemove()
        )

@router.callback_query(F.data == "register")
async def callback_register(callback: CallbackQuery, state: FSMContext):
    """Начало регистрации"""
    lang = get_user_lang(callback.from_user.id)

    if lang == "ru":
        text = "📱 Поделитесь своим номером телефона:"
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    else:
        text = "📱 Telefon raqamingizni yuboring:"
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )

    await callback.message.answer(text, reply_markup=kb)
    await state.set_state(RegistrationStates.waiting_for_phone)
    await callback.answer()


@router.callback_query(F.data == "toggle_lang")
async def callback_toggle_lang(callback: CallbackQuery):
    """Переключение языка"""
    user_id = callback.from_user.id
    current_lang = get_user_lang(user_id)
    new_lang = "uz" if current_lang == "ru" else "ru"
    set_user_lang(user_id, new_lang)

    if new_lang == "ru":
        text = "🇷🇺 Язык изменён на русский"
    else:
        text = "🇺🇿 Til o'zbek tiliga o'zgartirildi"

    await callback.answer(text, show_alert=True)

    # Обновляем меню
    profile = get_user_profile(user_id)
    if not profile or not all(k in profile for k in ["phone", "city", "full_name"]):
        if new_lang == "ru":
            text = "👋 Добро пожаловать! Для начала работы необходимо зарегистрироваться."
        else:
            text = "👋 Xush kelibsiz! Ishni boshlash uchun ro'yxatdan o'tish kerak."

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📝 Регистрация" if new_lang == "ru" else "📝 Ro'yxatdan o'tish",
                callback_data="register"
            )],
            [InlineKeyboardButton(
                text="🇷🇺 Русский" if new_lang == "uz" else "🇺🇿 O'zbekcha",
                callback_data="toggle_lang"
            )]
        ])

        await callback.message.edit_text(text, reply_markup=kb)


@router.message(RegistrationStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    """Обработка номера телефона"""
    lang = get_user_lang(message.from_user.id)

    if not message.contact:
        if lang == "ru":
            await message.answer("Пожалуйста, используйте кнопку для отправки номера.")
        else:
            await message.answer("Iltimos, raqamni yuborish uchun tugmadan foydalaning.")
        return

    phone = message.contact.phone_number
    await state.update_data(phone=phone)

    if lang == "ru":
        text = "🏙 Введите ваш город:"
    else:
        text = "🏙 Shaharingizni kiriting:"

    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_city)


@router.message(RegistrationStates.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    """Обработка города"""
    lang = get_user_lang(message.from_user.id)
    city = message.text.strip()

    if not city:
        if lang == "ru":
            await message.answer("Пожалуйста, введите город.")
        else:
            await message.answer("Iltimos, shaharni kiriting.")
        return

    await state.update_data(city=city)

    # Запрос локации
    if lang == "ru":
        text = "📍 Теперь поделитесь своей геолокацией:"
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить локацию", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    else:
        text = "📍 Endi joylashuvingizni yuboring:"
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )

    await message.answer(text, reply_markup=kb)
    await state.set_state(RegistrationStates.waiting_for_location)


@router.message(RegistrationStates.waiting_for_location)
async def process_location(message: Message, state: FSMContext):
    """Обработка геолокации"""
    lang = get_user_lang(message.from_user.id)

    if not message.location:
        if lang == "ru":
            await message.answer("Пожалуйста, используйте кнопку для отправки локации.")
        else:
            await message.answer("Iltimos, joylashuvni yuborish uchun tugmadan foydalaning.")
        return

    latitude = message.location.latitude
    longitude = message.location.longitude

    await state.update_data(latitude=latitude, longitude=longitude)

    if lang == "ru":
        text = "👤 Введите ваше полное имя:"
    else:
        text = "👤 To'liq ismingizni kiriting:"

    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegistrationStates.waiting_for_full_name)


@router.message(RegistrationStates.waiting_for_full_name)
async def process_full_name(message: Message, state: FSMContext):
    """Обработка полного имени + проверка дилера"""

    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    full_name = message.text.strip()

    # Проверка имени
    if not full_name or len(full_name) < 2:
        if lang == "ru":
            await message.answer("Пожалуйста, введите корректное имя (минимум 2 символа).")
        else:
            await message.answer("Iltimos, to'g'ri ismni kiriting (kamida 2 ta belgi).")
        return

    # Данные из FSM
    data = await state.get_data()

    # Сохраняем профиль
    profile = {
        "phone": data["phone"],
        "city": data["city"],
        "full_name": full_name,
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude")
    }
    set_user_profile(user_id, profile)

    # 🔍 ПРОВЕРКА ДИЛЕРА ЧЕРЕЗ GOOGLE SHEETS
    dealer_status = await check_dealer_status(
        user_id=user_id,
        phone=data["phone"],
        force_check=True
    )

    # Текст регистрации
    if lang == "ru":
        text = (
            "✅ Регистрация завершена!\n\n"
            f"👤 {full_name}\n"
            f"📱 {data['phone']}\n"
            f"🏙 {data['city']}"
        )
    else:
        text = (
            "✅ Ro'yxatdan o'tish yakunlandi!\n\n"
            f"👤 {full_name}\n"
            f"📱 {data['phone']}\n"
            f"🏙 {data['city']}"
        )

    # Если НЕ активный дилер — добавляем предупреждение
    if not dealer_status.get("is_active"):
        if dealer_status.get("is_dealer"):
            # Есть в списке, но статус не active
            if lang == "ru":
                text += (
                    "\n\n⚠️ ВНИМАНИЕ!\n"
                    f"Ваш статус: {dealer_status.get('status', 'неизвестно')}\n"
                    "Функция создания заказов временно недоступна.\n\n"
                    "Для получения доступа свяжитесь с администратором."
                )
            else:
                text += (
                    "\n\n⚠️ DIQQAT!\n"
                    f"Sizning holatingiz: {dealer_status.get('status', 'nomaʼlum')}\n"
                    "Buyurtma yaratish funksiyasi vaqtincha mavjud emas.\n\n"
                    "Administrator bilan bogʻlaning."
                )
        else:
            # Вообще не найден в списке дилеров
            if lang == "ru":
                text += (
                    "\n\n⚠️ ВНИМАНИЕ!\n"
                    "Вы не найдены в списке дилеров.\n"
                    "Функция создания заказов недоступна.\n"
                    "Нажав кнопку «📖 Каталог», вы можете ознакомиться с нашей продукцией."
                )
            else:
                text += (
                    "\n\n⚠️ DIQQAT!\n"
                    "Siz dilerlar roʻyxatida topilmadingiz.\n"
                    "Buyurtma yaratish funksiyasi mavjud emas.\n"
                    "Katalog tugmasini bosib, bizning mahsulotlarimiz bilan tanishishingiz mumkin."
                )

    # 🎛 Клавиатура В ЗАВИСИМОСТИ ОТ СТАТУСА
    kb = get_main_menu_keyboard(user_id, lang)

    await message.answer(text, reply_markup=kb)
    await state.clear()


@router.message(F.content_type == ContentType.WEB_APP_DATA)
async def handle_webapp_data(message: Message, state: FSMContext):
    """Обработка данных из WebApp + проверка дилера"""

    user_id = message.from_user.id
    lang = get_user_lang(user_id)

    # ===== 1. ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ =====
    profile = get_user_profile(user_id)

    if not profile or not profile.get("phone"):
        if lang == "ru":
            await message.answer("❌ Ошибка профиля. Пожалуйста, пройдите регистрацию заново.")
        else:
            await message.answer("❌ Profil xatosi. Iltimos, qayta ro'yxatdan o'ting.")
        return

    # ===== 2. ПРОВЕРКА СТАТУСА ДИЛЕРА (Google Sheets) =====
    dealer_status = await check_dealer_status(
        user_id=user_id,
        phone=profile["phone"]
    )

    # ❌ ЕСЛИ НЕ АКТИВНЫЙ ДИЛЕР — СРАЗУ ВЫХОД
    if not dealer_status.get("is_active"):
        if lang == "ru":
            await message.answer(
                "❌ У вас нет доступа к созданию заказов.\n\n"
                f"Статус: {dealer_status.get('status', 'не в списке')}\n"
                "Для получения доступа свяжитесь с администратором."
            )
        else:
            await message.answer(
                "❌ Sizda buyurtma yaratish huquqi yo'q.\n\n"
                f"Holat: {dealer_status.get('status', 'roʻyxatda yoʻq')}\n"
                "Administrator bilan bogʻlaning."
            )
        return

    # ===== 3. COOLDOWN (ЗАЩИТА ОТ СПАМА) =====
    can_order, remaining = rate_limiter.check_order_cooldown(user_id)

    if not can_order:
        if lang == "ru":
            await message.answer(
                f"⏱ Подождите {remaining} сек перед созданием нового заказа."
            )
        else:
            await message.answer(
                f"⏱ Yangi buyurtma yaratishdan oldin {remaining} soniya kuting."
            )
        return



    # ===== 4. ПАРСИНГ МИНИМАЛЬНЫХ ДАННЫХ =====
    try:
        raw_data = message.web_app_data.data
        logger.info(f"📦 Received WebApp data from user {user_id}: {raw_data}")

        data = json.loads(raw_data)
        
        # ✅ ВАЛИДАЦИЯ МИНИМАЛЬНЫХ ДАННЫХ
        if not isinstance(data, dict) or "items" not in data:
            raise ValidationError("Invalid data structure")
        
        if not isinstance(data["items"], list) or len(data["items"]) == 0:
            raise ValidationError("Items list is empty")
        
        # Проверяем, что каждый элемент содержит id и qty
        for item in data["items"]:
            if "id" not in item or "qty" not in item:
                raise ValidationError("Item missing id or qty")
            if not isinstance(item["id"], int) or not isinstance(item["qty"], int):
                raise ValidationError("Invalid item data types")
            if item["qty"] <= 0:
                raise ValidationError("Quantity must be positive")
        
        logger.info(f"✅ Validated {len(data['items'])} items from WebApp")
        
    except json.JSONDecodeError as e:
        logger.exception(f"JSON decode error for user {user_id}")
        if lang == "ru":
            await message.answer("❌ Ошибка: некорректный формат данных")
        else:
            await message.answer("❌ Xato: noto'g'ri ma'lumot formati")
        return
    except ValidationError as e:
        logger.warning(f"Validation error for user {user_id}: {e}")
        if lang == "ru":
            await message.answer(f"❌ Ошибка валидации: {e}")
        else:
            await message.answer(f"❌ Tekshirish xatosi: {e}")
        return

    # ===== 5. ПОЛУЧАЕМ ПОЛНУЮ ИНФОРМАЦИЮ ИЗ GOOGLE SHEETS =====
    try:
        # Показываем индикатор загрузки
        if lang == "ru":
            loading_msg = await message.answer("⏳ Загружаем информацию о товарах...")
        else:
            loading_msg = await message.answer("⏳ Mahsulotlar ma'lumotini yuklamoqdamiz...")
        
        # Загружаем товары из Google Sheets
        products = await fetch_products_from_sheets()
        
        if not products:
            await loading_msg.delete()
            if lang == "ru":
                await message.answer("❌ Не удалось загрузить каталог товаров. Попробуйте позже.")
            else:
                await message.answer("❌ Mahsulotlar katalogini yuklashda xatolik. Keyinroq urinib ko'ring.")
            return
        
        # Дополняем данные заказа полной информацией
        enriched_items = []
        total_price = 0
        
        for item_data in data["items"]:
            product_id = item_data["id"]
            qty = item_data["qty"]
            
            # Получаем полную информацию о товаре
            product = products.get(product_id)
            
            if not product:
                logger.warning(f"⚠️ Product ID {product_id} not found in Google Sheets")
                await loading_msg.delete()
                if lang == "ru":
                    await message.answer(f"❌ Товар с ID {product_id} не найден в каталоге.")
                else:
                    await message.answer(f"❌ {product_id} ID li mahsulot katalogda topilmadi.")
                return
            
            # Формируем полный объект товара
            # Получаем URL изображения (берём только первое если их несколько)
            raw_image = product.get("image", "")
            first_image = raw_image.split(",")[0].strip() if raw_image else ""

            enriched_item = {
                "id": product_id,
                "name": product.get("name", "Без названия"),
                "price": int(product.get("price", 0)),
                "qty": qty,
                "image": first_image,  # ← только первое фото для PDF
                "category": product.get("category", "unknown"),
                "weight": float(product.get("weight", 0)),
                "cube": float(product.get("cube", 0))
            }
            
            enriched_items.append(enriched_item)
            total_price += enriched_item["price"] * qty
        
        # Удаляем сообщение загрузки
        await loading_msg.delete()
        
        logger.info(f"✅ Enriched order data: {len(enriched_items)} items, total: {total_price}")
        
        # Формируем полный объект заказа
        validated_data = {
            "items": enriched_items,
            "total": total_price,
            "user_id": data.get("user_id", 0)
        }
        
    except Exception as e:
        logger.exception(f"❌ Error enriching order data for user {user_id}")
        if lang == "ru":
            await message.answer("❌ Ошибка при обработке заказа. Попробуйте позже.")
        else:
            await message.answer("❌ Buyurtmani qayta ishlashda xatolik. Keyinroq urinib ko'ring.")
        return

    # ===== 6. ГЕНЕРАЦИЯ ПРЕДПРОСМОТРА PDF =====
    temp_order_id = f"PREVIEW_{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id % 10000:04d}"
    
    # Получаем профиль и координаты клиента
    profile_name = profile.get("full_name", "Клиент")
    client_latitude = profile.get("latitude") if profile else None
    client_longitude = profile.get("longitude") if profile else None

    # Группируем товары по категориям для определения мультикатегорийности
    grouped_items = group_items_by_category(validated_data["items"])
    is_multi_category = len(grouped_items) > 1

    try:
        # ✅ Предзагружаем все изображения параллельно
        preloaded_images = {}  
        
        pdf_preview = await asyncio.to_thread(
            generate_order_pdf,
            order_items=validated_data["items"],
            total=validated_data["total"],
            client_name=profile_name,
            admin_name=ADMIN_NAME,
            order_id=temp_order_id,
            approved=False,
            category=None if is_multi_category else get_order_category(validated_data["items"]),
            latitude=client_latitude,
            longitude=client_longitude,
            preloaded_images=preloaded_images  # ✅ ПЕРЕДАЕМ
        )

    except Exception as e:
        logger.exception(f"PDF generation error for user {user_id}")
        if lang == "ru":
            await message.answer("❌ Ошибка создания PDF. Попробуйте позже.")
        else:
            await message.answer("❌ PDF yaratishda xatolik. Keyinroq urinib ko'ring.")
        return

    # ===== 7. ОТПРАВКА ПРЕДПРОСМОТРА =====
    pdf_file = BufferedInputFile(pdf_preview, filename=f"order_preview_{temp_order_id}.pdf")

    if lang == "ru":
        preview_text = (
            f"📋 Предпросмотр вашего заказа\n\n"
            f"💰 Сумма: {format_currency(validated_data['total'])}\n"
            f"📦 Товаров: {len(validated_data['items'])}\n\n"
            f"⚠️ ВНИМАНИЕ!\n"
            f"Внимательно проверьте заказ выше.\n"
            f"Вы несете ответственность за корректность данных.\n\n"
            f"❌ Если есть ошибки - нажмите Отменить заказ.\n"
            f"✅ Если все верно - введите ваше полное имя для подтверждения:"
        )
    else:
        preview_text = (
            f"📋 Buyurtmangizni ko'rib chiqing\n\n"
            f"💰 Summa: {format_currency(validated_data['total'])}\n"
            f"📦 Mahsulotlar: {len(validated_data['items'])}\n\n"
            f"⚠️ DIQQAT!\n"
            f"Yuqoridagi buyurtmani diqqat bilan tekshiring.\n"
            f"Siz ma'lumotlarning to'g'riligiga javobgarsiz.\n\n"
            f"❌ Agar xato bo'lsa - Buyurtmani bekor qilish tugmasini bosing.\n"
            f"✅ Agar hammasi to'g'ri bo'lsa - tasdiqlash uchun to'liq ismingizni kiriting:"
        )

    await message.answer_document(document=pdf_file, caption=preview_text)

    # Клавиатура с кнопкой отмены
    user_id_tmp = message.from_user.id
    lang_tmp = get_user_lang(user_id_tmp)
    cancel_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить заказ" if lang_tmp == "ru" else "❌ Buyurtmani bekor qilish")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    if lang_tmp == "ru":
        await message.answer("Введите полное имя для подтверждения или отмените заказ:", reply_markup=cancel_kb)
    else:
        await message.answer("Tasdiqlash uchun to'liq ismingizni kiriting yoki buyurtmani bekor qiling:", reply_markup=cancel_kb)

    # Сохраняем данные заказа для подписи
    await state.update_data(order_data=validated_data)
    await state.set_state(OrderSign.waiting_name)

@router.message(F.text.in_(["🏠 Главный меню", "🏠 Bosh menyu"]))
async def expired_button_as_start(message: Message, state: FSMContext):
    await cmd_start(message, state)

@router.message(F.text.in_(["📋 Мои заказы", "📋 Mening buyurtmalarim"]))
async def cmd_my_orders(message: Message):
    """Просмотр заказов пользователя"""
    user_id = message.from_user.id
    lang = get_user_lang(user_id)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT order_id, total, status, created_at 
            FROM orders 
            WHERE user_id = %s 
            ORDER BY created_at DESC 
            LIMIT 10
        """, (user_id,))
        orders = [dict(row) for row in cursor.fetchall()]

    if not orders:
        if lang == "ru":
            await message.answer("У вас пока нет заказов.")
        else:
            await message.answer("Sizda hali buyurtmalar yo'q.")
        return

    if lang == "ru":
        text = "📋 Ваши заказы:\n\n"
    else:
        text = "📋 Sizning buyurtmalaringiz:\n\n"

    status_names = {
        "pending": "⏳ Ожидает" if lang == "ru" else "⏳ Kutilmoqda",
        "approved": "✅ Одобрен" if lang == "ru" else "✅ Tasdiqlandi",
        "production_received": "📋 Производство получило" if lang == "ru" else "📋 Ishlab chiqarish qabul qildi",
        "production_started": "🏭 В производстве" if lang == "ru" else "🏭 Ishlab chiqarilmoqda",
        "sent_to_warehouse": "📦 На складе" if lang == "ru" else "📦 Omborga yuborildi",
        "warehouse_received": "✅ Готов" if lang == "ru" else "✅ Tayyor",
        "rejected": "❌ Отклонен" if lang == "ru" else "❌ Rad etildi"
    }

    for order in orders:
        status = status_names.get(order["status"], order["status"])
        text += f"№{order['order_id']}\n"
        text += f"💰 {format_currency(order['total'])}\n"
        text += f"📅 {order['created_at'].strftime('%Y-%m-%d') if isinstance(order['created_at'], datetime) else str(order['created_at'])[:10]}\n"
        text += f"📊 {status}\n\n"

    await message.answer(text)


@router.message(F.text.in_(["⚙️ Настройки", "⚙️ Sozlamalar"]))
async def cmd_settings(message: Message):
    """Настройки пользователя"""
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    profile = get_user_profile(user_id)

    if lang == "ru":
        location_text = ""
        if profile.get('latitude') and profile.get('longitude'):
            location_text = f"\n📍 Локация: {profile.get('latitude'):.6f}, {profile.get('longitude'):.6f}"
        text = f"⚙️ Настройки\n\n👤 {profile.get('full_name', 'Не указано')}\n📱 {profile.get('phone', 'Не указано')}\n🏙 {profile.get('city', 'Не указано')}{location_text}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇿 Переключить на узбекский", callback_data="toggle_lang")],
            [InlineKeyboardButton(text="📝 Изменить профиль", callback_data="register")]
        ])
    else:
        location_text = ""
        if profile.get('latitude') and profile.get('longitude'):
            location_text = f"\n📍 Joylashuv: {profile.get('latitude'):.6f}, {profile.get('longitude'):.6f}"
        text = f"⚙️ Sozlamalar\n\n👤 {profile.get('full_name', 'Kiritilmagan')}\n📱 {profile.get('phone', 'Kiritilmagan')}\n🏙 {profile.get('city', 'Kiritilmagan')}{location_text}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇷🇺 Rus tiliga o'tish", callback_data="toggle_lang")],
            [InlineKeyboardButton(text="📝 Profilni o'zgartirish", callback_data="register")]
        ])

    await message.answer(text, reply_markup=kb)


# ==================== ADMIN КОМАНДЫ ====================

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админ панель"""
    user_id = message.from_user.id

    if user_id not in ALL_ADMIN_IDS:
        await message.answer("У вас нет доступа к админ-панели.")
        return

    # Определяем роль
    role = "Супер-администратор" if user_id == SUPER_ADMIN_ID else \
        "Отдел продаж" if user_id in SALES_ADMIN_IDS else \
            "Отдел производства" if user_id in PRODUCTION_ADMIN_IDS else \
                "Склад" if user_id in WAREHOUSE_ADMIN_IDS else "Неизвестно"

    text = f"👨‍💼 Админ-панель\nРоль: {role}\n\n"
    text += "Доступные команды:\n"

    if user_id == SUPER_ADMIN_ID:
        text += "• /orders_export - экспорт заказов\n"
        text += "• /sendall - массовая рассылка\n"
        text += "• /send - отправить сообщение пользователю\n"
        text += "• /get_pdf - получить PDF заказа\n"

    if has_permission(user_id, AdminRole.SALES):
        text += "• Одобрение/отклонение заказов\n"

    if has_permission(user_id, AdminRole.PRODUCTION):
        text += "• Управление производством\n"

    if has_permission(user_id, AdminRole.WAREHOUSE):
        text += "• Управление складом\n"

    await message.answer(text)


# ==================== CALLBACK ОБРАБОТЧИКИ ДЛЯ СТАТУСОВ ====================

@router.callback_query(F.data.startswith("approve:"))
async def callback_approve_order(callback: CallbackQuery):
    """Одобрение заказа (отдел продаж)"""
    user_id = callback.from_user.id

    if not has_permission(user_id, AdminRole.SALES):
        await callback.answer("У вас нет прав для одобрения заказов", show_alert=True)
        return

    order_id = callback.data.split(":")[1]

    # Показываем подтверждение
    kb_confirm = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, одобрить", callback_data=f"admapprove_yes:{order_id}"),
            InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"admapprove_no:{order_id}")
        ]
    ])

    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n⚠️ Вы уверены, что хотите ОДОБРИТЬ этот заказ?",
        reply_markup=kb_confirm
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admapprove_yes:"))
async def callback_approve_order_confirmed(callback: CallbackQuery):
    """Подтверждение одобрения заказа"""
    user_id = callback.from_user.id

    if not has_permission(user_id, AdminRole.SALES):
        await callback.answer("У вас нет прав", show_alert=True)
        return

    order_id = callback.data.split(":")[1]
    order_data = get_order_raw(order_id)

    if not order_data:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # ⚡ ВАЖНО: Отвечаем сразу, чтобы избежать timeout (Telegram дает только 30 сек)
    await callback.answer("⏳ Обработка заказа началась...")

    # Получаем категорию заказа
    order_category = order_data.get("category")

    # Получаем координаты клиента
    client_profile = get_user_profile(order_data["user_id"])
    client_latitude = client_profile.get("latitude") if client_profile else None
    client_longitude = client_profile.get("longitude") if client_profile else None

    # Генерируем финальный PDF
    order_json = json.loads(order_data["order_json"])
    client_name = order_data.get("client_name", "Клиент")
    
    # Проверяем, является ли заказ мультикатегорийным
    is_multi_category = len(set(item.get("category") for item in order_json["items"])) > 1
    
    preloaded_images = await preload_order_images(order_json["items"])
    
    pdf_final = await asyncio.to_thread(
        generate_order_pdf,
        order_items=order_json["items"],
        total=order_json["total"],
        client_name=client_name,
        admin_name=ADMIN_NAME,
        order_id=order_id,
        approved=True,
        category=None if is_multi_category else get_order_category(order_json["items"]),
        latitude=client_latitude,
        longitude=client_longitude,
        preloaded_images=preloaded_images
    )

    # Обновляем статус
    update_order_status(order_id, OrderStatus.APPROVED, pdf_final, user_id)

    # Загружаем PDF
    await upload_pdf_to_hosting_async(order_id, pdf_final)

    # Уведомляем клиента через группированное сообщение
    client_user_id = order_data["user_id"]
    lang = get_user_lang(client_user_id)
    base_order_id = order_data.get("base_order_id") or order_id
    await send_or_update_client_notification(base_order_id, client_user_id, lang)

    # Уведомляем соответствующий цех производства
    if order_category:
        production_ids = get_production_ids_for_category(order_category)
        category_name = get_category_name(order_category)

        if production_ids:
            production_text = (
                f"🔔 Новый одобренный заказ для вашего цеха!\n\n"
                f"📋 Номер заказа: #{order_id}\n"
                f"🏭 Категория: {category_name}\n"
                f"👤 Клиент: {order_data['client_name']}\n"
                f"💰 Сумма: {format_currency(order_data['total'])}\n\n"
                f"⏰ Заказ ожидает получения производством"
            )

            for prod_id in production_ids:
                try:
                    await bot.send_message(
                        chat_id=prod_id,
                        text=production_text
                    )
                    logger.info(f"Notified production admin {prod_id} for category {category_name}")
                except Exception as e:
                    logger.exception(f"Failed to notify production admin {prod_id}")

    # Получаем информацию об админе
    admin_name = get_admin_name(user_id)
    admin_info = f"{admin_name} (ID: {user_id})"
    current_time = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Обновляем caption с историей действий
    original_caption = callback.message.caption
    # Удаляем старую строку статуса и подтверждение
    original_caption = re.sub(r'\n📊 Статус:.*?\n━━━━━━━━━━━━━━━━━━━━━━', '', original_caption)
    original_caption = re.sub(r'\n\n⚠️ Вы уверены.*', '', original_caption)

    new_caption = (
            original_caption +
            f"\n\n📊 Статус: ✅ Одобрен\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Одобрен: {admin_info}\n"
            f"   Время: {current_time}"
    )

    # Новые кнопки для следующего этапа
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📋 Получено производством",
            callback_data=f"production_received:{order_id}"
        )]
    ])

    await callback.message.edit_caption(
        caption=new_caption,
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("admapprove_no:"))
async def callback_approve_order_cancelled(callback: CallbackQuery):
    """Отмена одобрения"""
    user_id = callback.from_user.id

    if not has_permission(user_id, AdminRole.SALES):
        await callback.answer("У вас нет прав", show_alert=True)
        return

    order_id = callback.data.split(":")[1]

    kb_original = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{order_id}")
        ]
    ])

    await callback.message.edit_caption(
        caption=callback.message.caption.replace("\n\n⚠️ Вы уверены, что хотите ОДОБРИТЬ этот заказ?", ""),
        reply_markup=kb_original
    )
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("reject:"))
async def callback_reject_order(callback: CallbackQuery):
    """Отклонение заказа (отдел продаж)"""
    user_id = callback.from_user.id

    if not has_permission(user_id, AdminRole.SALES):
        await callback.answer("У вас нет прав для отклонения заказов", show_alert=True)
        return

    order_id = callback.data.split(":")[1]

    kb_confirm = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, отклонить", callback_data=f"admreject_yes:{order_id}"),
            InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"admreject_no:{order_id}")
        ]
    ])

    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n⚠️ Вы уверены, что хотите ОТКЛОНИТЬ этот заказ?",
        reply_markup=kb_confirm
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admreject_yes:"))
async def callback_reject_order_confirmed(callback: CallbackQuery):
    """Подтверждение отклонения"""
    user_id = callback.from_user.id

    if not has_permission(user_id, AdminRole.SALES):
        await callback.answer("У вас нет прав", show_alert=True)
        return

    order_id = callback.data.split(":")[1]
    order_data = get_order_raw(order_id)

    if not order_data:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # Обновляем статус
    update_order_status(order_id, OrderStatus.REJECTED, updated_by=user_id)

    # Уведомляем клиента через группированное сообщение
    client_user_id = order_data["user_id"]
    lang = get_user_lang(client_user_id)
    base_order_id = order_data.get("base_order_id") or order_id
    await send_or_update_client_notification(base_order_id, client_user_id, lang)

    # Получаем информацию об админе
    admin_name = get_admin_name(user_id)
    admin_info = f"{admin_name} (ID: {user_id})"
    current_time = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Обновляем caption
    original_caption = callback.message.caption
    original_caption = re.sub(r'\n📊 Статус:.*?\n━━━━━━━━━━━━━━━━━━━━━━', '', original_caption)
    original_caption = re.sub(r'\n\n⚠️ Вы уверены.*', '', original_caption)

    new_caption = (
            original_caption +
            f"\n\n📊 Статус: ❌ Отклонён\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ Отклонён: {admin_info}\n"
            f"   Время: {current_time}"
    )

    await callback.message.edit_caption(
        caption=new_caption,
        reply_markup=None
    )
    await callback.answer("❌ Заказ отклонён")


@router.callback_query(F.data.startswith("admreject_no:"))
async def callback_reject_order_cancelled(callback: CallbackQuery):
    """Отмена отклонения"""
    user_id = callback.from_user.id

    if not has_permission(user_id, AdminRole.SALES):
        await callback.answer("У вас нет прав", show_alert=True)
        return

    order_id = callback.data.split(":")[1]

    kb_original = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{order_id}")
        ]
    ])

    await callback.message.edit_caption(
        caption=callback.message.caption.replace("\n\n⚠️ Вы уверены, что хотите ОТКЛОНИТЬ этот заказ?", ""),
        reply_markup=kb_original
    )
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("production_received:"))
async def callback_production_received(callback: CallbackQuery):
    """Отдел производства получил заказ"""
    user_id = callback.from_user.id

    order_id = callback.data.split(":")[1]
    order_data = get_order_raw(order_id)

    if not order_data:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # Получаем категорию заказа
    order_category = order_data.get("category")

    # Проверяем права для конкретного цеха
    if not has_permission(user_id, AdminRole.PRODUCTION, order_category):
        category_name = get_category_name(order_category) if order_category else "этого заказа"
        await callback.answer(f"У вас нет прав для обработки заказов категории {category_name}", show_alert=True)
        return

    # Обновляем статус
    update_order_status(order_id, OrderStatus.PRODUCTION_RECEIVED, updated_by=user_id)

    # Уведомляем клиента через группированное сообщение
    client_user_id = order_data["user_id"]
    lang = get_user_lang(client_user_id)
    base_order_id = order_data.get("base_order_id") or order_id
    await send_or_update_client_notification(base_order_id, client_user_id, lang)

    # Получаем информацию об админе
    admin_name = get_admin_name(user_id)
    admin_info = f"{admin_name} (ID: {user_id})"
    current_time = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Обновляем caption с добавлением новой записи
    original_caption = callback.message.caption
    original_caption = re.sub(r'\n📊 Статус:.*?\n━━━━━━━━━━━━━━━━━━━━━━', '', original_caption)

    # Находим блок с историей действий
    history_match = re.search(r'(✅ Одобрен:.*?Время: \d{2}\.\d{2}\.\d{4} \d{2}:\d{2})', original_caption, re.DOTALL)
    history_text = history_match.group(1) if history_match else ""

    new_caption = (
            original_caption.split("━━━━━━━━━━━━━━━━━━━━━━")[0] +
            f"\n📊 Статус: 📋 Получен производством\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{history_text}\n"
            f"📋 Получено производством: {admin_info}\n"
            f"   Время: {current_time}"
    )

    # Новые кнопки
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🏭 Начать производство",
            callback_data=f"production_started:{order_id}"
        )]
    ])

    await callback.message.edit_caption(
        caption=new_caption,
        reply_markup=kb
    )
    await callback.answer("✅ Заказ получен")


@router.callback_query(F.data.startswith("production_started:"))
async def callback_production_started(callback: CallbackQuery):
    """Производство начато"""
    user_id = callback.from_user.id

    order_id = callback.data.split(":")[1]
    order_data = get_order_raw(order_id)

    if not order_data:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # Получаем категорию заказа и проверяем права
    order_category = order_data.get("category")

    if not has_permission(user_id, AdminRole.PRODUCTION, order_category):
        category_name = get_category_name(order_category) if order_category else "этого заказа"
        await callback.answer(f"У вас нет прав для обработки заказов категории {category_name}", show_alert=True)
        return

    # Обновляем статус
    update_order_status(order_id, OrderStatus.PRODUCTION_STARTED, updated_by=user_id)

    # Уведомляем клиента через группированное сообщение
    client_user_id = order_data["user_id"]
    lang = get_user_lang(client_user_id)
    base_order_id = order_data.get("base_order_id") or order_id
    await send_or_update_client_notification(base_order_id, client_user_id, lang)

    # Получаем информацию об админе
    admin_name = get_admin_name(user_id)
    admin_info = f"{admin_name} (ID: {user_id})"
    current_time = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Обновляем caption
    original_caption = callback.message.caption
    original_caption = re.sub(r'\n📊 Статус:.*?\n━━━━━━━━━━━━━━━━━━━━━━', '', original_caption)

    # Извлекаем всю историю
    history_section = re.search(r'━━━━━━━━━━━━━━━━━━━━━━\n(.*)', original_caption, re.DOTALL)
    history_text = history_section.group(1) if history_section else ""

    new_caption = (
            original_caption.split("━━━━━━━━━━━━━━━━━━━━━━")[0] +
            f"\n📊 Статус: 🏭 Производство начато\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{history_text}\n"
            f"🏭 Производство начато: {admin_info}\n"
            f"   Время: {current_time}"
    )

    # Новые кнопки
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📦 Передать на склад",
            callback_data=f"sent_to_warehouse:{order_id}"
        )]
    ])

    await callback.message.edit_caption(
        caption=new_caption,
        reply_markup=kb
    )
    await callback.answer("✅ Производство начато")


@router.callback_query(F.data.startswith("sent_to_warehouse:"))
async def callback_sent_to_warehouse(callback: CallbackQuery):
    """Передано на склад"""
    user_id = callback.from_user.id

    order_id = callback.data.split(":")[1]
    order_data = get_order_raw(order_id)

    if not order_data:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # Получаем категорию заказа и проверяем права
    order_category = order_data.get("category")

    if not has_permission(user_id, AdminRole.PRODUCTION, order_category):
        category_name = get_category_name(order_category) if order_category else "этого заказа"
        await callback.answer(f"У вас нет прав для обработки заказов категории {category_name}", show_alert=True)
        return

    # Обновляем статус
    update_order_status(order_id, OrderStatus.SENT_TO_WAREHOUSE, updated_by=user_id)

    # Уведомляем клиента через группированное сообщение
    client_user_id = order_data["user_id"]
    lang = get_user_lang(client_user_id)
    base_order_id = order_data.get("base_order_id") or order_id
    await send_or_update_client_notification(base_order_id, client_user_id, lang)

    # Получаем информацию об админе
    admin_name = get_admin_name(user_id)
    admin_info = f"{admin_name} (ID: {user_id})"
    current_time = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Обновляем caption
    original_caption = callback.message.caption
    original_caption = re.sub(r'\n📊 Статус:.*?\n━━━━━━━━━━━━━━━━━━━━━━', '', original_caption)

    # Извлекаем всю историю
    history_section = re.search(r'━━━━━━━━━━━━━━━━━━━━━━\n(.*)', original_caption, re.DOTALL)
    history_text = history_section.group(1) if history_section else ""

    new_caption = (
            original_caption.split("━━━━━━━━━━━━━━━━━━━━━━")[0] +
            f"\n📊 Статус: 📦 Передано на склад\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{history_text}\n"
            f"📦 Передано на склад: {admin_info}\n"
            f"   Время: {current_time}"
    )

    # Новые кнопки для склада
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Получено складом",
            callback_data=f"warehouse_received:{order_id}"
        )]
    ])

    await callback.message.edit_caption(
        caption=new_caption,
        reply_markup=kb
    )
    await callback.answer("✅ Передано на склад")


@router.callback_query(F.data.startswith("warehouse_received:"))
async def callback_warehouse_received(callback: CallbackQuery):
    """Склад получил партию"""
    user_id = callback.from_user.id

    if not has_permission(user_id, AdminRole.WAREHOUSE):
        await callback.answer("У вас нет прав", show_alert=True)
        return

    order_id = callback.data.split(":")[1]
    order_data = get_order_raw(order_id)

    if not order_data:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    # Обновляем статус
    update_order_status(order_id, OrderStatus.WAREHOUSE_RECEIVED, updated_by=user_id)

    # Уведомляем клиента
    client_user_id = order_data["user_id"]
    lang = get_user_lang(client_user_id)
    category = order_data.get("category")

    # НОВОЕ: Отправляем отдельное уведомление о готовности этой категории
    if category:
        await send_category_completion_notification(order_id, category, client_user_id, lang)

    # Обновляем группированное сообщение со всеми категориями
    base_order_id = order_data.get("base_order_id") or order_id
    await send_or_update_client_notification(base_order_id, client_user_id, lang)

    # Получаем информацию об админе
    admin_name = get_admin_name(user_id)
    admin_info = f"{admin_name} (ID: {user_id})"
    current_time = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Обновляем caption - финальный статус
    original_caption = callback.message.caption
    original_caption = re.sub(r'\n📊 Статус:.*?\n━━━━━━━━━━━━━━━━━━━━━━', '', original_caption)

    # Извлекаем всю историю
    history_section = re.search(r'━━━━━━━━━━━━━━━━━━━━━━\n(.*)', original_caption, re.DOTALL)
    history_text = history_section.group(1) if history_section else ""

    new_caption = (
            original_caption.split("━━━━━━━━━━━━━━━━━━━━━━")[0] +
            f"\n📊 Статус: ✅ Получено складом (ГОТОВО)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{history_text}\n"
            f"✅ Получено складом: {admin_info}\n"
            f"   Время: {current_time}\n\n"
            f"🎉 Заказ полностью выполнен!"
    )

    await callback.message.edit_caption(
        caption=new_caption,
        reply_markup=None
    )
    await callback.answer("✅ Партия получена")


# ==================== ВСПОМОГАТЕЛЬНЫЕ КОМАНДЫ ====================

@router.message(Command("send"))
async def cmd_send(message: Message):
    """Отправка сообщения пользователю (только супер-админ)"""
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "• `/send USER_ID текст`\n"
            "• Или ответь на сообщение с `/send USER_ID`",
            parse_mode="Markdown"
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ USER_ID должен быть числом.")
        return

    payload = parts[2] if len(parts) > 2 else ""
    ok = False

    try:
        if message.photo:
            file_id = message.photo[-1].file_id
            await bot.send_photo(chat_id=target_id, photo=file_id, caption=payload or None)
            ok = True

        elif message.video:
            file_id = message.video.file_id
            await bot.send_video(chat_id=target_id, video=file_id, caption=payload or None)
            ok = True

        elif message.reply_to_message:
            src = message.reply_to_message
            try:
                await bot.copy_message(chat_id=target_id, from_chat_id=src.chat.id, message_id=src.message_id)
                if payload:
                    await bot.send_message(target_id, payload)
                ok = True
            except Exception:
                try:
                    await bot.forward_message(chat_id=target_id, from_chat_id=src.chat.id, message_id=src.message_id)
                    if payload:
                        await bot.send_message(target_id, payload)
                    ok = True
                except Exception:
                    ok = False

        else:
            if not payload:
                await message.answer("Нет текста для отправки.")
                return
            await bot.send_message(target_id, payload)
            ok = True

    except Exception:
        logger.exception("Ошибка при отправке /send")
        ok = False

    if ok:
        await message.answer(f"✅ Отправлено пользователю {target_id}.")
    else:
        await message.answer(f"❌ Не удалось отправить пользователю {target_id}.")


@router.message(OrderSign.waiting_name)
async def order_signature_handler(message: Message, state: FSMContext):
    """Обработка подписи заказа"""
    try:
        lang = get_user_lang(message.from_user.id)
        sign_name = message.text.strip()
        profile_name = get_user_full_name(message.from_user.id)

        # ❌ Обработка отмены заказа
        if sign_name in ("❌ Отменить заказ", "❌ Buyurtmani bekor qilish"):
            await state.clear()
            kb = get_main_menu_keyboard(message.from_user.id, lang)
            if lang == "ru":
                await message.answer("Заказ отменён. Возврат в главное меню.", reply_markup=kb)
            else:
                await message.answer("Buyurtma bekor qilindi. Bosh menyuga qaytildi.", reply_markup=kb)
            return

        if not sign_name:
            if lang == "ru":
                await message.answer("Пожалуйста, введите имя для подписи.")
            else:
                await message.answer("Iltimos, imzo uchun ismingizni kiriting.")
            return

        # Проверка совпадения с профилем
        if profile_name:
            norm_input = " ".join(sign_name.split()).lower()
            norm_profile = " ".join(profile_name.split()).lower()

            if norm_input != norm_profile:
                if lang == "ru":
                    await message.answer(
                        "Имя для подписи должно совпадать с именем при регистрации.\n"
                        f"Ваше имя: *{profile_name}*\n\n"
                        "Введите его *точно так же*.",
                        parse_mode="Markdown"
                    )
                else:
                    await message.answer(
                        "Imzo uchun ism ro'yxatdan o'tishda yozilgan ism bilan bir xil bo'lishi kerak.\n"
                        f"Ismingiz: *{profile_name}*\n\n"
                        "Xuddi shunday kiriting.",
                        parse_mode="Markdown"
                    )
                return
            final_name = profile_name
        else:
            final_name = sign_name

        # Получаем данные заказа
        data = await state.get_data()
        order_data = data.get("order_data")

        if not order_data:
            if lang == "ru":
                await message.answer("Ошибка: данные заказа не найдены. Начните заново с /start")
            else:
                await message.answer("Xato: buyurtma ma'lumotlari topilmadi. /start dan qayta boshlang")
            await state.clear()
            return

        # Генерируем базовый ID заказа (без суффикса)
        base_order_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}{message.from_user.id % 10000:04d}"

        # Получаем координаты клиента
        client_profile = get_user_profile(message.from_user.id)
        client_latitude = client_profile.get("latitude") if client_profile else None
        client_longitude = client_profile.get("longitude") if client_profile else None

        # Группируем товары по категориям
        grouped_items = group_items_by_category(order_data["items"])
        num_categories = len(grouped_items)
        
        # ✅ ЛОГИРУЕМ КАТЕГОРИИ ДЛЯ ОТЛАДКИ
        logger.info(f"📋 Order {base_order_id} categories detected:")
        for cat, items in grouped_items.items():
            logger.info(f"  - {cat} ({get_category_name(cat)}): {len(items)} items")
        
        # Регистрируем заказ
        rate_limiter.register_order(message.from_user.id)

        # ===== ОТПРАВЛЯЕМ КЛИЕНТУ ТОЛЬКО ТЕКСТОВОЕ ПОДТВЕРЖДЕНИЕ =====
        # PDF клиент уже получил при предпросмотре, повторно не отправляем
        if lang == "ru":
            user_text = (
                f"Ваш заказ №{base_order_id} отправлен!\n\n"
                f"💰 Сумма: {format_currency(order_data['total'])}\n"
                f"📦 Товаров: {len(order_data['items'])}\n"
                f"🏭 Категорий: {num_categories}\n"
                f"✍️ Подпись: {final_name}\n\n"
                f"📋 Отдел продаж скоро обработает ваш заказ.\n"
                f"Мы уведомим вас о статусе заказа."
            )
        else:
            user_text = (
                f"Sizning №{base_order_id} raqamli buyurtmangiz  yuborildi!\n\n"
                f"💰 Summa: {format_currency(order_data['total'])}\n"
                f"📦 Mahsulotlar: {len(order_data['items'])}\n"
                f"🏭 Kategoriyalar: {num_categories}\n"
                f"✍️ Imzo: {final_name}\n\n"
                f"📋 Savdo bo'limi tez orada buyurtmangizni ko'rib chiqadi.\n"
                f"Buyurtma holati haqida sizga xabar yuboriladi"
            )

        kb = get_main_menu_keyboard(message.from_user.id, lang)
        await message.answer(user_text, reply_markup=kb)

        # Отправляем в админ-чат (группу) - отдельные PDF для каждой категории
        profile = get_user_profile(message.from_user.id)

        # Формируем строку с координатами
        location_text = ""
        if client_latitude is not None and client_longitude is not None:
            location_text = f"📍 Координаты: {client_latitude:.6f}, {client_longitude:.6f}\n"

        # Создаем и отправляем PDF для каждой категории
        part_num = 1
        for category, category_items in sorted(grouped_items.items()):
            # Формируем подномер заказа
            sub_order_id = f"{base_order_id}_{part_num}"

            # Вычисляем сумму для этой категории
            category_total = sum(item.get("qty", 0) * item.get("price", 0) for item in category_items)

            # Генерируем PDF для этой категории
            
            sub_preloaded = await preload_order_images(category_items)
            
            pdf_category = await asyncio.to_thread(
                generate_order_pdf,
                order_items=category_items,
                total=category_total,
                client_name=final_name,
                admin_name=ADMIN_NAME,
                order_id=sub_order_id,
                approved=True,
                category=category,
                latitude=client_latitude,
                longitude=client_longitude,
                preloaded_images=sub_preloaded
            )
            # Сохраняем в БД
            save_order(
                order_id=sub_order_id,
                client_name=final_name,
                user_id=message.from_user.id,
                total=category_total,
                pdf_draft=pdf_category,
                order_json={"items": category_items, "total": category_total},
                category=category,
                base_order_id=base_order_id
            )

            # Загружаем на хостинг
            await upload_pdf_to_hosting_async(sub_order_id, pdf_category)

            # Формируем текст для админов
            category_name = get_category_name(category)
            admin_text = (
                f"🆕 Новый заказ №{sub_order_id}\n"
                f"📋 Часть {part_num} из {num_categories} (Базовый номер: {base_order_id})\n\n"
                f"👤 Клиент: {final_name}\n"
                f"👤 User ID: {message.from_user.id}\n"
                f"📱 Телефон: {profile.get('phone', 'Не указан')}\n"
                f"🏙 Город: {profile.get('city', 'Не указан')}\n"
                f"{location_text}"
                f"🏭 Категория: {category_name}\n"
                f"💰 Сумма (этой категории): {format_currency(category_total)}\n"
                f"💰 Общая сумма заказа: {format_currency(order_data['total'])}\n"
                f"📦 Товаров (в этой категории): {len(category_items)}\n"
                f"📦 Товаров (всего в заказе): {len(order_data['items'])}\n\n"
                f"📊 Статус: ⏳ Ожидает одобрения\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{sub_order_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{sub_order_id}")
                ]
            ])

            try:
                pdf_file = BufferedInputFile(pdf_category, filename=f"order_{sub_order_id}.pdf")
                await bot.send_document(
                    chat_id=ADMIN_CHAT_ID,
                    document=pdf_file,
                    caption=admin_text,
                    reply_markup=kb
                )
                logger.info(f"Order part {sub_order_id} (category: {category_name}) sent to admin chat {ADMIN_CHAT_ID}")
            except Exception as e:
                logger.exception(f"Failed to send order part {sub_order_id} to admin chat {ADMIN_CHAT_ID}")

            part_num += 1

        await state.clear()

    except Exception as e:
        logger.exception(f"Error in order signature handler")
        lang = get_user_lang(message.from_user.id)
        kb = get_main_menu_keyboard(message.from_user.id, lang)
        if lang == "ru":
            await message.answer("❌ Произошла ошибка при обработке заказа. Попробуйте позже.", reply_markup=kb)
        else:
            await message.answer("❌ Buyurtmani qayta ishlashda xatolik yuz berdi. Keyinroq urinib ko'ring.", reply_markup=kb)
        await state.clear()


@router.message(Command("orders_export"))
async def cmd_orders_export(message: Message):
    """Экспорт заказов (только супер-админ)"""
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    orders = get_all_orders(limit=10000)

    if not orders:
        await message.answer("В базе нет заказов.")
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["order_id", "client_name", "user_id", "total", "created_at", "status"])

    for o in orders:
        writer.writerow([
            o["order_id"],
            o["client_name"],
            o["user_id"],
            o["total"],
            o["created_at"],
            o["status"] or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    output.close()

    filename = f"orders_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    file = BufferedInputFile(csv_bytes, filename=filename)

    await message.answer_document(document=file, caption="Экспорт заказов (CSV)")


@router.message(Command("users_stats"))
async def cmd_users_stats(message: Message):
    """Статистика пользователей (только супер-админ)"""
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    
    stats = get_users_stats()
    
    text = (
        "📊 Статистика пользователей:\n\n"
        f"👥 Всего пользователей: {stats['total']}\n"
        f"🟢 Активных (30 дней): {stats['active_30d']}\n"
        f"✨ Новых (7 дней): {stats['new_7d']}\n"
    )
    
    await message.answer(text)


@router.message(Command("sendall"))
async def cmd_sendall(message: Message):
    """Массовая рассылка (только супер-админ)"""
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    text_part = ""

    if message.text:
        parts = message.text.split(" ", 1)
        if len(parts) > 1:
            text_part = parts[1].strip()

    if message.caption:
        parts = message.caption.split(" ", 1)
        if len(parts) > 1:
            text_part = parts[1].strip()

    if not text_part:
        await message.answer(
            "Использование:\n"
            "• Текст: `/sendall текст`\n"
            "• Фото/видео: отправь медиа с подписью `/sendall текст`",
            parse_mode="Markdown"
        )
        return

    user_ids = get_all_user_ids()
    if not user_ids:
        await message.answer("Нет пользователей.")
        return

    ok = 0
    fail = 0

    if message.photo:
        file_id = message.photo[-1].file_id
        for uid in user_ids:
            try:
                await bot.send_photo(uid, file_id, caption=text_part)
                ok += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                fail += 1
            except Exception:
                fail += 1

    elif message.video:
        file_id = message.video.file_id
        for uid in user_ids:
            try:
                await bot.send_video(uid, file_id, caption=text_part)
                ok += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                fail += 1
            except Exception:
                fail += 1

    else:
        for uid in user_ids:
            try:
                await bot.send_message(uid, text_part)
                ok += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                fail += 1
            except Exception:
                fail += 1

    await message.answer(f"✅ Отправлено: {ok}\n❌ Не доставлено: {fail}")


@router.message(Command("get_pdf"))
async def cmd_get_pdf(message: Message):
    """Получить PDF заказа"""
    user_id = message.from_user.id
    lang = get_user_lang(user_id)

    args = message.text.split()
    if len(args) < 2:
        if lang == "ru":
            await message.answer("Использование: /get_pdf <номер_заказа>")
        else:
            await message.answer("Foydalanish: /get_pdf <buyurtma_raqami>")
        return

    order_id = args[1].strip()

    # Админы могут получать любые заказы
    if user_id in ALL_ADMIN_IDS:
        record = get_order_raw(order_id)
    else:
        record = get_order_for_user(order_id, user_id)

    if not record:
        if lang == "ru":
            await message.answer("Заказ не найден.")
        else:
            await message.answer("Buyurtma topilmadi.")
        return

    pdf_bytes = record.get("pdf_final") or record.get("pdf_draft")
    if not pdf_bytes:
        if lang == "ru":
            await message.answer("PDF не доступен.")
        else:
            await message.answer("PDF mavjud emas.")
        return

    pdf_file = BufferedInputFile(pdf_bytes, filename=f"order_{order_id}.pdf")

    if lang == "ru":
        caption = f"PDF заказа №{order_id}"
    else:
        caption = f"Buyurtma №{order_id} PDF"

    await message.answer_document(document=pdf_file, caption=caption)


# ==================== УПРАВЛЕНИЕ ДИЛЕРАМИ ====================

@router.message(Command("add_dealer"))
async def cmd_add_dealer(message: Message):
    """
    Добавить дилера в БД.
    Использование: /add_dealer <telegram_id> <телефон> <имя>
    Пример: /add_dealer 8301464087 998901234567 Asad beeline
    """
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        await message.answer(
            "❌ Использование:\n"
            "<code>/add_dealer &lt;telegram_id&gt; &lt;телефон&gt; &lt;имя&gt;</code>\n\n"
            "Пример:\n"
            "<code>/add_dealer 8301464087 998901234567 Asad beeline</code>",
            parse_mode="HTML"
        )
        return

    try:
        tg_id = int(parts[1])
        phone = parts[2]
        name = parts[3]
    except ValueError:
        await message.answer("❌ telegram_id должен быть числом.")
        return

    success = add_dealer_to_db(tg_id, phone, name, status='active')
    # Сбрасываем кеш для этого пользователя
    dealer_cache.pop(tg_id, None)

    if success:
        await message.answer(
            f"✅ Дилер добавлен/обновлён:\n"
            f"🆔 TG ID: <code>{tg_id}</code>\n"
            f"📱 Телефон: {phone}\n"
            f"👤 Имя: {name}\n"
            f"🟢 Статус: active",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Ошибка при добавлении дилера.")


@router.message(Command("remove_dealer"))
async def cmd_remove_dealer(message: Message):
    """
    Удалить дилера из БД.
    Использование: /remove_dealer <telegram_id>
    """
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ Использование: <code>/remove_dealer &lt;telegram_id&gt;</code>",
            parse_mode="HTML"
        )
        return

    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("❌ telegram_id должен быть числом.")
        return

    removed = remove_dealer_from_db(tg_id)
    dealer_cache.pop(tg_id, None)

    if removed:
        await message.answer(f"✅ Дилер <code>{tg_id}</code> удалён.", parse_mode="HTML")
    else:
        await message.answer(f"⚠️ Дилер с ID <code>{tg_id}</code> не найден.", parse_mode="HTML")


@router.message(Command("dealer_status"))
async def cmd_dealer_status(message: Message):
    """
    Изменить статус дилера.
    Использование: /dealer_status <telegram_id> <active|inactive>
    """
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "❌ Использование: <code>/dealer_status &lt;telegram_id&gt; &lt;active|inactive&gt;</code>",
            parse_mode="HTML"
        )
        return

    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("❌ telegram_id должен быть числом.")
        return

    status = parts[2].lower()
    if status not in ('active', 'inactive'):
        await message.answer("❌ Статус должен быть: active или inactive")
        return

    updated = set_dealer_status_in_db(tg_id, status)
    dealer_cache.pop(tg_id, None)

    emoji = "🟢" if status == "active" else "🔴"
    if updated:
        await message.answer(
            f"{emoji} Статус дилера <code>{tg_id}</code> изменён на <b>{status}</b>",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"⚠️ Дилер с ID <code>{tg_id}</code> не найден.")


@router.message(Command("list_dealers"))
async def cmd_list_dealers(message: Message):
    """Показать список всех дилеров из БД"""
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    dealers = get_all_dealers_from_db()
    if not dealers:
        await message.answer("📋 Список дилеров пуст.")
        return

    lines = [f"📋 <b>Дилеры ({len(dealers)}):</b>\n"]
    for d in dealers:
        emoji = "🟢" if d['status'] == 'active' else "🔴"
        tg_id = d['telegram_id'] or '—'
        lines.append(
            f"{emoji} <code>{tg_id}</code> | {d['phone']} | {d['name']}"
        )

    # Разбиваем на части если много дилеров
    chunk = []
    for line in lines:
        chunk.append(line)
        if len(chunk) >= 30:
            await message.answer("\n".join(chunk), parse_mode="HTML")
            chunk = []
    if chunk:
        await message.answer("\n".join(chunk), parse_mode="HTML")
        
# ==================== ЗАПУСК ====================

async def on_startup(bot: Bot):
    """Действия при запуске"""
    logger.info("=" * 50)
    logger.info("🤖 Bot starting up...")
    logger.info(f"Bot username: {(await bot.get_me()).username}")
    logger.info(f"Super Admin ID: {SUPER_ADMIN_ID}")
    logger.info(f"Sales Admins: {SALES_ADMIN_IDS}")
    logger.info(f"Production Admins: {PRODUCTION_ADMIN_IDS}")
    logger.info(f"Warehouse Admins: {WAREHOUSE_ADMIN_IDS}")
    logger.info(f"Rate limiting: ✅")
    logger.info(f"Database: MySQL at {DB_CONFIG['host']}:{DB_CONFIG['port']}")
    logger.info(f"Async FTP: {'✅' if AIOFTP_AVAILABLE else '⚠️  Fallback to sync'}")
    logger.info("=" * 50)

    try:
        init_db()
        logger.info("✅ Database initialized")
        
        # Миграция данных из локальных файлов в БД
        migrate_users_from_files()
    except Exception as e:
        logger.exception(f"❌ Database init failed: {e}")
        raise

    # ✅ Предзагружаем товары в кеш
    try:
        products = await fetch_products_from_sheets()
        logger.info(f"✅ Pre-loaded {len(products)} products into cache")
    except Exception as e:
        logger.warning(f"⚠️ Failed to pre-load products: {e}")



async def on_shutdown(bot: Bot):
    """Действия при остановке"""
    logger.info("🛑 Bot shutting down...")
    try:
        await bot.send_message(ADMIN_CHAT_ID, "🛑 Бот остановлен")
    except:
        pass

async def background_cache_updater():
    """Фоновое обновление кеша товаров"""
    await asyncio.sleep(60)  # Подождать 1 минуту после старта
    
    while True:
        try:
            await asyncio.sleep(1800)  # 30 минут
            products = await fetch_products_from_sheets()
            logger.info(f"🔄 Background cache update: {len(products)} products")
        except Exception as e:
            logger.exception(f"❌ Background cache update failed: {e}")

async def main():
    """Главная функция"""
    logger.info("Starting bot initialization...")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        logger.info("Starting polling...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.exception(f"Critical error: {e}")
    finally:
        await bot.session.close()

@router.message(Command("analysis"))
async def analysis_command(message: Message):
    user_id = message.from_user.id

    if user_id != SUPER_ADMIN_ID:
        return

    if not URL_ANALYSIS:
        await message.answer("❌ URL_ANALYSIS не указан в .env")
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Открыть Analysis",
                    web_app=WebAppInfo(url=URL_ANALYSIS)
                )
            ]
        ]
    )

    await message.answer(
        "📈 Панель анализа:",
        reply_markup=kb
    )
    
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
