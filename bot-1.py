"""
Телеграм-бот модерации чата по правилам.
Автоматически: п.1 мат, п.2 оскорбления, п.3 флуд, п.5 политика, п.7 (по ссылкам на 18+).
Вручную (п.4 реклама, п.6 медиа и всё остальное): /mute, /ban, /unmute, /unban, /mods, /rules
или быстрые слова без слеша: мут, размут, бан, разбан, модеры, правила.
Владелец (п.8) может всё и без причины. Модеры (п.9) — только с номером пункта правил.
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message

TOKEN = os.getenv("BOT_TOKEN")                       # задаётся в переменных окружения хостинга
OWNER_ID = int(os.getenv("OWNER_ID", "0"))           # Telegram ID владельца
if not TOKEN or not OWNER_ID:
    raise SystemExit("Задай переменные окружения BOT_TOKEN и OWNER_ID")
MODERATORS_FILE = "moderators.json"                  # список ID модеров
LOG_FILE = "actions.log"

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN)
dp = Dispatcher()

RULES_TEXT = """📜 Правила чата:
1. Нецензурная лексика — мут 30 минут.
2. Оскорбления — мут 1 день.
3. Флуд — мут 1 час.
4. Реклама, спам — мут 1 неделя.
5. Обсуждение политики — мут 3 часа + удаление сообщений.
6. Медиа с матом — удаление и мут 30 минут (по решению модера).
7. Порнография — удаление и мут 1 неделя, повторно — бан.
8. Владелец может наказать/помиловать на любой срок без объяснений.
9. Модеры наказывают только по правилам, иначе — снятие с должности.

