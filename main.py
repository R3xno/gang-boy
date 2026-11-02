import asyncio, html, io, json, logging, os, random, re, time, uuid

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import db, imaging
from pokedata import (MAX_ID, LEGENDARY_IDS, NATURES, STAT_NAMES, TYPE_EMOJI, art_bytes, calc_stats,
                      exp_for_level, get_move, get_poke, ideal_nature, moves_at_level, slug_name)

TOKEN = "7422247259:AAFKNQX22FsPRbjcCCR0MXlTssyBsDM0aj4"
HTML = ParseMode.HTML
esc = html.escape

# ---------------- tuning ----------------
SHINY_WILD = 1 / 512        # real games: 1/4096
GUESS_TIME = 60
HINT_AT = (15, 30, 45)      # seconds after spawn
HINT_FRAC = (0.25, 0.40, 0.55)
COOLDOWN = 5                # seconds between rounds per chat
CANDY_PRICE = 50
DAILY = 500
MAX_LEVEL = 100
CRAFT_SUCCESS = 0.60
CRAFT_LEGEND = 0.03
CRAFT_SHINY_BASE, CRAFT_SHINY_PER = 0.02, 0.20
CRAFT_TTL = 180

ROUNDS = {}      # chat_id -> Round | PENDING
NEXT_OK = {}     # chat_id -> timestamp
PENDING = object()


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("♀", "f").replace("♂", "m"))


class Round:
    def __init__(self, d, level, nature, ivs, shiny):
        self.d, self.level, self.nature, self.ivs, self.shiny = d, level, nature, ivs, shiny
        self.done = False
        self.task = None
        self.accept = {norm(d["name"]), norm(d["slug"])}
        if d["slug"] in ("nidoran-f", "nidoran-m"):
            self.accept.add("nidoran")
        letters = [i for i, ch in enumerate(d["name"]) if ch.isalnum()]
        random.shuffle(letters)
        self.order = letters  # reveal order stays consistent across hints

    def hint(self, stage):
        name = self.d["name"]
        n = len(self.order)
        k = min(n - 1, max(1, round(n * HINT_FRAC[stage])))
        shown = set(self.order[:k])
        return "".join(ch if (not ch.isalnum() or i in shown) else "_" for i, ch in enumerate(name))


# ---------------- helpers ----------------
def uname(u):
    return u.full_name or u.username or str(u.id)


def mention(u):
    return f'<a href="tg://user?id={u.id}">{esc(uname(u))}</a>'


def pbar(row, d):
    lvl = row["level"]
    if lvl >= MAX_LEVEL:
        return "MAX", "▓" * 10
    cur, nxt = exp_for_level(d["growth"], lvl), exp_for_level(d["growth"], lvl + 1)
    frac = max(0, min(1, (row["exp"] - cur) / max(1, nxt - cur)))
    f = int(frac * 10)
    return f"{max(0, nxt - row['exp']):,}", "▓" * f + "░" * (10 - f)


def type_str(d, brackets=True):
    t = " / ".join(slug_name(x) for x in d["types"])
    return t


def title(row, d):
    nm = esc(row["nickname"] or d["name"])
    star = " ✨" if row["shiny"] else ""
    sub = f" ({esc(d['name'])})" if row["nickname"] else ""
    return f"<b>{nm}</b>{star}{sub}"


