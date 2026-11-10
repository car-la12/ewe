import os, math, sqlite3, hashlib, threading, time, json, urllib.request, datetime as dt
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", BASE / "data"))
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
DB = DATA / "ewe.db"
TOKEN = os.getenv("SUPERVISOR_TOKEN", "change-me").strip()
SMS_WEBHOOK = os.getenv("SMS_WEBHOOK_URL", "")          # passerelle SMS (facultatif)
SIMULATE = os.getenv("AI_SIMULATE", "1") == "1"         # 1 = diagnostic simulé pour démo
SCHEDULE = [s.strip() for s in os.getenv("ANALYSIS_TIMES", "06:00,12:00,18:00").split(",") if s.strip()]
SEASONS = ["Grande pluie", "Petite sèche", "Petite pluie", "Grande sèche"]
SICK = ("fongique", "parasite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trees(id TEXT PRIMARY KEY, species TEXT, zone TEXT, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, name TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, kind TEXT, lat REAL, lon REAL, snapshot_url TEXT);
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, source TEXT, season INTEGER,
  tree_id TEXT, species TEXT, zone TEXT, lat REAL, lon REAL, temp REAL, humidity REAL, rain REAL,
  photo TEXT, diagnostic TEXT, confidence REAL, score INTEGER, level TEXT);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id INTEGER, status TEXT,
  agent_id TEXT, sms_sent_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id INTEGER, message TEXT,
  created_at TEXT, read INTEGER DEFAULT 0);
