#!/usr/bin/env python3
"""
sync_manga.py — modèle TRACKER
================================
Synchronise automatiquement les fiches manga (métadonnées + couverture)
depuis l'API publique MangaDex vers Firestore, pour MangaTrack.

Ce script ne récupère PLUS aucune page de chapitre : le site ne fait
que lister les titres et rediriger vers la source (mangadex.org) pour
la lecture. C'est le modèle "tracker", conforme à l'usage prévu par
l'API MangaDex.

La couverture est téléchargée une fois côté serveur (GitHub Actions)
et stockée en base64 dans Firestore, pour que l'image s'affiche
correctement dans le navigateur (le hotlink direct depuis un site
tiers déclenche un visuel de blocage côté MangaDex).

Coût : 0. API MangaDex gratuite, Firestore Spark (gratuit) largement
suffisant pour ce volume de données (pas de scans, juste des fiches).
"""

import os
import sys
import time
import json
import base64
import requests
import firebase_admin
from firebase_admin import credentials, firestore

MANGADEX_API = "https://api.mangadex.org"
CONTENT_RATINGS = ["safe", "suggestive"]
MAX_MANGA_PER_RUN = 20
REQUEST_DELAY = 0.3
MAX_COVER_BYTES = 900_000  # marge sous la limite Firestore de 1 Mo par champ


def init_firestore():
    raw_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        key_json = json.loads(raw_json)
    else:
        b64_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT_B64")
        if not b64_key:
            print("ERREUR: aucune des deux variables d'environnement "
                  "(FIREBASE_SERVICE_ACCOUNT_JSON ou FIREBASE_SERVICE_ACCOUNT_B64) n'est définie.")
            sys.exit(1)
        key_json = json.loads(base64.b64decode(b64_key))
    cred = credentials.Certificate(key_json)
    firebase_admin.initialize_app(cred)
    return firestore.client()


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MangaTrack-Sync/1.0 (personal tracker project)"})


def api_get(path, params=None):
    resp = SESSION.get(f"{MANGADEX_API}{path}", params=params, timeout=20)
    time.sleep(REQUEST_DELAY)
    resp.raise_for_status()
    return resp.json()


def fetch_updated_manga(limit=MAX_MANGA_PER_RUN):
    params = {
        "limit": limit,
        "order[latestUploadedChapter]": "desc",
        "contentRating[]": CONTENT_RATINGS,
        "includes[]": ["cover_art", "author"],
        # Ne garder QUE les titres ayant au moins un chapitre traduit en anglais
        "availableTranslatedLanguage[]": ["en"],
    }
    return api_get("/manga", params=params).get("data", [])


def extract_title(attrs):
    titles = attrs.get("title", {})
    return titles.get("en") or titles.get("fr") or next(iter(titles.values()), "Sans titre")


def extract_description(attrs):
    desc = attrs.get("description", {})
    return desc.get("en") or desc.get("fr") or next(iter(desc.values()), "")


def fetch_best_cover_filename(manga_id):
    """Interroge toutes les couvertures disponibles pour ce manga et choisit
    en priorité celle marquée comme anglaise ou sans langue spécifique
    (souvent la couverture officielle "propre"), plutôt que la première
    venue qui peut porter un texte traduit dans une autre langue."""
    try:
        data = api_get("/cover", params={
            "manga[]": manga_id,
            "limit": 20,
            "order[volume]": "asc",
        })
        covers = data.get("data", [])
        if not covers:
            return None

        def locale_of(c):
            return c["attributes"].get("locale")

        # priorité : anglais > pas de langue précisée (souvent l'art original) > n'importe laquelle
        english   = [c for c in covers if locale_of(c) == "en"]
        no_locale = [c for c in covers if not locale_of(c)]
        chosen = (english or no_locale or covers)[0]
        return chosen["attributes"].get("fileName")
    except Exception as e:
        print(f"  ! impossible de récupérer la liste des couvertures: {e}")
        return None


def extract_cover_filename_fallback(relationships):
    for rel in relationships:
        if rel.get("type") == "cover_art" and "attributes" in rel:
            return rel["attributes"].get("fileName")
    return None


def extract_author(relationships):
    for rel in relationships:
        if rel.get("type") == "author" and "attributes" in rel:
            return rel["attributes"].get("name", "Inconnu")
    return "Inconnu"


def download_cover_as_data_uri(manga_id, filename):
    """Télécharge la couverture côté serveur et la convertit en data URI
    (évite le blocage anti-hotlink que MangaDex applique aux requêtes
    faites directement depuis le navigateur d'un site tiers)."""
    if not filename:
        return None
    url = f"https://uploads.mangadex.org/covers/{manga_id}/{filename}.512.jpg"
    try:
        resp = SESSION.get(url, timeout=20)
        time.sleep(REQUEST_DELAY)
        resp.raise_for_status()
        if len(resp.content) > MAX_COVER_BYTES:
            return None
        b64 = base64.b64encode(resp.content).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        print(f"  ! impossible de télécharger la couverture: {e}")
        return None


def manga_to_doc(manga):
    attrs = manga["attributes"]
    relationships = manga.get("relationships", [])
    status_map = {"ongoing": "ongoing", "completed": "completed", "hiatus": "hiatus", "cancelled": "hiatus"}
    manga_id = manga["id"]
    cover_filename = fetch_best_cover_filename(manga_id)
    if not cover_filename:
        cover_filename = extract_cover_filename_fallback(relationships)
    cover_data_uri = download_cover_as_data_uri(manga_id, cover_filename)

    return {
        "mangadexId": manga_id,
        "title": extract_title(attrs),
        "author": extract_author(relationships),
        "type": "Manhwa" if attrs.get("originalLanguage") == "ko" else
                "Manhua" if attrs.get("originalLanguage") == "zh" else "Manga",
        "status": status_map.get(attrs.get("status"), "ongoing"),
        "genres": [t["attributes"]["name"].get("en", "") for t in attrs.get("tags", [])
                   if t["attributes"]["group"] == "genre"][:6],
        "cover": cover_data_uri or "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=400&q=80",
        "description": extract_description(attrs)[:800],
        "sourceUrl": f"https://mangadex.org/title/{manga_id}",
    }


def sync():
    db = init_firestore()
    manga_ref = db.collection("manga")

    print("Récupération des mangas récemment mis à jour sur MangaDex...")
    updated_manga = fetch_updated_manga()
    print(f"{len(updated_manga)} mangas trouvés.")

    for manga in updated_manga:
        mangadex_id = manga["id"]
        title = extract_title(manga["attributes"])
        print(f"\n-> {title} ({mangadex_id})")

        existing_query = manga_ref.where("mangadexId", "==", mangadex_id).limit(1).get()
        doc_data = manga_to_doc(manga)

        if existing_query:
            doc_ref = existing_query[0].reference
            # Ne pas écraser les champs modifiés manuellement par l'admin :
            # on ne met à jour que les infos "fraîches" (statut, cover si dispo).
            update_payload = {
                "status": doc_data["status"],
                "sourceUrl": doc_data["sourceUrl"],
            }
            if doc_data["cover"] and "unsplash.com" not in doc_data["cover"]:
                update_payload["cover"] = doc_data["cover"]
            doc_ref.update(update_payload)
            print("   déjà en base, statut/couverture actualisés.")
        else:
            doc_data.update({
                "comments": [],
                "views": 0,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
            manga_ref.document().set(doc_data)
            print("   nouveau titre ajouté.")

    print("\nSynchronisation terminée.")


if __name__ == "__main__":
    sync()