async def caption(row, d, view):
    ivs, evs = json.loads(row["ivs"]), json.loads(row["evs"])
    head = title(row, d)
    if view == "info":
        to_next, bar = pbar(row, d)
        types = " ".join(f"[{slug_name(t)} {TYPE_EMOJI.get(t, '')}]" for t in d["types"])
        return (f"{head}\nLv. {row['level']} | <b>Nature:</b> {row['nature']}\nType: {types}\n"
                f"Exp. {row['exp']:,}\nTo Next Lv. {to_next}\nEXP {bar}")
    if view == "stats":
        st = calc_stats(d["base"], ivs, evs, row["level"], row["nature"])
        pm = NATURES[row["nature"]]
        lines = []
        for i in range(6):
            mark = " (+)" if pm and pm[0] == i else " (-)" if pm and pm[1] == i else ""
            lines.append(f"<b>{STAT_NAMES[i]}:</b> {st[i]}{mark}")
        return head + "\n" + "\n".join(lines)
    if view == "ivs":
        rows = [f"{'Points':<13}{'IV':>3} | {'EV':>3}", "-" * 22]
        for i in range(6):
            rows.append(f"{STAT_NAMES[i]:<13}{ivs[i]:>3} | {evs[i]:>3}")
        rows += ["-" * 22, f"{'Total':<13}{sum(ivs):>3} | {sum(evs):>3}"]
        return head + f"\n<pre>{esc(chr(10).join(rows))}</pre>"
    if view == "moves":
        slugs = json.loads(row["moves"])
        infos = await asyncio.gather(*[get_move(m) for m in slugs], return_exceptions=True)
        out = [head]
        for slug, m in zip(slugs, infos):
            if isinstance(m, Exception):
                out.append(f"<b>{slug_name(slug)}</b>")
                continue
            out.append(f"<b>{esc(m['name'])}</b> [{slug_name(m['type'])} {TYPE_EMOJI.get(m['type'], '')}]\n"
                       f"Power: {m['power'] or '-'}, Accuracy: {m['acc'] or '-'} ({m['cls'].title()})"
                       + (f"\nEffect: {esc(m['effect'])}" if m["effect"] and m["cls"] == "status" else ""))
        return "\n".join(out)
    return head


def view_kb(pid, cur):
    names = [("info", "Info"), ("stats", "Stats"), ("moves", "Move Set"), ("ivs", "IVs/EVs")]
    row = [Btn(t, callback_data=f"v:{pid}:{k}") for k, t in names if k != cur]
    return Kb([row, [Btn("🗑 Release", callback_data=f"rel:{pid}")]])


async def send_card(chat_id, ctx, row, view="info", reply_to=None, prefix=""):
    d = await get_poke(row["species"])
    img = await asyncio.to_thread(imaging.card, await art_bytes(d, row["shiny"]))
    cap = prefix + await caption(row, d, view)
    await ctx.bot.send_photo(chat_id, img, caption=cap[:1024], parse_mode=HTML,
                             reply_markup=view_kb(row["id"], view), reply_to_message_id=reply_to)