"""
sqlite3.connect(DB).executescript(SCHEMA)

def run(sql, args=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        con.commit()
        if sql.lstrip().upper().startswith("SELECT"):
            return [dict(r) for r in cur.fetchall()]
        return cur.lastrowid
    finally:
        con.close()

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def season_index(d):
    m = d.month
    return 0 if 4 <= m <= 7 else 1 if m in (8, 9) else 2 if m in (10, 11) else 3

def haversine(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))

def analyse(photo):
    # Point d'entrée du modèle IA : remplacer ce bloc par l'appel au vrai modèle.
    if not SIMULATE:
        return "non analysé", 0.0
    names = ["sain", "fongique", "parasite"]
    k = int(hashlib.sha256(photo).hexdigest(), 16) % 3
    return names[k], (0.91 if k == 0 else 0.87)

def risk(diag, conf, humidity, temp):
    sick = diag in SICK
    tf = min(100, max(0, ((temp if temp is not None else 25) - 20) * 4))
    hum = humidity if humidity is not None else 50
    score = round((conf * 100 * 0.5 if sick else 0) + 0.3 * hum + 0.2 * tf)
    level = "faible" if score < 35 else "moyen" if score < 60 else "élevé"
    return int(score), level

def send_sms(to, text):
    if not SMS_WEBHOOK:
        print(f"[SMS simulé] à {to} : {text}")
        return "simulé"
    req = urllib.request.Request(SMS_WEBHOOK, data=json.dumps({"to": to, "message": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return f"HTTP {r.status}"

def ingest(photo, source, lat=None, lon=None, tree_id=None, temp=None, humidity=None, rain=None):
    name = hashlib.sha1(photo).hexdigest()[:20] + ".jpg"
    (UPLOADS / name).write_bytes(photo)
    tree = None
    if tree_id:
        found = run("SELECT * FROM trees WHERE id=?", (tree_id,))
        tree = found[0] if found else None
    elif lat is not None and lon is not None:
        best, best_d = None, 50.0
        for t in run("SELECT * FROM trees"):
            d = haversine(lat, lon, t["lat"], t["lon"])
            if d < best_d:
                best, best_d = t, d
        tree = best
    diag, conf = analyse(photo)
    score, level = risk(diag, conf, humidity, temp)
    oid = run("INSERT INTO observations(created_at,source,season,tree_id,species,zone,lat,lon,temp,humidity,rain,"
              "photo,diagnostic,confidence,score,level) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (now(), source, season_index(dt.date.today()), tree["id"] if tree else None,
               tree["species"] if tree else "inconnue", tree["zone"] if tree else "hors référentiel",
               lat, lon, temp, humidity, rain, name, diag, conf, score, level))
    if diag in SICK or level == "élevé":
        aid = run("INSERT INTO alerts(observation_id,status,created_at) VALUES(?,?,?)", (oid, "à vérifier", now()))
        where = f"{tree['id']} · {tree['zone']}" if tree else "position GPS hors référentiel"
        run("INSERT INTO notifications(alert_id,message,created_at) VALUES(?,?,?)",
            (aid, f"{level.capitalize()} : {diag} — {where}", now()))
    return {"observation_id": oid, "diagnostic": diag, "score": score, "level": level,
            "species": tree["species"] if tree else None}

def pull_sources():
    for s in run("SELECT * FROM sources WHERE snapshot_url IS NOT NULL AND snapshot_url<>''"):
        try:
            with urllib.request.urlopen(s["snapshot_url"], timeout=20) as r:
                ingest(r.read(), f"auto:{s['id']}", s["lat"], s["lon"])
        except Exception as e:
            print("capture échouée", s["id"], e)

def scheduler():
    day = dt.date.today().isoformat()
    t = dt.datetime.now().strftime("%H:%M")
    done = {(day, s) for s in SCHEDULE if t >= s}   # créneaux déjà passés : ignorés au démarrage
    while True:
        t = dt.datetime.now().strftime("%H:%M")
        day = dt.date.today().isoformat()
        for slot in SCHEDULE:
            if t >= slot and (day, slot) not in done:
                done.add((day, slot))
                pull_sources()
        time.sleep(30)

if os.getenv("SCHEDULER", "1") == "1":
    threading.Thread(target=scheduler, daemon=True).start()

def check(tok):
    if (tok or "").strip() != TOKEN:
        raise HTTPException(status_code=401, detail="Jeton superviseur invalide")

class Tree(BaseModel):
    id: str; species: str; zone: str; lat: float; lon: float

class Agent(BaseModel):
    id: str; name: str; phone: str

class Source(BaseModel):
    id: str; kind: str; lat: float; lon: float; snapshot_url: Optional[str] = None

class SmsIn(BaseModel):
    agent_id: str
    message: Optional[str] = None

app = FastAPI(title="ÉWÉ · Plateforme de suivi des espaces verts")

@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")

@app.get("/api/health")
def health():
    return {"ok": True}

# ---- Application agent : photo + GPS + météo + saison envoyés à la plateforme ----
@app.post("/api/observations")
async def observation(photo: UploadFile = File(...), agent_id: str = Form(""),
                      lat: Optional[float] = Form(None), lon: Optional[float] = Form(None),
                      temp: Optional[float] = Form(None), humidity: Optional[float] = Form(None),
                      rain: Optional[float] = Form(None), tree_id: Optional[str] = Form(None)):
    data = await photo.read()
    if not data:
        raise HTTPException(400, "Photo vide")
    return ingest(data, f"agent:{agent_id}", lat, lon, tree_id, temp, humidity, rain)

# ---- Caméras, drones, capteurs : envoi direct par la source ----
@app.post("/api/sources/ingest")
async def source_ingest(photo: UploadFile = File(...), source_id: str = Form(...),
                        x_token: str = Header(default="")):
    check(x_token)
    src = run("SELECT * FROM sources WHERE id=?", (source_id,))
    if not src:
        raise HTTPException(404, "Source inconnue")
    return ingest(await photo.read(), f"source:{source_id}", src[0]["lat"], src[0]["lon"])

# ---- Référentiel (superviseur) ----
@app.post("/api/trees")
def add_tree(t: Tree, x_token: str = Header(default="")):
    check(x_token)
    run("INSERT OR REPLACE INTO trees VALUES(?,?,?,?,?)", (t.id, t.species, t.zone, t.lat, t.lon))
    return {"ok": True}

@app.post("/api/agents")
def add_agent(a: Agent, x_token: str = Header(default="")):
    check(x_token)
    run("INSERT OR REPLACE INTO agents VALUES(?,?,?)", (a.id, a.name, a.phone))
    return {"ok": True}

@app.post("/api/sources")
def add_source(s: Source, x_token: str = Header(default="")):
    check(x_token)
    run("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)", (s.id, s.kind, s.lat, s.lon, s.snapshot_url))
    return {"ok": True}

@app.get("/api/agents")
def list_agents(x_token: str = Header(default="")):
    check(x_token)
    return run("SELECT id, name, phone FROM agents ORDER BY name")

# ---- Surveillance : notifications, alertes, SMS, tableau de bord ----
@app.get("/api/notifications")
def notifications(x_token: str = Header(default="")):
    check(x_token)
    return run("SELECT n.id, n.message, n.created_at, n.read, a.id AS alert_id, a.status "
               "FROM notifications n JOIN alerts a ON a.id=n.alert_id ORDER BY n.id DESC LIMIT 50")

@app.get("/api/alerts")
def alerts(x_token: str = Header(default="")):
    check(x_token)
    return run("SELECT a.id, a.status, a.agent_id, a.sms_sent_at, o.diagnostic, o.score, o.level, "
               "o.species, o.zone, o.created_at FROM alerts a JOIN observations o ON o.id=a.observation_id "
               "ORDER BY a.id DESC LIMIT 50")

@app.post("/api/alerts/{alert_id}/sms")
def send_alert_sms(alert_id: int, body: SmsIn, x_token: str = Header(default="")):
    check(x_token)
    rows = run("SELECT a.id, o.diagnostic, o.species, o.zone, o.level FROM alerts a "
               "JOIN observations o ON o.id=a.observation_id WHERE a.id=?", (alert_id,))
    if not rows:
        raise HTTPException(404, "Alerte introuvable")
    ag = run("SELECT * FROM agents WHERE id=?", (body.agent_id,))
    if not ag:
        raise HTTPException(404, "Agent introuvable")
    r = rows[0]
    text = body.message or (f"ÉWÉ : alerte {r['level']} sur {r['species']} ({r['zone']}), "
                            f"{r['diagnostic']}. Traiter sous 48 h. Répondre 1 après traitement.")
    gateway = send_sms(ag[0]["phone"], text)
    run("UPDATE alerts SET status=?, agent_id=?, sms_sent_at=? WHERE id=?",
        ("sms envoyé", body.agent_id, now(), alert_id))
    run("UPDATE notifications SET read=1 WHERE alert_id=?", (alert_id,))
    return {"ok": True, "gateway": gateway}

def pct(p, n):
    return round(100 * (p or 0) / n) if n else None

@app.get("/api/dashboard")
def dashboard(x_token: str = Header(default="")):
    check(x_token)
    touched = "SUM(diagnostic IN ('fongique','parasite'))"
    rows = run(f"SELECT species, season, COUNT(*) n, {touched} p FROM observations GROUP BY species, season")
    species = sorted({r["species"] for r in rows})
    matrix = []
    for sp in species:
        rates = [None] * 4
        for r in rows:
            if r["species"] == sp:
                rates[r["season"]] = pct(r["p"], r["n"])
        matrix.append({"species": sp, "rates": rates})
    zones = run(f"SELECT zone, COUNT(*) n, {touched} p FROM observations GROUP BY zone ORDER BY zone")
    total = run(f"SELECT COUNT(*) n, {touched} p FROM observations")[0]
    return {"seasons": SEASONS, "matrix": matrix,
            "zones": [{"zone": z["zone"], "n": z["n"], "rate": pct(z["p"], z["n"])} for z in zones],
            "total": total["n"], "touched_pct": pct(total["p"], total["n"])}

@app.get("/api/observations")
def observations(x_token: str = Header(default="")):
    check(x_token)
    return run("SELECT id, created_at, source, season, species, zone, temp, humidity, rain, diagnostic, "
               "score, level FROM observations ORDER BY id DESC LIMIT 50")
