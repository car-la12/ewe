# ÉWÉ · Plateforme de suivi des espaces verts

## Lancer en local
    pip install -r requirements.txt
    SUPERVISOR_TOKEN=mon-secret uvicorn app:app --reload
    # application agent et plateforme : http://localhost:8000
    # documentation interactive (ajout d'arbres, agents, sources) : http://localhost:8000/docs
    python test_app.py      # test de bout en bout

## Déployer
    docker build -t ewe .
    docker run -p 8000:8000 -e SUPERVISOR_TOKEN=mon-secret -v ewe-data:/data ewe

Variables d'environnement :
- SUPERVISOR_TOKEN : jeton des surveillants (obligatoire en production)
- SMS_WEBHOOK_URL : adresse de la passerelle SMS de votre opérateur (reçoit {"to", "message"}). Sans elle, les SMS sont seulement journalisés.
- AI_SIMULATE : 1 pour un diagnostic simulé (démo), 0 quand le vrai modèle est branché dans analyse()
- ANALYSIS_TIMES : créneaux d'analyse journalière des sources, par défaut 06:00,12:00,18:00
- DATA_DIR : dossier de la base SQLite et des photos (à sauvegarder)

## Avant la mise en production
- Servir le site en HTTPS : le téléphone ne donne sa position et n'ouvre la caméra qu'en HTTPS.
- Remplacer le jeton unique par une vraie connexion pour les surveillants, et identifier les agents.
- Brancher le modèle IA réel dans analyse() (app.py).
- Brancher la passerelle SMS retenue par l'ANAP.
