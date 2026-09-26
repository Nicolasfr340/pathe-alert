#!/usr/bin/env python3
"""
Surveillance de la page événement Pathé "Dune - Troisième partie : Projection IMAX 70mm"
au Pathé Odysseum (Montpellier, seule salle IMAX 70mm de France).
Envoie un email uniquement lors de la transition indisponible -> disponible.

Adapté du script original https://github.com/SATHEESHPRASHANTH/pathe-alert
"""

import os
import json
import re
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError  # type: ignore

# --- Constantes ---
FILM_NAME = "Dune - Troisième partie : Projection IMAX 70mm"
FILM_URL = "https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-55289/"
CINEMA_KEYWORD = "Odysseum"  # seul cinéma français équipé d'un projecteur IMAX 70mm
CINEMA_URL = FILM_URL  # on surveille directement la page de l'événement

STATE_FILE = "state.json"

# --- SMTP Brevo ---
SMTP_HOST = "smtp-relay.brevo.com"
SMTP_PORT = 587


def log(message: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {message}", flush=True)


def read_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠️ Impossible de lire {STATE_FILE} ({e}). État par défaut.")
    return {"last_status": "unavailable", "last_seen_at": None}


def write_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"❌ Impossible d'écrire {STATE_FILE}: {e}")


def check_availability() -> tuple[bool, dict]:
    """
    - Ouvre CINEMA_URL (la page événement Dune 3 IMAX 70mm)
    - Accepte les cookies
    - Récupère HTML + texte
    - Détecte le mot-clé du cinéma (Odysseum) + un signal de réservation
      (réserver / e-billet / billetterie / acheter) OU un horaire HH:MM
    """
    import unicodedata

    debug_info = {
        "cinema_found": False,
        "reservation_signal": False,
        "nb_horaires": 0,
        "error": None,
        "used": [],
    }

    def normalize(s: str) -> str:
        s = s.replace("\u00a0", " ").lower()
        s = "".join(
            c for c in unicodedata.normalize("NFD", s)
            if unicodedata.category(c) != "Mn"
        )
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def accept_cookies(page) -> None:
        for _ in range(4):
            for txt in ["Tout accepter", "Accepter", "J'accepte", "OK", "Continuer"]:
                try:
                    page.get_by_role("button", name=re.compile(txt, re.I)).click(timeout=1500)
                    log("🍪 Cookies acceptés/fermés")
                    page.wait_for_timeout(400)
                    return
                except Exception:
                    pass
            page.wait_for_timeout(700)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                locale="fr-FR",
                viewport={"width": 1400, "height": 900},
            )
            page = context.new_page()
            page.set_default_timeout(60000)

            log(f"🎬 Ouverture page événement: {CINEMA_URL}")
            page.goto(CINEMA_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            accept_cookies(page)

            # Scroll pour charger le contenu en lazy-load (liste des cinémas)
            try:
                page.mouse.wheel(0, 3000)
                debug_info["used"].append("scroll")
                page.wait_for_timeout(1500)
                page.mouse.wheel(0, 3000)
            except Exception:
                pass

            page.wait_for_timeout(3000)

            html = page.content()
            debug_info["used"].append("page.content")
            try:
                text = page.inner_text("body")
                debug_info["used"].append("inner_text")
            except Exception:
                text = ""

            browser.close()

        html_n = normalize(html)
        text_n = normalize(text)

        # Le cinéma qui nous intéresse
        cinema_key = normalize(CINEMA_KEYWORD)
        cinema_found = (cinema_key in html_n) or (cinema_key in text_n)
        debug_info["cinema_found"] = cinema_found

        # Signal de réservation
        reservation_keywords = ["reserver", "e-billet", "e billet", "billetterie", "acheter"]
        reservation_signal = any(k in html_n for k in reservation_keywords) or any(
            k in text_n for k in reservation_keywords
        )
        debug_info["reservation_signal"] = reservation_signal

        # Horaires HH:MM
        horaire_pattern = r"\b(?:[01]\d|2[0-3]):[0-5]\d\b"
        times_html = re.findall(horaire_pattern, html_n)
        times_text = re.findall(horaire_pattern, text_n)
        all_times = list(dict.fromkeys(times_html + times_text))
        debug_info["nb_horaires"] = len(all_times)

        available = True # test

        log(
            f"🔎 cinema_found={cinema_found} | reservation_signal={reservation_signal} "
            f"| nb_horaires={debug_info['nb_horaires']} | available={available} | used={debug_info['used']}"
        )
        return available, debug_info

    except PlaywrightTimeoutError as e:
        debug_info["error"] = f"Timeout Playwright: {e}"
        log(f"❌ {debug_info['error']}")
        return False, debug_info
    except Exception as e:
        debug_info["error"] = f"Erreur: {e}"
        log(f"❌ {debug_info['error']}")
        return False, debug_info


def send_email_brevo(subject: str, body: str) -> bool:
    smtp_user = os.environ.get("BREVO_SMTP_USER")
    smtp_pass = os.environ.get("BREVO_SMTP_KEY")
    from_email = os.environ.get("BREVO_FROM_EMAIL")
    to_email = os.environ.get("ALERT_TO_EMAIL")

    if not all([smtp_user, smtp_pass, from_email, to_email]):
        log("❌ Variables SMTP manquantes (vérifie les secrets GitHub)")
        return False

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        log("✉️ Connexion SMTP Brevo…")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_email, [to_email], msg.as_string())
        log(f"✅ Email envoyé à {to_email}")
        return True
    except Exception as e:
        log(f"❌ Erreur envoi email: {e}")
        return False


def main():
    log("===== START =====")
    state = read_state()
    last_status = state.get("last_status", "unavailable")

    available, debug = check_availability()
    new_status = "available" if available else "unavailable"

    if new_status == "available" and last_status != "available":
        subject = f"🎬 Dune 3 IMAX 70mm dispo à Pathé {CINEMA_KEYWORD} !"
        body = (
            f"Film: {FILM_NAME}\n"
            f"Cinéma: Pathé {CINEMA_KEYWORD}\n"
            f"URL: {FILM_URL}\n\n"
            f"Détails:\n"
            f"- cinema_found: {debug.get('cinema_found')}\n"
            f"- reservation_signal: {debug.get('reservation_signal')}\n"
            f"- nb_horaires: {debug.get('nb_horaires')}\n"
            f"- error: {debug.get('error')}\n\n"
            f"Date (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        send_email_brevo(subject, body)
    else:
        log("ℹ️ Pas de transition indispo->dispo")

    state["last_status"] = new_status
    state["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    write_state(state)
    log("===== END =====")


if __name__ == "__main__":
    main()
