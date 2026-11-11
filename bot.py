"""Bot Telegram : prévient quand une nouvelle offre de cours apparaît
dans l'espace professeur Complétude.

Fonctionnement : connexion via un navigateur headless (Playwright) -> récupération
du jeton d'API -> appel de l'API interne cap/search -> comparaison avec les offres
déjà vues (state/seen.json) -> notification Telegram pour chaque nouvelle offre.
"""
import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "https://professeur.completude.com"
API = "https://sic.internetude.fr/Api/ProfesseurCompletude"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
DEBUG = os.environ.get("DEBUG") == "1"


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Variable d'environnement manquante : {name}")
    return value


# ---------- état ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": None, "alerted": False}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if state.get("seen"):
        state["seen"] = state["seen"][-2000:]
    STATE_FILE.write_text(json.dumps(state))


# ---------- Telegram ----------
def notify(text):
    if DRY_RUN:
        print("[DRY_RUN] " + text + "\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{env('TELEGRAM_TOKEN', required=True)}/sendMessage",
        json={
            "chat_id": env("TELEGRAM_CHAT_ID", required=True),
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    r.raise_for_status()


def format_offer(d):
    quand = "".join(
        label
        for ok, label in (
            (d.get("AdequatSemaine"), " semaine"),
            (d.get("AdequatWeekend"), " week-end"),
            (d.get("AdequatVacance"), " vacances"),
        )
        if ok
    )
    animaux = ", ".join(a for a, k in (("chien", "Chien"), ("chat", "Chat")) if d.get(k))
    lines = [
        "📚 Nouvelle offre de cours !",
        f"{d.get('LibelleMatiere')} – {d.get('LibelleCombinaison')} "
        f"({'fille' if d.get('EstFille') else 'garçon'})",
        f"📍 {d.get('Ville')} ({d.get('DistanceKm')})",
        f"🕒 {d.get('LibelleFrequence')} – {d.get('LibelleDuree')}",
        f"💶 {d.get('DetailPrix')}",
        f"📅 {d.get('DebutCours')}",
    ]
    if quand:
        lines.append(f"Disponibilités :{quand}")
    if animaux:
        lines.append(f"🐾 Présence de : {animaux}")
    lines.append(f"Ref. {d.get('IdDemande')}")
    lines.append(f"{BASE}/cap")
    return "\n".join(lines)


def keep(d):
    """Filtres optionnels (MATIERES="Mathématiques,Physique", MAX_KM=25)."""
    matieres = [m.strip().lower() for m in env("MATIERES", "").split(",") if m.strip()]
    if matieres and (d.get("LibelleMatiere") or "").lower() not in matieres:
        return False
    max_km = env("MAX_KM")
    if max_km:
        try:
            km = float(str(d.get("DistanceKm")).replace(",", ".").split()[0])
            if km > float(max_km):
                return False
        except (ValueError, IndexError):
            pass
    return True


# ---------- récupération des offres ----------
def first_key(d, pattern):
    for k, v in d.items():
        if re.search(pattern, k, re.I) and isinstance(v, (int, float)):
            return v
    return None


def fetch_offers():
    login = env("COMPLETUDE_LOGIN", required=True)
    password = env("COMPLETUDE_PASSWORD", required=True)
    token = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=env("BROWSER_CHANNEL") or None, headless=True)
        ctx = browser.new_context(locale="fr-FR")
        page = ctx.new_page()

        def on_request(req):
            auth = req.headers.get("authorization")
            if auth and req.url.startswith(API) and "v" not in token:
                token["v"] = auth

        page.on("request", on_request)
        page.goto(BASE, wait_until="networkidle")

        if "v" not in token:  # pas encore connecté : on remplit le formulaire
            pwd = page.locator('input[type="password"]').first
            pwd.wait_for(state="visible", timeout=20000)
            user = page.locator(
                'input[type="email"], input[type="text"], input:not([type])'
            ).first
            user.fill(login)
            pwd.fill(password)
            def wait_token(seconds):
                for _ in range(seconds * 2):
                    if "v" in token:
                        return
                    page.wait_for_timeout(500)

            pwd.press("Enter")
            wait_token(20)  # le site affiche un spinner pendant la connexion
            if "v" not in token:  # Entrée n'a pas suffi : on tente le bouton
                try:
                    page.get_by_role(
                        "button", name=re.compile("connexion|connecter|valider", re.I)
                    ).first.click(timeout=5000)
                except Exception:
                    pass
                wait_token(20)

        if "v" not in token:
            if DEBUG:
                page.screenshot(path="debug_login.png")
            raise RuntimeError("Connexion impossible (jeton non obtenu). Identifiants ou formulaire changé ?")

        headers = {
            "Authorization": token["v"],
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # adresse du professeur
        addr_id, lat, lon = env("ADDRESS_ID"), env("LAT"), env("LON")
        if not (addr_id and lat and lon):
            r = ctx.request.get(f"{API}/intervenant/adresses", headers=headers)
            if not r.ok:
                raise RuntimeError(f"intervenant/adresses -> HTTP {r.status}")
            data = r.json()
            items = data if isinstance(data, list) else next(
                (v for v in data.values() if isinstance(v, list)), [data]
            )
            a = items[0]
            if DEBUG:
                print("Clés adresse :", list(a.keys()))
            addr_id = addr_id or a.get("IdAdresse") or a.get("Id")
            lat = lat or first_key(a, "lat")
            lon = lon or first_key(a, "lon|lng")
        if addr_id is None or lat is None or lon is None:
            raise RuntimeError("Adresse introuvable : renseigne ADDRESS_ID, LAT et LON.")
        lat, lon, half = float(lat), float(lon), float(env("BOX_DEG", "0.7"))

        body = {
            "SessionRecherche": {
                "Id": 0,
                "AvecChat": True,
                "AvecChien": True,
                "AvecVehicule": env("AVEC_VEHICULE") == "1",
                "DispoSemaine": True,
                "DispoVacance": True,
                "DispoWeekEnd": True,
                "IdAdresse": addr_id,
            },
            "MapBox": {
                "IdSessionRecherche": 0,
                "NordOuestLatitude": lat + half,
                "NordOuestLongitude": lon - half,
                "SudEstLatitude": lat - half,
                "SudEstLongitude": lon + half,
            },
        }
        last = None
        for _ in range(3):
            try:
                r = ctx.request.post(
                    f"{API}/cap/search", headers=headers, data=json.dumps(body), timeout=60000
                )
                if r.ok:
                    break
                last = f"HTTP {r.status}"
            except Exception as e:
                last = str(e).splitlines()[0][:150]
            page.wait_for_timeout(5000)
        else:
            raise RuntimeError(f"cap/search : {last}")
        offers = r.json().get("Demandes", [])
        browser.close()
        return offers


# ---------- main ----------
def main():
    state = load_state()
    try:
        offers = fetch_offers()
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        reason = re.sub(r"Bearer\s+\S+", "Bearer ***", msg)[:200]
        print("Erreur :", reason)  # pas de traceback complète : elle peut contenir le jeton
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] >= 3 and not state.get("alerted"):
            notify(f"⚠️ Le bot Complétude est en erreur depuis plusieurs vérifications : {reason}")
            state["alerted"] = True
        save_state(state)
        sys.exit(1)

    ids = [o["IdDemande"] for o in offers]
    print(f"{len(offers)} offre(s) récupérée(s)")

    if state.get("seen") is None:  # premier lancement : on mémorise sans spammer
        notify(f"✅ Bot Complétude actif. {len(offers)} offre(s) actuellement visibles ; "
               "je te préviens dès qu'une nouvelle arrive.")
        state["seen"] = ids
    else:
        known = set(state["seen"])
        for o in offers:
            if o["IdDemande"] not in known:
                if keep(o):
                    notify(format_offer(o))
                state["seen"].append(o["IdDemande"])
    state["alerted"] = False
    state["fails"] = 0
    save_state(state)


if __name__ == "__main__":
    main()
