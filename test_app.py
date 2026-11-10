import os, sys, tempfile, hashlib
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["SUPERVISOR_TOKEN"] = "test"
os.environ["SCHEDULER"] = "0"
sys.path.insert(0, os.path.dirname(__file__))
from fastapi.testclient import TestClient
import app as ewe

c = TestClient(ewe.app)
H = {"X-Token": "test"}

def sick_photo():
    # octets dont le diagnostic simulé est une maladie ou un parasite
    for i in range(1000):
        b = f"feuille-{i}".encode()
        if int(hashlib.sha256(b).hexdigest(), 16) % 3 == 1:
            return b

def test_flow():
    assert c.get("/api/health").json() == {"ok": True}
    assert c.post("/api/trees", json={"id": "A-014", "species": "Neem", "zone": "Tronçon Asphaltage 4",
                                      "lat": 6.3703, "lon": 2.4306}, headers=H).status_code == 200
    assert c.post("/api/agents", json={"id": "AG1", "name": "Agent 1", "phone": "+22960000000"},
                  headers=H).status_code == 200
    # 1. l'agent envoie une photo : GPS, météo et saison remplis automatiquement par l'app
    r = c.post("/api/observations", data={"agent_id": "AG1", "lat": 6.37031, "lon": 2.43061,
                                          "temp": 34, "humidity": 85, "rain": 0},
               files={"photo": ("f.jpg", sick_photo(), "image/jpeg")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["species"] == "Neem" and j["diagnostic"] in ("fongique", "parasite")
    # 2. une notification est créée pour les surveillants
    notes = c.get("/api/notifications", headers=H).json()
    assert len(notes) == 1 and notes[0]["read"] == 0
    # 3. le surveillant envoie le SMS à l'agent
    s = c.post(f"/api/alerts/{notes[0]['alert_id']}/sms", json={"agent_id": "AG1"}, headers=H)
    assert s.status_code == 200 and s.json()["gateway"] == "simulé"
    assert c.get("/api/notifications", headers=H).json()[0]["read"] == 1
    # 4. le tableau de bord reflète l'observation
    d = c.get("/api/dashboard", headers=H).json()
    assert d["total"] == 1 and d["touched_pct"] == 100
    assert any(row["species"] == "Neem" for row in d["matrix"])
    # 5. source automatique (caméra, drone)
    assert c.post("/api/sources", json={"id": "CAM1", "kind": "camera", "lat": 6.37, "lon": 2.43},
                  headers=H).status_code == 200
    r2 = c.post("/api/sources/ingest", data={"source_id": "CAM1"},
                files={"photo": ("c.jpg", b"camera-frame-1", "image/jpeg")}, headers=H)
    assert r2.status_code == 200 and r2.json()["observation_id"] == 2
    # 6. accès refusé sans jeton
    assert c.get("/api/dashboard").status_code == 401
    assert c.get("/api/alerts", headers={"X-Token": "faux"}).status_code == 401
    # 6b. jeton avec espaces (copié-collé sur téléphone) accepté
    assert c.get("/api/dashboard", headers={"X-Token": "  test \n"}).status_code == 200
    # 7. la page front est servie
    assert "Surveillance ANAP" in c.get("/").text and "Prendre la photo" in c.get("/").text
    print("tests OK")

if __name__ == "__main__":
    test_flow()
