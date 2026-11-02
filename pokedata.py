import asyncio, json, os
import httpx
import db

API = "https://pokeapi.co/api/v2"
MAX_ID = 1025
os.makedirs("cache/img", exist_ok=True)
_client = None

LEGENDARY_IDS = (list(range(144, 147)) + [150, 151] + list(range(243, 252)) + list(range(377, 387))
                 + list(range(480, 495)) + list(range(638, 650)) + list(range(716, 722))
                 + list(range(785, 810)) + list(range(888, 899)) + [905, 1001, 1002, 1003, 1004]
                 + list(range(1007, 1011)) + list(range(1014, 1026)))

TYPE_EMOJI = {"normal": "⚪", "fire": "🔥", "water": "💧", "electric": "⚡", "grass": "🌿", "ice": "❄️",
              "fighting": "🥊", "poison": "☠️", "ground": "⛰", "flying": "🕊", "psychic": "🔮", "bug": "🐛",
              "rock": "🪨", "ghost": "👻", "dragon": "🐉", "dark": "🌑", "steel": "⚙️", "fairy": "🧚"}

STAT_NAMES = ["HP", "Attack", "Defense", "Sp. Attack", "Sp. Defense", "Speed"]

# (plus_index, minus_index) ; indices: 0 hp,1 atk,2 def,3 spa,4 spd,5 spe
NATURES = {"Hardy": None, "Lonely": (1, 2), "Brave": (1, 5), "Adamant": (1, 3), "Naughty": (1, 4),
           "Bold": (2, 1), "Docile": None, "Relaxed": (2, 5), "Impish": (2, 3), "Lax": (2, 4),
           "Timid": (5, 1), "Hasty": (5, 2), "Serious": None, "Jolly": (5, 3), "Naive": (5, 4),
           "Modest": (3, 1), "Mild": (3, 2), "Quiet": (3, 5), "Bashful": None, "Rash": (3, 4),
           "Calm": (4, 1), "Gentle": (4, 2), "Sassy": (4, 5), "Careful": (4, 3), "Quirky": None}


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20, follow_redirects=True)
    return _client


async def _fetch(path):
    for i in range(3):
        try:
            r = await client().get(API + path)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == 2:
                raise
            await asyncio.sleep(1 + i)


def slug_name(s):
    return s.replace("-", " ").title()


async def get_poke(sid):
    c = db.cache_get(f"poke:{sid}")
    if c:
        return json.loads(c)
    p, s = await asyncio.gather(_fetch(f"/pokemon/{sid}"), _fetch(f"/pokemon-species/{sid}"))
    en = [n["name"] for n in s["names"] if n["language"]["name"] == "en"]
    art = p["sprites"]["other"]["official-artwork"]
    entries = []
    for m in p["moves"]:
        for v in m["version_group_details"]:
            if v["move_learn_method"]["name"] == "level-up":
                vg = int(v["version_group"]["url"].rstrip("/").split("/")[-1])
                entries.append((vg, max(1, v["level_learned_at"]), m["move"]["name"]))
    learn = {}
    if entries:
        best = max(e[0] for e in entries)  # most recent game this Pokémon appears in
        for vg, lvl, mv in sorted(entries, key=lambda e: e[1]):
            if vg == best and mv not in learn:
                learn[mv] = lvl
    d = {
        "id": sid, "slug": s["name"], "name": en[0] if en else slug_name(s["name"]),
        "types": [t["type"]["name"] for t in sorted(p["types"], key=lambda t: t["slot"])],
        "base": [st["base_stat"] for st in p["stats"]],  # hp, atk, def, spa, spd, spe
        "legendary": bool(s["is_legendary"] or s["is_mythical"]),
        "growth": s["growth_rate"]["name"],
        "art": art.get("front_default") or p["sprites"]["other"]["home"]["front_default"] or p["sprites"]["front_default"],
        "shiny_art": art.get("front_shiny") or p["sprites"]["other"]["home"]["front_shiny"],
        "learnset": sorted(([l, m] for m, l in learn.items()), key=lambda x: x[0]),
    }
    db.cache_set(f"poke:{sid}", json.dumps(d))
    return d


async def get_move(slug):
    c = db.cache_get(f"move:{slug}")
    if c:
        return json.loads(c)
    m = await _fetch(f"/move/{slug}")
    eff = ""
    for e in m.get("effect_entries", []):
        if e["language"]["name"] == "en":
            eff = e["short_effect"].replace("$effect_chance", str(m.get("effect_chance") or ""))
    en = [n["name"] for n in m["names"] if n["language"]["name"] == "en"]
    d = {"name": en[0] if en else slug_name(slug), "type": m["type"]["name"], "power": m["power"],
         "acc": m["accuracy"], "cls": m["damage_class"]["name"], "effect": eff}
    db.cache_set(f"move:{slug}", json.dumps(d))
    return d


async def art_bytes(d, shiny):
    url = (d["shiny_art"] if shiny and d["shiny_art"] else d["art"])
    path = f"cache/img/{d['id']}_{int(bool(shiny and d['shiny_art']))}.png"
    if os.path.exists(path):
        return open(path, "rb").read()
    r = await client().get(url)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return r.content


# ---------- game math ----------
def exp_for_level(growth, n):
    if n <= 1:
        return 0
    c = n ** 3
    if growth == "fast":
        v = 4 * c // 5
    elif growth == "medium-slow":
        v = 6 * c // 5 - 15 * n * n + 100 * n - 140
    elif growth == "slow":
        v = 5 * c // 4
    elif growth == "slow-then-very-fast":  # erratic
        if n <= 50: v = c * (100 - n) // 50
        elif n <= 68: v = c * (150 - n) // 100
        elif n <= 98: v = c * ((1911 - 10 * n) // 3) // 500
        else: v = c * (160 - n) // 100
    elif growth == "fast-then-very-slow":  # fluctuating
        if n <= 15: v = c * ((n + 1) // 3 + 24) // 50
        elif n <= 36: v = c * (n + 14) // 50
        else: v = c * (n // 2 + 32) // 50
    else:  # medium
        v = c
    return max(0, v)


def calc_stats(base, ivs, evs, level, nature):
    pm = NATURES.get(nature)
    out = []
    for i in range(6):
        core = (2 * base[i] + ivs[i] + evs[i] // 4) * level // 100
        if i == 0:
            out.append(core + level + 10)
        else:
            v = core + 5
            if pm and pm[0] == i: v = v * 110 // 100
            elif pm and pm[1] == i: v = v * 90 // 100
            out.append(v)
    return out


def ideal_nature(base):
    atk, spa, spe = base[1], base[3], base[5]
    if spe >= max(atk, spa) + 20:
        plus, minus = 5, (3 if atk >= spa else 1)
    elif atk >= spa:
        plus, minus = 1, 3
    else:
        plus, minus = 3, 1
    return next(n for n, v in NATURES.items() if v == (plus, minus))


def moves_at_level(learnset, level):
    known = [m for l, m in learnset if l <= level]
    return known[-4:]
