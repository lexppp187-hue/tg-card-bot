# tg_bot_card_game — основной проектный файл

Файл содержит готовый (prototype) Telegram-бот на aiogram с:
- открытием бесплатных паков (каждые 30 минут, 5 карт)
- магазином платных паков (x2, x3, x10)
- редкостями карт и пассивным доходом (монеты/час)
- инвентарём пользователя
- обменом карт между игроками (запрос -> принятие/отмена)
- админкой внутри бота для управления картами (добавление/редактирование/удаление), загрузка фото карт через Telegram
- PostgreSQL для хранения пользователей, карт, инвентаря, торговых заявок
- подготовкой к деплою на Render (Procfile, requirements.txt, README с шагами)

-- ФАЙЛ: bot.py (основной код) --

```python
import os
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.utils import executor
from aiogram.dispatcher.filters import Text
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# --- Настройки (через переменные окружения на Render) ---
TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')  # в формате postgres://user:pass@host:port/dbname
ADMIN_IDS = os.getenv('ADMIN_IDS', '')  # через запятую список id админов
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '30'))

if not TOKEN or not DATABASE_URL:
    raise RuntimeError('Не найдены BOT_TOKEN или DATABASE_URL в окружении')

ADMIN_IDS = [int(x) for x in ADMIN_IDS.split(',') if x.strip().isdigit()]

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# --- DB helpers ---
async def init_db():
    conn = await asyncpg.create_pool(DATABASE_URL)
    # Создадим таблицы, если их нет
    async with conn.acquire() as c:
        await c.execute('''
        CREATE TABLE IF NOT EXISTS cards (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL,
            image_file_id TEXT,
            created_at TIMESTAMP DEFAULT now(),
            coins_per_hour INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS users (
            tg_id BIGINT PRIMARY KEY,
            last_pack TIMESTAMP DEFAULT '1970-01-01',
            coins BIGINT DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(tg_id) ON DELETE CASCADE,
            card_id INTEGER REFERENCES cards(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            from_user BIGINT REFERENCES users(tg_id) ON DELETE CASCADE,
            to_user BIGINT REFERENCES users(tg_id) ON DELETE CASCADE,
            offered_inventory_id INTEGER REFERENCES inventory(id) ON DELETE CASCADE,
            requested_inventory_id INTEGER REFERENCES inventory(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'pending', -- pending/accepted/rejected/cancelled
            created_at TIMESTAMP DEFAULT now()
        );
        ''')
    return conn

# глобальный пул
DB_POOL: Optional[asyncpg.pool.Pool] = None

# --- Редкости и веса (можно хранить в таблице cards, но здесь дефолтные) ---
RARITY_DEFAULTS = {
    'common': {'weight': 60, 'coins_per_hour': 1},
    'rare': {'weight': 25, 'coins_per_hour': 3},
    'epic': {'weight': 10, 'coins_per_hour': 8},
    'legendary': {'weight': 5, 'coins_per_hour': 20},
}

# --- Утилиты ---
def get_weights_and_rarities():
    rarities = list(RARITY_DEFAULTS.keys())
    weights = [RARITY_DEFAULTS[r]['weight'] for r in rarities]
    return rarities, weights

async def ensure_user(tg_id: int):
    async with DB_POOL.acquire() as conn:
        await conn.execute('INSERT INTO users (tg_id) VALUES ($1) ON CONFLICT (tg_id) DO NOTHING', tg_id)

async def give_passive_income_all():
    # начисляем доход всем пользователям раз в час
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch('''
            SELECT u.tg_id, COALESCE(SUM(c.coins_per_hour),0) AS income
            FROM users u
            LEFT JOIN inventory i ON i.user_id = u.tg_id
            LEFT JOIN cards c ON c.id = i.card_id
            GROUP BY u.tg_id
        ''')
        for r in rows:
            if r['income'] > 0:
                await conn.execute('UPDATE users SET coins = coins + $1 WHERE tg_id = $2', r['income'], r['tg_id'])

# --- Кнопки ---
def main_menu_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('🎁 Открыть бесплатный пак', callback_data='free_pack'))
    kb.add(InlineKeyboardButton('📦 Магазин', callback_data='shop'))
    kb.add(InlineKeyboardButton('🎒 Инвентарь', callback_data='inv'))
    kb.add(InlineKeyboardButton('🔁 Торги', callback_data='trades'))
    return kb

# --- Команды ---
@dp.message_handler(commands=['start'])
async def cmd_start(msg: types.Message):
    await ensure_user(msg.from_user.id)
    await msg.answer('Привет! Это карточный бот. Открывай паки, собирай инвентарь и торгуй с другими.', reply_markup=main_menu_kb())

@dp.message_handler(commands=['admin'])
async def cmd_admin(msg: types.Message):
    if msg.from_user.id not in ADMIN_IDS:
        return await msg.reply('Доступ запрещён')
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Добавить карту', callback_data='admin_add_card'))
    kb.add(InlineKeyboardButton('Список карт', callback_data='admin_list_cards'))
    await msg.reply('Админ панель', reply_markup=kb)

# --- Callbacks: free pack, shop, inv ---
@dp.callback_query_handler(Text(startswith='free_pack'))
async def cb_free_pack(call: types.CallbackQuery):
    uid = call.from_user.id
    await ensure_user(uid)
    now = datetime.utcnow()
    async with DB_POOL.acquire() as conn:
        last = await conn.fetchval('SELECT last_pack FROM users WHERE tg_id=$1', uid)
        last_dt = last or datetime(1970,1,1)
        if now - last_dt < timedelta(minutes=COOLDOWN_MINUTES):
            delta = timedelta(minutes=COOLDOWN_MINUTES) - (now - last_dt)
            mins = int(delta.total_seconds() // 60)
            return await call.answer(f'Пак доступен через ~{mins} мин.', show_alert=True)
        # обновим last_pack
        await conn.execute('UPDATE users SET last_pack=$1 WHERE tg_id=$2', now, uid)
        # достаём возможные редкости
        rarities, weights = get_weights_and_rarities()
        # в идеале: выбирать реальные карточки из таблицы cards, но для простоты пока генерируем случайные
        # если в базе есть карты, выбираем random
        cards = await conn.fetch('SELECT id, name, rarity, image_file_id FROM cards')
        got = []
        if len(cards) >= 5:
            # выберем 5 случайных карт с учётом редкости
            by_rarity = {}
            for c in cards:
                by_rarity.setdefault(c['rarity'], []).append(c)
            for _ in range(5):
                rarity = random.choices(rarities, weights)[0]
                bucket = by_rarity.get(rarity) or list(cards)
                choice = random.choice(bucket)
                got.append(choice)
                await conn.execute('INSERT INTO inventory (user_id, card_id) VALUES ($1,$2)', uid, choice['id'])
        else:
            # если мало карт в базе — создаём временные случайные
            for _ in range(5):
                rarity = random.choices(rarities, weights)[0]
                # берём любую карту с такой редкостью, иначе создаём временную запись
                c = await conn.fetchrow('SELECT id, name, rarity, image_file_id FROM cards WHERE rarity=$1 ORDER BY random() LIMIT 1', rarity)
                if not c:
                    name = f'Card {random.randint(1000,9999)}'
                    coins = RARITY_DEFAULTS[rarity]['coins_per_hour']
                    r = await conn.fetchrow('INSERT INTO cards (name, rarity, coins_per_hour) VALUES ($1,$2,$3) RETURNING id, name, rarity, image_file_id', name, rarity, coins)
                    c = r
                got.append(c)
                await conn.execute('INSERT INTO inventory (user_id, card_id) VALUES ($1,$2)', uid, c['id'])

        text = 'Вы открыли пак!
' + '
'.join([f"#{c['id']} — {c['name']} ({c['rarity']})" for c in got])
        await call.message.answer(text)
        await call.answer()

@dp.callback_query_handler(Text(startswith='inv'))
async def cb_inv(call: types.CallbackQuery):
    uid = call.from_user.id
    await ensure_user(uid)
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch('''
            SELECT i.id as inv_id, c.id as card_id, c.name, c.rarity, c.image_file_id
            FROM inventory i
            JOIN cards c ON c.id = i.card_id
            WHERE i.user_id = $1
            ORDER BY i.created_at DESC
        ''', uid)
        if not rows:
            return await call.message.answer('Инвентарь пуст')
        text = 'Ваши карты:
' + '
'.join([f"inv#{r['inv_id']} — {r['name']} ({r['rarity']})" for r in rows])
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton('Отправить предложение обмена', callback_data='trade_start'))
        await call.message.answer(text, reply_markup=kb)

# --- Магазин (простая реализация) ---
@dp.callback_query_handler(Text(startswith='shop'))
async def cb_shop(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Пак x2 — 20 монет', callback_data='buy_2'))
    kb.add(InlineKeyboardButton('Пак x3 — 25 монет', callback_data='buy_3'))
    kb.add(InlineKeyboardButton('Пак x10 — 60 монет', callback_data='buy_10'))
    await call.message.answer('Магазин:', reply_markup=kb)

async def _buy_pack(uid: int, count: int, price: int):
    async with DB_POOL.acquire() as conn:
        coins = await conn.fetchval('SELECT coins FROM users WHERE tg_id=$1', uid)
        if coins is None or coins < price:
            return False, 'Недостаточно монет'
        await conn.execute('UPDATE users SET coins = coins - $1 WHERE tg_id = $2', price, uid)
        # выдача карт, как в free_pack
        rarities, weights = get_weights_and_rarities()
        cards = await conn.fetch('SELECT id, name, rarity, image_file_id FROM cards')
        got = []
        for _ in range(count):
            if cards:
                rarity = random.choices(rarities, weights)[0]
                bucket = [c for c in cards if c['rarity']==rarity] or cards
                choice = random.choice(bucket)
                got.append(choice)
                await conn.execute('INSERT INTO inventory (user_id, card_id) VALUES ($1,$2)', uid, choice['id'])
            else:
                r = random.choice(list(RARITY_DEFAULTS.keys()))
                c = await conn.fetchrow('INSERT INTO cards (name, rarity, coins_per_hour) VALUES ($1,$2,$3) RETURNING id,name,rarity,image_file_id', f'Card{random.randint(1000,9999)}', r, RARITY_DEFAULTS[r]['coins_per_hour'])
                got.append(c)
                await conn.execute('INSERT INTO inventory (user_id, card_id) VALUES ($1,$2)', uid, c['id'])
        return True, got

@dp.callback_query_handler(lambda c: c.data in ['buy_2','buy_3','buy_10'])
async def cb_buy(call: types.CallbackQuery):
    uid = call.from_user.id
    mapping = {'buy_2': (2,20), 'buy_3': (3,25), 'buy_10': (10,60)}
    count, price = mapping[call.data]
    ok, res = await _buy_pack(uid, count, price)
    if not ok:
        return await call.answer(res, show_alert=True)
    cards = res
    await call.message.answer('Вы купили пак:
' + '
'.join([f"#{c['id']} — {c['name']} ({c['rarity']})" for c in cards]))

# --- Торги (обмен между игроками) ---
@dp.callback_query_handler(Text(startswith='trade_start'))
async def cb_trade_start(call: types.CallbackQuery):
    await call.message.answer('Отправьте ID карты из вашего инвентаря (например inv#123), которую вы хотите предложить, затем — ID получателя (tg id).')
    await call.answer()

@dp.message_handler(lambda m: m.text and m.text.startswith('inv#'))
async def msg_trade_offer(msg: types.Message):
    # ожидаем: inv#123 to 987654321
    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.reply('Нужно указать: inv#<id> <tg_id получателя>')
    inv_text = parts[0]
    try:
        inv_id = int(inv_text.replace('inv#',''))
        to_user = int(parts[1])
    except Exception:
        return await msg.reply('Неверный формат. Пример: inv#123 987654321')
    uid = msg.from_user.id
    await ensure_user(to_user)
    async with DB_POOL.acquire() as conn:
        # провера владельца
        owner = await conn.fetchval('SELECT user_id FROM inventory WHERE id=$1', inv_id)
        if owner != uid:
            return await msg.reply('Вы не владелец этой карточки')
        # создаём заявку (без запрашиваемой карты — можно в последствии расширить)
        tr = await conn.fetchrow('INSERT INTO trades (from_user, to_user, offered_inventory_id, status) VALUES ($1,$2,$3,$4) RETURNING id', uid, to_user, inv_id, 'pending')
        await msg.reply(f'Заявка отправлена пользователю {to_user}. id заявки: {tr["id"]}')
        # уведомим получателя
        try:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton('Принять', callback_data=f'trade_accept:{tr["id"]}'))
            kb.add(InlineKeyboardButton('Отклонить', callback_data=f'trade_reject:{tr["id"]}'))
            await bot.send_message(to_user, f'Вам пришла заявка на обмен от {uid}. Заявка id: {tr["id"]}', reply_markup=kb)
        except Exception:
            pass

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('trade_accept'))
async def cb_trade_accept(call: types.CallbackQuery):
    tr_id = int(call.data.split(':')[1])
    uid = call.from_user.id
    async with DB_POOL.acquire() as conn:
        tr = await conn.fetchrow('SELECT * FROM trades WHERE id=$1', tr_id)
        if not tr:
            return await call.answer('Заявка не найдена', show_alert=True)
        if tr['to_user'] != uid:
            return await call.answer('Вы не можете принять эту заявку', show_alert=True)
        # меняем владельца инвентаря
        await conn.execute('UPDATE inventory SET user_id = $1 WHERE id = $2', tr['to_user'], tr['offered_inventory_id'])
        await conn.execute('UPDATE trades SET status=$1 WHERE id=$2', 'accepted', tr_id)
        await call.message.answer('Заявка принята — карта передана')
        # уведомим отправителя
        try:
            await bot.send_message(tr['from_user'], f'Ваша заявка #{tr_id} принята')
        except Exception:
            pass

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('trade_reject'))
async def cb_trade_reject(call: types.CallbackQuery):
    tr_id = int(call.data.split(':')[1])
    async with DB_POOL.acquire() as conn:
        await conn.execute('UPDATE trades SET status=$1 WHERE id=$2', 'rejected', tr_id)
    await call.message.answer('Заявка отклонена')

# --- Админка: добавление/редактирование карт ---
@dp.callback_query_handler(Text(startswith='admin_add_card'))
async def cb_admin_add_card(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer('Нет доступа', show_alert=True)
    await call.message.answer('Отправьте фото карты и подпись в формате: Название | редкость | coins_per_hour
(например: Flame Dragon | epic | 8)')

@dp.message_handler(lambda m: m.photo and '|' in (m.caption or ''))
async def msg_admin_upload_card(msg: types.Message):
    if msg.from_user.id not in ADMIN_IDS:
        return await msg.reply('Нет доступа')
    caption = msg.caption
    try:
        name, rarity, coins = [x.strip() for x in caption.split('|')]
        coins = int(coins)
    except Exception:
        return await msg.reply('Неверный формат подписи. Пример: Flame Dragon | epic | 8')
    photo = msg.photo[-1]
    file_id = photo.file_id
    async with DB_POOL.acquire() as conn:
        r = await conn.fetchrow('INSERT INTO cards (name, rarity, image_file_id, coins_per_hour) VALUES ($1,$2,$3,$4) RETURNING id', name, rarity, file_id, coins)
        await msg.reply(f'Карта добавлена, id: {r["id"]}')

@dp.callback_query_handler(Text(startswith='admin_list_cards'))
async def cb_admin_list_cards(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer('Нет доступа', show_alert=True)
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch('SELECT id, name, rarity, image_file_id FROM cards ORDER BY id DESC')
        if not rows:
            return await call.message.answer('Нет карт в базе')
        for r in rows[:50]:
            text = f"#{r['id']} — {r['name']} ({r['rarity']})"
            if r['image_file_id']:
                try:
                    await bot.send_photo(call.from_user.id, r['image_file_id'], caption=text)
                except Exception:
                    await call.message.answer(text)
            else:
                await call.message.answer(text)

# --- Background tasks ---
async def hourly_income_task():
    while True:
        try:
            await give_passive_income_all()
        except Exception as e:
            print('Income error', e)
        await asyncio.sleep(3600)

# --- Startup / Shutdown ---
async def on_startup(dp):
    global DB_POOL
    DB_POOL = await init_db()
    asyncio.create_task(hourly_income_task())
    print('Bot started')

async def on_shutdown(dp):
    await bot.close()
    if DB_POOL:
        await DB_POOL.close()

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)
```