Перед вопросом о причине наказания перечитайте правила."""

# Срок наказания по пункту правил (секунды). None — бан.
RULE_DURATION = {
    1: 1800, 2: 86400, 3: 3600, 4: 7 * 86400,
    5: 3 * 3600, 6: 1800, 7: 7 * 86400,
}

# --- Словари (дополняй по вкусу; проверка по корням) ---
PROFANITY = [r"х[уy][йиеяю]", r"п[иі]зд", r"\bе[бб]а[тлн]", r"\bёб", r"бля", r"сук[аи]\b",
             r"муд[аи]к", r"залуп", r"г[ао]ндон", r"\bнах\b", r"пид[оа]р"]
INSULTS = [r"\bдебил", r"\bидиот", r"\bтуп(ой|ая|ица)", r"\bдаун\b", r"\bурод",
           r"\bчмо\b", r"\bлох\b", r"\bкретин", r"\bдегенерат", r"\bшлюх"]
POLITICS = [r"путин", r"зеленск", r"навальн", r"байден", r"трамп", r"\bвыбор(ы|ах)\b",
            r"госдум", r"\bсво\b", r"мобилизац", r"оппозици", r"\bкремл"]
PORN = [r"porn", r"xvideos", r"pornhub", r"onlyfans", r"\bпорн", r"xxx"]

def _compile(lst): return re.compile("|".join(lst), re.IGNORECASE)
RE_PROF, RE_INS, RE_POL, RE_PORN = map(_compile, (PROFANITY, INSULTS, POLITICS, PORN))

# Флуд: больше FLOOD_COUNT сообщений за FLOOD_WINDOW секунд или 3 одинаковых подряд
FLOOD_COUNT, FLOOD_WINDOW = 6, 10
history: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=20))      # (chat,user) -> [(time, text, msg_id)]
porn_strikes: dict[tuple, int] = defaultdict(int)


def load_mods() -> dict[int, str]:
    """{id: имя}"""
    try:
        with open(MODERATORS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if isinstance(data, list):                       # старый формат — просто список ID
        return {int(i): str(i) for i in data}
    return {int(k): v for k, v in data.items()}

def save_mods(mods: dict[int, str]):
    with open(MODERATORS_FILE, "w", encoding="utf-8") as f:
        json.dump(mods, f, ensure_ascii=False)

MODS = load_mods()

def log_action(text: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    logging.info(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def is_owner(uid): return uid == OWNER_ID
def is_staff(uid): return is_owner(uid) or uid in MODS

def fmt(sec: int) -> str:
    if sec >= 86400: return f"{sec // 86400} дн."
    if sec >= 3600: return f"{sec // 3600} ч."
    return f"{sec // 60} мин."

def parse_duration(s: str) -> int | None:
    m = re.fullmatch(r"(\d+)([mhdw])", s.lower())
    if not m: return None
    return int(m[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]


async def mute(chat_id, user_id, seconds):
    await bot.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False),
                                   until_date=timedelta(seconds=max(seconds, 60)))

async def unmute(chat_id, user_id):
    await bot.restrict_chat_member(chat_id, user_id, ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
        can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True))

async def punish(msg: Message, rule: int, delete_ids=()):
    user = msg.from_user
    for mid in {msg.message_id, *delete_ids}:
        try: await bot.delete_message(msg.chat.id, mid)
        except Exception: pass
    sec = RULE_DURATION[rule]
    await mute(msg.chat.id, user.id, sec)
    log_action(f"AUTO rule {rule}: {user.full_name} ({user.id}) mute {fmt(sec)}")
    await msg.answer(f"🔇 {user.mention_html()} — мут на {fmt(sec)} (п.{rule} правил).", parse_mode="HTML")


# ---------------- Команды ----------------
@dp.message(Command("rules", "start"))
async def cmd_rules(msg: Message):
    await msg.answer(RULES_TEXT)

@dp.message(Command("addmod", "delmod"))
async def cmd_mods(msg: Message, command: CommandObject):
    if not is_owner(msg.from_user.id) or not msg.reply_to_message:
        return await msg.reply("Только владелец, ответом на сообщение человека.")
    target = msg.reply_to_message.from_user
    if command.command == "addmod":
        MODS[target.id] = target.full_name
    else:
        MODS.pop(target.id, None)
    save_mods(MODS)
    log_action(f"OWNER {command.command} {target.id}")
    await msg.reply("✅ Готово.")

@dp.message(Command("mods"))
async def cmd_mods_list(msg: Message):
    lines = [f'👑 <a href="tg://user?id={OWNER_ID}">Владелец</a>']
    lines += [f'🛡 <a href="tg://user?id={uid}">{name}</a>' for uid, name in MODS.items()]
    if not MODS:
        lines.append("Модераторов пока нет.")
    await msg.answer("Администрация чата:\n" + "\n".join(lines), parse_mode="HTML")

def target_and_args(msg: Message, command: CommandObject):
    if not msg.reply_to_message:
        return None, []
    return msg.reply_to_message.from_user, (command.args or "").split()

@dp.message(Command("mute", "ban"))
async def cmd_punish(msg: Message, command: CommandObject):
    """
    Модер:    /mute <пункт>          — срок берётся из правил
              /ban 7                 — бан только по п.7 при повторе
    Владелец: /mute 2h | /mute 5     — любой срок или пункт; /ban без причины
    """
    actor = msg.from_user
    if not is_staff(actor.id):
        return
    target, args = target_and_args(msg, command)
    if not target:
        return await msg.reply("Ответь на сообщение нарушителя.")
    if is_staff(target.id) and not is_owner(actor.id):
        return await msg.reply("Модеров может наказывать только владелец.")

    rule = int(args[0]) if args and args[0].isdigit() else None
    if command.command == "ban":
        if not is_owner(actor.id) and not (rule == 7 and porn_strikes[(msg.chat.id, target.id)] >= 1):
            return await msg.reply("❌ П.9: модер может банить только по п.7 при повторном нарушении.")
        await bot.ban_chat_member(msg.chat.id, target.id)
        reason = f"п.{rule}" if rule else "решение владельца (п.8)"
        log_action(f"{'OWNER' if is_owner(actor.id) else 'MOD'} {actor.id} BAN {target.id} {reason}")
        return await msg.answer(f"⛔ {target.mention_html()} забанен ({reason}).", parse_mode="HTML")

    if is_owner(actor.id):
        sec = parse_duration(args[0]) if args else None
        sec = sec or (RULE_DURATION.get(rule) if rule else None) or 3600
        reason = f"п.{rule}" if rule in RULE_DURATION else "п.8"
    else:
        if rule not in RULE_DURATION:
            return await msg.reply("❌ П.9: укажи номер нарушенного пункта (1–7), например /mute 3.")
        sec, reason = RULE_DURATION[rule], f"п.{rule}"
    if rule == 7:
        porn_strikes[(msg.chat.id, target.id)] += 1
    if rule in (5, 6, 7):
        try: await msg.reply_to_message.delete()
        except Exception: pass
    await mute(msg.chat.id, target.id, sec)
    log_action(f"{'OWNER' if is_owner(actor.id) else 'MOD'} {actor.id} MUTE {target.id} {fmt(sec)} {reason}")
    await msg.answer(f"🔇 {target.mention_html()} — мут на {fmt(sec)} ({reason}).", parse_mode="HTML")

@dp.message(Command("unmute", "unban"))
async def cmd_forgive(msg: Message, command: CommandObject):
    # Размут — любой модер (п.1 «размут по желанию»), разбан — только владелец.
    if not is_staff(msg.from_user.id):
        return
    if command.command == "unban" and not is_owner(msg.from_user.id):
        return await msg.reply("Разбан — только владелец.")
    target, args = target_and_args(msg, command)
    uid = target.id if target else (int(args[0]) if (args := (command.args or "").split()) and args[0].isdigit() else None)
    if not uid:
        return await msg.reply("Ответь на сообщение или укажи ID.")
    if command.command == "unban":
        await bot.unban_chat_member(msg.chat.id, uid, only_if_banned=True)
    else:
        await unmute(msg.chat.id, uid)
    log_action(f"{msg.from_user.id} {command.command.upper()} {uid}")
    await msg.reply("✅ Готово.")


# ---------------- Быстрые команды без слеша ----------------
QUICK = {"мут": "mute", "размут": "unmute", "бан": "ban", "разбан": "unban",
         "модеры": "mods", "правила": "rules"}
QUICK_HANDLERS = {"mute": cmd_punish, "ban": cmd_punish, "unmute": cmd_forgive, "unban": cmd_forgive}

@dp.message(F.text.regexp(r"(?i)^(мут|размут|бан|разбан|модеры|правила)(\s|$)"))
async def quick_command(msg: Message):
    word, _, args = msg.text.strip().partition(" ")
    cmd = QUICK[word.lower()]
    if cmd == "rules":
        return await cmd_rules(msg)
    if cmd == "mods":
        return await cmd_mods_list(msg)
    await QUICK_HANDLERS[cmd](msg, CommandObject(prefix="", command=cmd, args=args.strip() or None))


# ---------------- Автомодерация ----------------
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def auto_moderate(msg: Message):
    user = msg.from_user
    if not user or is_staff(user.id):
        return
    text = (msg.text or msg.caption or "").lower().replace("ё", "е")
    key = (msg.chat.id, user.id)
    now = time.time()
    h = history[key]
    h.append((now, text, msg.message_id))

    if RE_PORN.search(text):
        porn_strikes[key] += 1
        if porn_strikes[key] >= 2:
            await msg.delete()
            await bot.ban_chat_member(msg.chat.id, user.id)
            log_action(f"AUTO rule 7 repeat: BAN {user.id}")
            return await msg.answer(f"⛔ {user.mention_html()} забанен (п.7, повтор).", parse_mode="HTML")
        return await punish(msg, 7)
    if RE_POL.search(text):
        # удаляем последние сообщения пользователя про политику
        ids = [mid for _, t, mid in h if RE_POL.search(t)]
        return await punish(msg, 5, ids)
    if RE_INS.search(text):
        return await punish(msg, 2)
    if RE_PROF.search(text):
        return await punish(msg, 1)

    recent = [x for x in h if now - x[0] <= FLOOD_WINDOW]
    last3 = list(h)[-3:]
    if len(recent) > FLOOD_COUNT or (len(last3) == 3 and text and all(t == text for _, t, _ in last3)):
        h.clear()
        return await punish(msg, 3, [mid for _, _, mid in recent])


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
