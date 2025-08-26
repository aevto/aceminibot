# app.py — Render Web Service (Python + FastAPI + Postgres via psycopg3)
import os, httpx, math
from fastapi import FastAPI, Request, Header
from fastapi.responses import PlainTextResponse

from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DATABASE_URL = os.environ["DATABASE_URL"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()

# small async pool
pool = AsyncConnectionPool(
    DATABASE_URL,
    min_size=1, max_size=3,
    kwargs={"autocommit": True}  # we use simple statements; autocommit is fine here
)

ACT_MAP = {1:1.2, 2:1.375, 3:1.55, 4:1.725, 5:1.9}

HELP = (
"Hi! I can store your fitness profile and do quick checks.\n\n"
"Commands:\n"
"/setprofile Name, Sex(M/F), Age, Height_cm, Weight_kg, Activity(1-5)\n"
"  e.g.  /setprofile Ace, M, 22, 175, 76, 3\n\n"
"/profile  → show your saved data\n"
"/bmi      → your BMI + category\n"
"/cutcal   → daily calories to lose weight (modest cut)\n"
"/edit field value  → update one item (fields: name, sex, age, height, weight, activity)\n"
"  e.g.  /edit weight 74.5\n"
)

def clean_int(x): return int(str(x).strip())
def clean_float(x): return float(str(x).strip())

def bmi_value(height_cm, weight_kg):
    h_m = height_cm / 100.0
    return weight_kg / (h_m*h_m)

def bmi_label(b):
    if b < 18.5: return "Underweight"
    if b < 25:   return "Normal"
    if b < 30:   return "Overweight"
    return "Obese"

def mifflin_bmr(sex, age, height_cm, weight_kg):
    if sex == 'M':
        return 10*weight_kg + 6.25*height_cm - 5*age + 5
    else:
        return 10*weight_kg + 6.25*height_cm - 5*age - 161

def tdee(sex, age, height_cm, weight_kg, activity):
    return mifflin_bmr(sex, age, height_cm, weight_kg) * ACT_MAP.get(activity, 1.2)

async def send(chat_id, text):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})