def resolve(uid, args):
    """number argument -> row; else selected; else most recent."""
    if args and args[0].isdigit():
        return db.by_number(uid, int(args[0]))
    u = db.get_user(uid)
    if u["selected"]:
        r = db.pokemon(u["selected"])
        if r and r["owner"] == uid:
            return r
    return db.conn.execute("SELECT * FROM pokemon WHERE owner=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()


# ---------------- /start /help ----------------
async def start(update: Update, ctx):
    u = update.effective_user
    db.get_user(u.id, uname(u))
    await update.message.reply_text(
        "👋 <b>Welcome to the Pokémon bot!</b>\n\n"
        "/guess – start a Who's That Pokémon round (first correct guess keeps it)\n"
        "/mypokemon – your collection • /select n • /info [n] • /moves • /nickname name\n"
        "/shop • /buy candy n • /candy [n] – level up the selected Pokémon\n"
        "/daily – free coins • /balance • /sort • /display • /pagesize\n"
        "/craft n1 n2 n3 n4 – combine 4 non-legendary Pokémon into a better one (60% chance)",
        parse_mode=HTML)


# ---------------- /guess ----------------
async def guess(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    if chat in ROUNDS:
        return await update.message.reply_text("⏳ A round is already running in this chat!")
    wait = NEXT_OK.get(chat, 0) - time.time()
    if wait > 0:
        return await update.message.reply_text(f"Slow down! Next round in {int(wait) + 1}s.")
    ROUNDS[chat] = PENDING  # claim the slot before any await -> only one round per chat
    try:
        d = None
        for _ in range(4):
            try:
                d = await get_poke(random.randint(1, MAX_ID))
                art = await art_bytes(d, False)
                break
            except Exception:
                logging.exception("spawn fetch failed")
        if d is None:
            raise RuntimeError("PokeAPI unreachable")
        shiny = random.random() < SHINY_WILD
        rnd = Round(d, random.randint(5, 50), random.choice(list(NATURES)),
                    [random.randint(0, 31) for _ in range(6)], shiny)
        img = await asyncio.to_thread(imaging.silhouette, art)
        await ctx.bot.send_photo(chat, img, caption="❓ <b>Who's that Pokémon?</b>\nType its name to catch it! "
                                 f"({GUESS_TIME}s)", parse_mode=HTML)
    except Exception:
        ROUNDS.pop(chat, None)
        logging.exception("guess failed")
        return await update.message.reply_text("⚠️ Couldn't start a round, try again in a moment.")
    ROUNDS[chat] = rnd
    rnd.task = asyncio.create_task(round_flow(ctx, chat, rnd))


async def round_flow(ctx, chat, rnd: Round):
    try:
        t = 0
        for stage, at in enumerate(HINT_AT):
            await asyncio.sleep(at - t)
            t = at
            if rnd.done:
                return
            await ctx.bot.send_message(chat, f"💡 Hint: <code>{esc(rnd.hint(stage))}</code>", parse_mode=HTML)
        await asyncio.sleep(GUESS_TIME - t)
        if rnd.done:
            return
        rnd.done = True
        if ROUNDS.get(chat) is rnd:
            ROUNDS.pop(chat)
        NEXT_OK[chat] = time.time() + COOLDOWN
        art = await art_bytes(rnd.d, rnd.shiny)
        img = await asyncio.to_thread(imaging.reveal, art)
        await ctx.bot.send_photo(chat, img, caption=f"⏰ Time's up! It was <b>{esc(rnd.d['name'])}</b>.",
                                 parse_mode=HTML)
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("round_flow")
        if ROUNDS.get(chat) is rnd:
            ROUNDS.pop(chat)


async def on_text(update: Update, ctx):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not msg.text or not user or user.is_bot:
        return
    rnd = ROUNDS.get(chat.id)
    if not isinstance(rnd, Round) or rnd.done or norm(msg.text) not in rnd.accept:
        return
    # ---- winner: everything below up to the DB write has no await -> first correct guess wins ----
    rnd.done = True
    ROUNDS.pop(chat.id, None)
    NEXT_OK[chat.id] = time.time() + COOLDOWN
    if rnd.task:
        rnd.task.cancel()
    d = rnd.d
    coins = random.randint(100, 300)
    with db.tx() as c:
        db.get_user(user.id, uname(user))
        pid = db.insert_pokemon(c, user.id, d["id"], rnd.level, exp_for_level(d["growth"], rnd.level),
                                rnd.nature, rnd.shiny, rnd.ivs, moves_at_level(d["learnset"], rnd.level))
        c.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (coins, user.id))
        c.execute("UPDATE users SET selected=? WHERE user_id=? AND selected IS NULL", (pid, user.id))
    row = db.pokemon(pid)
    pre = (f"🎉 {mention(user)} guessed it right!\n" + ("✨ <b>It's SHINY!</b> ✨\n" if rnd.shiny else "")
           + f"💰 +{coins} Hexa coins\n\n")
    await send_card(chat.id, ctx, row, "info", reply_to=msg.message_id, prefix=pre)


# ---------------- collection ----------------
async def list_text(uid, page):
    u = db.get_user(uid)
    rows = db.all_of(uid)
    if not rows:
        return "You have no Pokémon yet. Use /guess in a group!", None
    nums = {r["id"]: i + 1 for i, r in enumerate(rows)}
    ds = await asyncio.gather(*[get_poke(s) for s in {r["species"] for r in rows}])
    data = {d["id"]: d for d in ds}
    key = u["sort_by"]
    if key == "level": rows = sorted(rows, key=lambda r: -r["level"])
    elif key == "iv": rows = sorted(rows, key=lambda r: -sum(json.loads(r["ivs"])))
    elif key == "name": rows = sorted(rows, key=lambda r: data[r["species"]]["name"])
    size = u["pagesize"]
    pages = max(1, -(-len(rows) // size))
    page = max(0, min(page, pages - 1))
    lines = []
    for r in rows[page * size:(page + 1) * size]:
        d = data[r["species"]]
        nm = esc(r["nickname"] or d["name"]) + (" ✨" if r["shiny"] else "")
        if u["display"] == "level": extra = f"Lv. {r['level']}"
        elif u["display"] == "iv": extra = f"IV {sum(json.loads(r['ivs'])) / 186 * 100:.1f}%"
        else: extra = type_str(d)
        lines.append(f"{nums[r['id']]}. <b>{nm}</b> - {extra}")
    sel = {"caught": "Order caught ↑", "level": "Level ↓", "iv": "IV total ↓", "name": "Name"}[key]
    txt = (("\n".join(lines)) + f"\n\nTotal Pokémon: {len(rows)}  (page {page + 1}/{pages})\n"
           f"/sort by: {sel}\n/display: {u['display'].title()}\n/pagesize: {size}")
    btns = []
    if page > 0: btns.append(Btn("◀", callback_data=f"pg:{uid}:{page - 1}"))
    if page < pages - 1: btns.append(Btn("▶", callback_data=f"pg:{uid}:{page + 1}"))
    return txt, Kb([btns]) if btns else None


async def mypokemon(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid, uname(update.effective_user))
    txt, kb = await list_text(uid, 0)
    await update.message.reply_text(txt, parse_mode=HTML, reply_markup=kb)


async def select(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid, uname(update.effective_user))
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Usage: /select <number from /mypokemon>")
    r = db.by_number(uid, int(ctx.args[0]))
    if not r:
        return await update.message.reply_text("You don't have a Pokémon with that number.")
    db.conn.execute("UPDATE users SET selected=? WHERE user_id=?", (r["id"], uid))
    d = await get_poke(r["species"])
    await update.message.reply_text(f"✅ Selected {title(r, d)} (Lv. {r['level']})", parse_mode=HTML)


async def info(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid, uname(update.effective_user))
    r = resolve(uid, ctx.args)
    if not r:
        return await update.message.reply_text("No Pokémon found. Use /guess to catch one!")
    await send_card(update.effective_chat.id, ctx, r, "info", reply_to=update.message.message_id)


async def nickname(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid, uname(update.effective_user))
    r = resolve(uid, [])
    name = " ".join(ctx.args).strip()
    if not r:
        return await update.message.reply_text("Select a Pokémon first (/select n).")
    if not name or len(name) > 20:
        return await update.message.reply_text("Usage: /nickname <name> (max 20 chars) — applies to the selected Pokémon.")
    db.conn.execute("UPDATE pokemon SET nickname=? WHERE id=? AND owner=?", (name, r["id"], uid))
    await update.message.reply_text(f"Nickname set to <b>{esc(name)}</b>.", parse_mode=HTML)


async def sort_cmd(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid)
    if not ctx.args or ctx.args[0] not in ("caught", "level", "iv", "name"):
        return await update.message.reply_text("Usage: /sort caught|level|iv|name")
    db.conn.execute("UPDATE users SET sort_by=? WHERE user_id=?", (ctx.args[0], uid))
    await update.message.reply_text("Sort updated.")


async def display_cmd(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid)
    if not ctx.args or ctx.args[0] not in ("type", "level", "iv"):
        return await update.message.reply_text("Usage: /display type|level|iv")
    db.conn.execute("UPDATE users SET display=? WHERE user_id=?", (ctx.args[0], uid))
    await update.message.reply_text("Display updated.")


async def pagesize_cmd(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid)
    if not ctx.args or not ctx.args[0].isdigit() or not 5 <= int(ctx.args[0]) <= 40:
        return await update.message.reply_text("Usage: /pagesize 5-40")
    db.conn.execute("UPDATE users SET pagesize=? WHERE user_id=?", (int(ctx.args[0]), uid))
    await update.message.reply_text("Page size updated.")


# ---------------- economy ----------------
async def balance(update: Update, ctx):
    u = db.get_user(update.effective_user.id, uname(update.effective_user))
    await update.message.reply_text(f"💰 Hexa coins: <b>{u['coins']:,}</b>\n🍬 Rare Candies: <b>{u['candies']:,}</b>",
                                    parse_mode=HTML)


async def daily(update: Update, ctx):
    uid = update.effective_user.id
    now = time.time()
    with db.tx() as c:
        u = db.get_user(uid, uname(update.effective_user))
        if now - u["last_daily"] < 86400:
            left = int(86400 - (now - u["last_daily"]))
            return await update.message.reply_text(f"Come back in {left // 3600}h {left % 3600 // 60}m.")
        c.execute("UPDATE users SET coins=coins+?, last_daily=? WHERE user_id=?", (DAILY, now, uid))
    await update.message.reply_text(f"🎁 +{DAILY} Hexa coins!")


async def shop(update: Update, ctx):
    await update.message.reply_text(
        f"🏪 <b>PokéStore</b>\n🍬 Rare Candy — {CANDY_PRICE} coins (+1 level)\n\n"
        "Buy: /buy candy <amount>\nUse: /candy [amount] on your selected Pokémon", parse_mode=HTML)


async def buy(update: Update, ctx):
    uid = update.effective_user.id
    if len(ctx.args) != 2 or ctx.args[0] != "candy" or not ctx.args[1].isdigit() or not 0 < int(ctx.args[1]) <= 1000:
        return await update.message.reply_text("Usage: /buy candy <1-1000>")
    qty = int(ctx.args[1])
    cost = qty * CANDY_PRICE
    with db.tx() as c:
        db.get_user(uid, uname(update.effective_user))
        cur = c.execute("UPDATE users SET coins=coins-?, candies=candies+? WHERE user_id=? AND coins>=?",
                        (cost, qty, uid, cost))  # single atomic conditional update
        if cur.rowcount == 0:
            return await update.message.reply_text("❌ Not enough Hexa coins.")
    await update.message.reply_text(f"🍬 Bought {qty} Rare Candy for {cost:,} coins.")


def learn_moves(c, row, d, old, new):
    moves = json.loads(row["moves"])
    queued = {r["move"] for r in c.execute("SELECT move FROM pending WHERE pid=?", (row["id"],))}
    auto, pend = [], []
    for lvl, m in d["learnset"]:
        if old < lvl <= new and m not in moves and m not in queued:
            if len(moves) < 4:
                moves.append(m); auto.append(m)
            else:
                c.execute("INSERT INTO pending(pid,move) VALUES(?,?)", (row["id"], m)); pend.append(m)
    c.execute("UPDATE pokemon SET moves=? WHERE id=?", (json.dumps(moves), row["id"]))
    return auto, pend


async def candy(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid, uname(update.effective_user))
    r = resolve(uid, [])
    if not r:
        return await update.message.reply_text("Select a Pokémon first with /select n.")
    want = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 1
    if want < 1:
        return await update.message.reply_text("Amount must be at least 1.")
    d = await get_poke(r["species"])
    with db.tx() as c:
        row = db.pokemon(r["id"])
        u = db.get_user(uid)
        if not row or row["owner"] != uid:
            return await update.message.reply_text("That Pokémon is gone.")
        if row["level"] >= MAX_LEVEL:
            return await update.message.reply_text("Already max level!")
        qty = min(want, u["candies"], MAX_LEVEL - row["level"])
        if qty <= 0:
            return await update.message.reply_text("🍬 You have no Rare Candies. Buy some with /buy candy n.")
        new = row["level"] + qty
        c.execute("UPDATE users SET candies=candies-? WHERE user_id=?", (qty, uid))
        c.execute("UPDATE pokemon SET level=?, exp=? WHERE id=?", (new, exp_for_level(d["growth"], new), row["id"]))
        auto, pend = learn_moves(c, row, d, row["level"], new)
    txt = f"🍬 Used {qty} Rare Candy — {title(row, d)} is now <b>Lv. {new}</b>!"
    if auto:
        txt += "\n📘 Learned: " + ", ".join(slug_name(m) for m in auto)
    await update.message.reply_text(txt, parse_mode=HTML)
    if pend:
        await move_prompt(ctx, update.effective_chat.id, row["id"])


async def move_prompt(ctx, chat_id, pid):
    p = db.conn.execute("SELECT * FROM pending WHERE pid=? ORDER BY id LIMIT 1", (pid,)).fetchone()
    row = db.pokemon(pid)
    if not p or not row:
        return
    d = await get_poke(row["species"])
    cur = json.loads(row["moves"])
    kb = [[Btn(f"Forget {slug_name(m)}", callback_data=f"mv:{p['id']}:{i}")] for i, m in enumerate(cur)]
    kb.append([Btn(f"Skip (don't learn {slug_name(p['move'])})", callback_data=f"mv:{p['id']}:x")])
    await ctx.bot.send_message(chat_id, f"{title(row, d)} wants to learn <b>{slug_name(p['move'])}</b>, but already knows 4 moves.\n"
                               "Which move should it forget?", parse_mode=HTML, reply_markup=Kb(kb))


async def moves_cmd(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid, uname(update.effective_user))
    r = resolve(uid, ctx.args)
    if not r:
        return await update.message.reply_text("No Pokémon found.")
    if db.conn.execute("SELECT 1 FROM pending WHERE pid=?", (r["id"],)).fetchone():
        return await move_prompt(ctx, update.effective_chat.id, r["id"])
    await send_card(update.effective_chat.id, ctx, r, "moves", reply_to=update.message.message_id)


# ---------------- crafting ----------------
def craft_ivs(rows):
    best_in = max(sum(json.loads(r["ivs"])) for r in rows)
    best = None
    for _ in range(300):
        iv = [max(random.randint(12, 31) for _ in range(3)) for _ in range(6)]
        if best is None or sum(iv) > sum(best):
            best = iv
        if sum(iv) > best_in:      # guaranteed better than every ingredient when possible
            return iv
    return best


async def craft(update: Update, ctx):
    uid = update.effective_user.id
    db.get_user(uid, uname(update.effective_user))
    args = ctx.args
    if len(args) != 4 or not all(a.isdigit() for a in args) or len(set(args)) != 4:
        return await update.message.reply_text(
            "⚗️ <b>Craft</b>: combine 4 different non-legendary Pokémon.\nUsage: /craft n1 n2 n3 n4 (numbers from /mypokemon)\n"
            f"Success chance {int(CRAFT_SUCCESS * 100)}%. On success you get one stronger Pokémon (better IVs & nature, "
            "small chance of a legendary, shiny ingredients boost shiny odds). On failure all 4 are lost.", parse_mode=HTML)
    rows = [db.by_number(uid, int(a)) for a in args]
    if any(r is None for r in rows):
        return await update.message.reply_text("One of those numbers isn't in your collection.")
    ds = await asyncio.gather(*[get_poke(r["species"]) for r in rows])
    if any(d["legendary"] for d in ds):
        return await update.message.reply_text("❌ Legendary/Mythical Pokémon can't be used for crafting.")
    imgs = await asyncio.gather(*[art_bytes(d, r["shiny"]) for d, r in zip(ds, rows)])
    pic = await asyncio.to_thread(imaging.collage, [(b, f"{r['nickname'] or d['name']} Lv{r['level']}" + (" ✨" if r["shiny"] else ""))
                                                    for b, d, r in zip(imgs, ds, rows)])
    sid = uuid.uuid4().hex[:12]
    with db.tx() as c:
        c.execute("DELETE FROM craft WHERE owner=? OR expires<?", (uid, time.time()))  # one open session per user
        c.execute("INSERT INTO craft(id,owner,ids,expires) VALUES(?,?,?,?)",
                  (sid, uid, json.dumps([r["id"] for r in rows]), time.time() + CRAFT_TTL))
    names = ", ".join(esc(r["nickname"] or d["name"]) for r, d in zip(rows, ds))
    await update.message.reply_photo(
        pic, caption=f"⚗️ <b>Craft these 4?</b>\n{names}\n\nSuccess chance: {int(CRAFT_SUCCESS * 100)}%\n"
                     "⚠️ If it fails, ALL 4 are lost.", parse_mode=HTML,
        reply_markup=Kb([[Btn("⚗️ Craft", callback_data=f"cf:{sid}"), Btn("✖ Cancel", callback_data=f"cx:{sid}")]]))


async def craft_confirm(q, ctx):
    uid = q.from_user.id
    sid = q.data.split(":")[1]
    s = db.conn.execute("SELECT * FROM craft WHERE id=? AND owner=?", (sid, uid)).fetchone()
    if not s or s["expires"] < time.time():
        db.conn.execute("DELETE FROM craft WHERE id=?", (sid,))
        return await q.answer("This craft offer expired.", show_alert=True)
    ids = json.loads(s["ids"])
    rows = [db.pokemon(i) for i in ids]
    if any(r is None or r["owner"] != uid for r in rows):
        db.conn.execute("DELETE FROM craft WHERE id=?", (sid,))
        return await q.answer("One of those Pokémon is no longer yours.", show_alert=True)
    ds = await asyncio.gather(*[get_poke(r["species"]) for r in rows])
    shinies = sum(r["shiny"] for r in rows)
    ok = random.random() < CRAFT_SUCCESS
    new = None
    if ok:
        pool = LEGENDARY_IDS if random.random() < CRAFT_LEGEND else None
        for _ in range(5):
            sid_new = random.choice(pool) if pool else random.choice([i for i in range(1, MAX_ID + 1) if i not in set(LEGENDARY_IDS)])
            try:
                nd = await get_poke(sid_new); break
            except Exception:
                nd = None
        if nd is None:
            return await q.answer("PokeAPI error, nothing was consumed. Try again.", show_alert=True)
        shiny = random.random() < min(0.9, CRAFT_SHINY_BASE + CRAFT_SHINY_PER * shinies)
        nature = ideal_nature(nd["base"]) if random.random() < 0.75 else random.choice(list(NATURES))
        lvl = max(5, round(sum(r["level"] for r in rows) / 4))
        new = (nd, lvl, nature, shiny, craft_ivs(rows))
    # ---- atomic: session is single-use, all 4 must still exist, then delete + insert ----
    with db.tx() as c:
        if c.execute("DELETE FROM craft WHERE id=? AND owner=?", (sid, uid)).rowcount != 1:
            return await q.answer("Already used.", show_alert=True)
        qs = ",".join("?" * 4)
        have = c.execute(f"SELECT COUNT(*) n FROM pokemon WHERE owner=? AND id IN ({qs})", (uid, *ids)).fetchone()["n"]
        if have != 4:
            return await q.answer("Craft cancelled: a Pokémon changed.", show_alert=True)
        c.execute(f"DELETE FROM pokemon WHERE id IN ({qs})", ids)
        c.execute(f"DELETE FROM pending WHERE pid IN ({qs})", ids)
        c.execute(f"UPDATE users SET selected=NULL WHERE selected IN ({qs})", ids)
        pid = None
        if new:
            nd, lvl, nature, shiny, ivs = new
            pid = db.insert_pokemon(c, uid, nd["id"], lvl, exp_for_level(nd["growth"], lvl), nature, shiny, ivs,
                                    moves_at_level(nd["learnset"], lvl))
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
    if pid:
        row = db.pokemon(pid)
        pre = ("⚗️ <b>Craft successful!</b>" + (" ✨ SHINY! ✨" if row["shiny"] else "") + "\n"
               + ("🌟 A legendary appeared!\n" if new[0]["legendary"] else "") + "\n")
        await send_card(q.message.chat_id, ctx, row, "info", prefix=pre)
    else:
        await ctx.bot.send_message(q.message.chat_id, "💥 <b>Craft failed!</b> All 4 Pokémon were lost.", parse_mode=HTML)


# ---------------- callbacks ----------------
async def on_cb(update: Update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    kind = parts[0]
    try:
        if kind == "v":
            pid, view = int(parts[1]), parts[2]
            row = db.pokemon(pid)
            if not row: return await q.answer("This Pokémon no longer exists.", show_alert=True)
            if row["owner"] != uid: return await q.answer("This isn't your Pokémon!", show_alert=True)
            d = await get_poke(row["species"])
            await q.answer()
            try:
                await q.edit_message_caption((await caption(row, d, view))[:1024], parse_mode=HTML,
                                             reply_markup=view_kb(pid, view))
            except BadRequest as e:
                if "not modified" not in str(e).lower(): raise
        elif kind == "pg":
            if int(parts[1]) != uid: return await q.answer("Not your list!", show_alert=True)
            txt, kb = await list_text(uid, int(parts[2]))
            await q.answer()
            try:
                await q.edit_message_text(txt, parse_mode=HTML, reply_markup=kb)
            except BadRequest:
                pass
        elif kind == "rel":
            row = db.pokemon(int(parts[1]))
            if not row or row["owner"] != uid: return await q.answer("Not available.", show_alert=True)
            await q.answer()
            await q.message.reply_text("Release this Pokémon forever?", reply_markup=Kb(
                [[Btn("Yes, release", callback_data=f"relc:{row['id']}"), Btn("No", callback_data="relx")]]))
        elif kind == "relx":
            await q.answer(); await q.message.delete()
        elif kind == "relc":
            pid = int(parts[1])
            with db.tx() as c:
                if c.execute("DELETE FROM pokemon WHERE id=? AND owner=?", (pid, uid)).rowcount != 1:
                    return await q.answer("Not available.", show_alert=True)
                c.execute("DELETE FROM pending WHERE pid=?", (pid,))
                c.execute("UPDATE users SET selected=NULL WHERE selected=?", (pid,))
            await q.answer("Released.")
            await q.edit_message_text("Pokémon released. Bye! 👋")
        elif kind == "mv":
            p = db.conn.execute("SELECT * FROM pending WHERE id=?", (int(parts[1]),)).fetchone()
            if not p: return await q.answer("Already handled.", show_alert=True)
            row = db.pokemon(p["pid"])
            if not row or row["owner"] != uid: return await q.answer("Not yours!", show_alert=True)
            with db.tx() as c:
                if c.execute("DELETE FROM pending WHERE id=?", (p["id"],)).rowcount != 1:
                    return await q.answer("Already handled.", show_alert=True)
                cur = json.loads(row["moves"])
                if parts[2] == "x":
                    msg = f"Skipped {slug_name(p['move'])}."
                else:
                    i = int(parts[2])
                    if not 0 <= i < len(cur) or p["move"] in cur: return await q.answer("Invalid.", show_alert=True)
                    old, cur[i] = cur[i], p["move"]
                    c.execute("UPDATE pokemon SET moves=? WHERE id=?", (json.dumps(cur), row["id"]))
                    msg = f"✅ Forgot {slug_name(old)} and learned {slug_name(p['move'])}!"
            await q.answer()
            await q.edit_message_text(msg)
            await move_prompt(ctx, q.message.chat_id, row["id"])
        elif kind == "cf":
            await craft_confirm(q, ctx)
        elif kind == "cx":
            db.conn.execute("DELETE FROM craft WHERE id=? AND owner=?", (parts[1], uid))
            await q.answer("Craft cancelled.")
            await q.message.delete()
        else:
            await q.answer()
    except Exception:
        logging.exception("callback error")
        try: await q.answer("Something went wrong.", show_alert=True)
        except Exception: pass


def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(TOKEN).build()
    for name, fn in [("start", start), ("help", start), ("guess", guess), ("mypokemon", mypokemon), ("select", select),
                     ("info", info), ("nickname", nickname), ("sort", sort_cmd), ("display", display_cmd),
                     ("pagesize", pagesize_cmd), ("balance", balance), ("daily", daily), ("shop", shop),
                     ("buy", buy), ("candy", candy), ("moves", moves_cmd), ("craft", craft)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