async def upsert_profile(user_id, chat_id, name=None, sex=None, age=None, height_cm=None, weight_kg=None, activity=None):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM user_profile WHERE user_id=%s", (user_id,))
            exists = await cur.fetchone()
        async with conn.cursor() as cur:
            if exists:
                await cur.execute("""
                  UPDATE user_profile
                  SET name=COALESCE(%s,name),
                      sex=COALESCE(%s,sex),
                      age=COALESCE(%s,age),
                      height_cm=COALESCE(%s,height_cm),
                      weight_kg=COALESCE(%s,weight_kg),
                      activity=COALESCE(%s,activity),
                      updated_at=now()
                  WHERE user_id=%s
                """, (name, sex, age, height_cm, weight_kg, activity, user_id))
            else:
                await cur.execute("""
                  INSERT INTO user_profile (user_id, chat_id, name, sex, age, height_cm, weight_kg, activity)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (user_id, chat_id, name, sex, age, height_cm, weight_kg, activity))

async def get_profile(user_id):
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM user_profile WHERE user_id=%s", (user_id,))
            return await cur.fetchone()

@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")

@app.post("/telegram")
async def telegram(req: Request, x_telegram_bot_api_secret_token: str | None = Header(None)):
    if WEBHOOK_SECRET and (x_telegram_bot_api_secret_token != WEBHOOK_SECRET):
        return PlainTextResponse("unauthorized", status_code=401)

    data = await req.json()
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return PlainTextResponse("ok")

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        await send(chat_id, "Use /start for help.")
        return PlainTextResponse("ok")

    cmd, *rest = text.split(" ", 1)
    cmd = cmd.lower()

    if cmd in ("/start", "/help"):
        await send(chat_id, HELP)

    elif cmd == "/profile":
        row = await get_profile(user_id)
        if not row:
            await send(chat_id, "No profile yet. Set it with:\n/setprofile Name, Sex(M/F), Age, Height_cm, Weight_kg, Activity(1-5)")
        else:
            msgp = (f"Your profile:\n"
                    f"Name: {row.get('name')}\nSex: {row.get('sex')}\nAge: {row.get('age')}\n"
                    f"Height: {row.get('height_cm')} cm\nWeight: {row.get('weight_kg')} kg\n"
                    f"Activity (1-5): {row.get('activity')}")
            await send(chat_id, msgp)

    elif cmd == "/setprofile":
        if not rest:
            await send(chat_id, "Format:\n/setprofile Name, Sex(M/F), Age, Height_cm, Weight_kg, Activity(1-5)")
        else:
            parts = [p.strip() for p in rest[0].split(",")]
            if len(parts) != 6:
                await send(chat_id, "Please send exactly 6 items, e.g.\n/setprofile Ace, M, 22, 175, 76, 3")
            else:
                try:
                    name = parts[0]
                    sex = parts[1].upper()
                    age = clean_int(parts[2])
                    height_cm = clean_int(parts[3])
                    weight_kg = clean_float(parts[4])
                    activity = clean_int(parts[5])
                    if sex not in ("M","F") or activity not in (1,2,3,4,5):
                        raise ValueError("bad sex/activity")
                    await upsert_profile(user_id, chat_id, name, sex, age, height_cm, weight_kg, activity)
                    await send(chat_id, "Saved ✅  (Use /profile to check)")
                except Exception:
                    await send(chat_id, "Could not read that. Example:\n/setprofile Ace, M, 22, 175, 76, 3")

    elif cmd == "/edit":
        if not rest:
            await send(chat_id, "Format:\n/edit field value\nFields: name, sex(M/F), age, height, weight, activity(1-5)")
        else:
            try:
                field, value = rest[0].split(" ", 1)
                field = field.lower().strip()
                value = value.strip()
                updates = {}
                if field == "name":
                    updates["name"] = value
                elif field == "sex":
                    if value.upper() not in ("M","F"): raise ValueError
                    updates["sex"] = value.upper()
                elif field == "age":
                    updates["age"] = clean_int(value)
                elif field == "height":
                    updates["height_cm"] = clean_int(value)
                elif field == "weight":
                    updates["weight_kg"] = clean_float(value)
                elif field == "activity":
                    v = clean_int(value)
                    if v not in (1,2,3,4,5): raise ValueError
                    updates["activity"] = v
                else:
                    await send(chat_id, "Unknown field.")
                    return PlainTextResponse("ok")

                await upsert_profile(user_id, chat_id, **updates)
                await send(chat_id, "Updated ✅")
            except Exception:
                await send(chat_id, "Could not update. Example:\n/edit weight 74.5")

    elif cmd == "/bmi":
        row = await get_profile(user_id)
        if not row or not row.get("height_cm") or not row.get("weight_kg"):
            await send(chat_id, "Please set height & weight first:\n/setprofile Name, Sex(M/F), Age, Height_cm, Weight_kg, Activity(1-5)")
        else:
            b = bmi_value(row["height_cm"], float(row["weight_kg"]))
            await send(chat_id, f"BMI: {b:.1f} ({bmi_label(b)})")

    elif cmd == "/cutcal":
        row = await get_profile(user_id)
        need = ["sex","age","height_cm","weight_kg","activity"]
        if not row or not all(row.get(k) for k in need):
            await send(chat_id, "Please complete your profile first with /setprofile.")
        else:
            t = tdee(row["sex"], row["age"], row["height_cm"], float(row["weight_kg"]), row["activity"])
            cut = max(t - 500, t - 300)
            msgc = (f"Estimated maintenance (TDEE): {t:.0f} kcal/day\n"
                    f"Suggested to lose weight: ~{cut:.0f} kcal/day (300–500 kcal deficit).")
            await send(chat_id, msgc)
    else:
        await send(chat_id, "Unknown command. Use /start for help.")

    return PlainTextResponse("ok")
