# ==============================================================================
# BLOC 1 : INSTALLATION, IMPORTS ET CONFIGURATION
# ==============================================================================

# --- Installation des bibliothèques ---
!pip install geopy folium requests pandas -q

# --- Imports ---
import sqlite3
import pandas as pd
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import folium
import requests
import re
from IPython.display import display
from datetime import datetime, timedelta

# --- Connexion à la base de données ---
# !!! ATTENTION : Modifiez ce chemin si nécessaire !!!
db_path = "/content/drive/MyDrive/Colab Notebook/SNCF/tgvmax.db"
try:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    print("✅ Connexion à la base de données réussie.")
except Exception as e:
    print(f"❌ Erreur de connexion à la base de données : {e}")
    print("Veuillez vérifier que le chemin d'accès est correct et que votre Drive est monté.")

# --- Initialisation du géocodeur ---
geolocator = Nominatim(user_agent="mon_appli_itineraire_v2", timeout=10)

# ==============================================================================
# BLOC 2 : FONCTIONS UTILITAIRES (HELPERS)
# ==============================================================================

def clean_city_name(city_name):
    """Nettoie le nom d'une ville en supprimant les mentions comme '(intramuros)' et les gares."""
    if not isinstance(city_name, str):
        return ""
    # Remove "(intramuros)" and any leading/trailing whitespace
    cleaned_name = re.sub(r'\s*\([^)]*\)$', '', city_name).strip()
    # Remove common station names like "MATABIAU", "ST JEAN", "VILLE BOURBON", "ST CHARLES", "PART DIEU", etc.
    cleaned_name = re.sub(r'\s*(ST JEAN|MATABIAU|VILLE BOURBON|ST CHARLES|PART DIEU|SAINT LAUD|MONTPARNASSE|EST|NORD|LYON|AUSTERLITZ)\s*$', '', cleaned_name, flags=re.IGNORECASE).strip()
    # Specifically handle "TOULOUSE MATABIAU"
    if cleaned_name.lower() == "toulouse":
        return "TOULOUSE" # Or "TOULOUSE MATABIAU" depending on how the database is structured
    return cleaned_name

def estimer_temps_visite(tags):
    """Estime le temps de visite en minutes basé sur les tags OSM."""
    if tags.get('tourism') == 'museum' or tags.get('historic') == 'castle':
        return 120  # 2 heures
    if tags.get('historic') in ['cathedral', 'church']:
        return 45   # 45 minutes
    if tags.get('tourism') == 'attraction' or tags.get('historic') == 'monument':
        return 30   # 30 minutes
    if tags.get('leisure') == 'park':
        return 60   # 1 heure
    return 20 # 20 minutes par défaut

def calculer_temps_trajet_a_pied(coords1, coords2, vitesse_kmh=4.5):
    """Calcule le temps de trajet à pied en minutes entre deux points GPS."""
    if not coords1 or not coords2:
        return 0

    try:
        distance_km = geodesic(coords1, coords2).kilometers
    except ValueError:
        # Handle cases where coordinates are invalid
        return 0

    temps_minutes = (distance_km / vitesse_kmh) * 60
    return round(temps_minutes)

# ==============================================================================
# BLOC 3 : FONCTIONS PRINCIPALES (LOGIQUE DU PROJET)
# ==============================================================================

def trouver_destinations_par_temps(ville_depart, temps_trajet_max_str):
    """Trouve les villes accessibles depuis une ville de départ dans un temps de trajet donné."""
    pattern = f"%{clean_city_name(ville_depart)}%"
    sql = """
    SELECT Origine, Destination,
           strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree,
           TIME(Heure_depart) AS heure_depart,
           TIME(Heure_arrivee) AS heure_arrivee
    FROM tgvmax_trajets
    WHERE LOWER(Origine) LIKE LOWER(?) AND duree > '00:00:00' AND duree <= ?
    ORDER BY duree
    """
    cur.execute(sql, (pattern, temps_trajet_max_str))
    return cur.fetchall()

def get_lieux_touristiques(nom_ville):
    """Récupère les lieux touristiques d'une ville via Overpass API."""
    nom_ville_nettoye = clean_city_name(nom_ville)
    try:
        location = geolocator.geocode(nom_ville_nettoye, exactly_one=True)
        if not location or 'boundingbox' not in location.raw:
            print(f"   -> Info: Impossible de trouver les limites pour {nom_ville_nettoye}")
            return []
    except Exception as e:
        print(f"   -> Erreur de géocodage pour {nom_ville_nettoye}: {e}")
        return []

    bbox = location.raw['boundingbox']
    south, north, west, east = [float(x) for x in bbox]
    overpass_url = "http://overpass-api.de/api/interpreter"
    overpass_query = f"""
    [out:json][timeout:25];
    (
      node["tourism"~"museum|attraction|gallery|viewpoint"]({south},{west},{north},{east});
      way["tourism"~"museum|attraction|gallery|viewpoint"]({south},{west},{north},{east});
      node["historic"~"castle|monument|ruins|cathedral|church"]({south},{west},{north},{east});
      way["historic"~"castle|monument|ruins|cathedral|church"]({south},{west},{north},{east});
    );
    out center;
    """
    try:
        response = requests.get(overpass_url, params={'data': overpass_query})
        response.raise_for_status()
        data = response.json()
        lieux = []
        for element in data.get('elements', []):
            tags = element.get('tags', {})
            if 'name' not in tags: continue # On ignore les lieux sans nom

            lat, lon = (element.get('lat'), element.get('lon')) if element['type'] == 'node' else (element.get('center', {}).get('lat'), element.get('center', {}).get('lon'))
            if not lat or not lon: continue

            lieux.append({
                'nom': tags['name'], 'latitude': lat, 'longitude': lon, 'tags': tags,
                'temps_visite_min': estimer_temps_visite(tags),
                'score_pertinence': 1 if 'wikipedia' in tags else 0
            })
        return lieux
    except requests.exceptions.RequestException as e:
        print(f"   -> Erreur API Overpass pour {nom_ville_nettoye}: {e}")
        return []

def creer_itineraire_visite_avec_trajet(lieux_tries, temps_disponible_min):
    """Crée un itinéraire de visite en incluant le temps de trajet à pied."""
    itineraire = []
    temps_total = 0
    if not lieux_tries: return [], 0

    premier_lieu = lieux_tries[0]
    if premier_lieu['temps_visite_min'] <= temps_disponible_min:
        itineraire.append(premier_lieu)
        temps_total += premier_lieu['temps_visite_min']
    else:
        return [], 0

    for lieu_candidat in lieux_tries[1:]:
        dernier_lieu_visite = itineraire[-1]
        coords_dernier_lieu = (dernier_lieu_visite['latitude'], dernier_lieu_visite['longitude'])
        coords_candidat = (lieu_candidat['latitude'], lieu_candidat['longitude'])
        temps_trajet = calculer_temps_trajet_a_pied(coords_dernier_lieu, coords_candidat)

        if temps_total + temps_trajet + lieu_candidat['temps_visite_min'] <= temps_disponible_min:
            lieu_candidat['trajet_depuis_precedent'] = temps_trajet
            itineraire.append(lieu_candidat)
            temps_total += temps_trajet + lieu_candidat['temps_visite_min']

    return itineraire, temps_total

def trouver_train_ideal(ville_depart, ville_arrivee, heure_min_depart_str):
    """
    Trouve le premier train disponible après une heure donnée pour un trajet direct.
    Retourne un tuple (origine, destination, duree, heure_depart, heure_arrivee) ou None.
    """
    pattern_dep = f"%{clean_city_name(ville_depart)}%"
    pattern_arr = f"%{clean_city_name(ville_arrivee)}%"
    sql = """
    SELECT Origine, Destination,
           strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree,
           TIME(Heure_depart) AS heure_depart,
           TIME(Heure_arrivee) AS heure_arrivee
    FROM tgvmax_trajets
    WHERE LOWER(Origine) LIKE LOWER(?) AND LOWER(Destination) LIKE LOWER(?) AND TIME(Heure_depart) >= ?
    ORDER BY TIME(Heure_depart) ASC
    LIMIT 1
    """
    cur.execute(sql, (pattern_dep, pattern_arr, heure_min_depart_str))
    return cur.fetchone()


def generer_carte_recommandation(ville_depart, destinations, itineraire_choisi, ville_choisie, geolocator):
    """Génère la carte Folium finale avec toutes les informations."""
    ville_depart_nettoyee = clean_city_name(ville_depart)
    loc_depart = geolocator.geocode(ville_depart_nettoyee)
    if not loc_depart:
        return "Impossible de géolocaliser la ville de départ."

    m = folium.Map(location=[loc_depart.latitude, loc_depart.longitude], zoom_start=7)

    folium.Marker(
        location=[loc_depart.latitude, loc_depart.longitude],
        popup=f"<b>Départ : {ville_depart}</b>",
        icon=folium.Icon(color='red', icon='train', prefix='fa')
    ).add_to(m)

    # Add a red circle around the recommended destination
    loc_choisie = geolocator.geocode(clean_city_name(ville_choisie))
    if loc_choisie:
        folium.Circle(
            location=[loc_choisie.latitude, loc_choisie.longitude],
            radius=5000,  # Radius in meters (adjust as needed for visibility)
            color='red',
            fill=True,
            fill_color='red',
            fill_opacity=0.2,
            popup=f"Destination recommandée: {ville_choisie}"
        ).add_to(m)


    for dest in destinations:
        ville_dest_nom = dest[1]
        if ville_dest_nom != ville_choisie:
            try:
                loc_dest = geolocator.geocode(clean_city_name(ville_dest_nom))
                if loc_dest:
                    folium.Marker(
                        location=[loc_dest.latitude, loc_dest.longitude],
                        popup=f"<i>{ville_dest_nom}</i><br>Durée : {dest[2]}",
                        icon=folium.Icon(color='blue', icon='info-sign')
                    ).add_to(m)
            except: continue


    # Marqueurs pour l'itinéraire recommandé
    for i, lieu in enumerate(itineraire_choisi):
        popup_html = f"<b>{i+1}. {lieu['nom']}</b><br>Visite: {lieu['temps_visite_min']} min"
        if 'trajet_depuis_precedent' in lieu:
            popup_html = f"Trajet: {lieu['trajet_depuis_precedent']} min<br>" + popup_html
        folium.Marker(
            location=[lieu['latitude'], lieu['longitude']],
            popup=popup_html,
            icon=folium.Icon(color='green', icon='camera', prefix='fa')
        ).add_to(m)

    return m

# ==============================================================================
# BLOC 4 : EXÉCUTION DU TEST AVEC INPUTS UTILISATEUR
# ==============================================================================

# --- 1. Définir les conditions de l'utilisateur (avec inputs) ---
VILLE_DEPART = input("Entrez votre ville de départ : ")
TEMPS_TRAJET_MAX = input("Entrez le temps de trajet maximum souhaité (HH:MM:SS) : ")
temps_sur_place_heures = float(input("Entrez le temps que vous souhaitez passer sur place (en heures) : "))
TEMPS_SUR_PLACE_MIN = int(temps_sur_place_heures * 60) # Convertir en minutes
HEURE_DEPART_SOUHAITEE = input("Entrez l'heure de départ souhaitée (HH:MM) : ")


print("=============================================")
print(f"🚀 Lancement de la recherche d'itinéraire")
print(f"   Ville de départ : {VILLE_DEPART}")
print(f"   Temps de trajet max : {TEMPS_TRAJET_MAX}")
print(f"   Temps sur place : {TEMPS_SUR_PLACE_MIN / 60} heures")
print(f"   Heure de départ souhaitée : {HEURE_DEPART_SOUHAITEE}")
print("=============================================\n")


# --- 2. Trouver toutes les destinations candidates ---
destinations_candidates = trouver_destinations_par_temps(VILLE_DEPART, TEMPS_TRAJET_MAX)
# Create a list of unique destination names, keeping the first occurrence for train info
destinations_uniques_dict = {}
for dest in destinations_candidates:
    dest_name = dest[1]
    if dest_name not in destinations_uniques_dict:
        destinations_uniques_dict[dest_name] = dest

destinations_uniques_list = list(destinations_uniques_dict.values())


print(f"🔎 {len(destinations_uniques_list)} destinations uniques trouvées en moins de {TEMPS_TRAJET_MAX} de trajet.\n")

# --- 3. Évaluer chaque destination ---
meilleure_destination_info = None
meilleur_itineraire_visite = []
max_score = -1

for dest_info in destinations_uniques_list:
    ville_arrivee = dest_info[1] # Get the destination name
    print(f"🏙️  Analyse de : {ville_arrivee}...")
    lieux = get_lieux_touristiques(ville_arrivee)

    if not lieux:
        print("   -> Aucun lieu touristique trouvé.\n")
        continue

    # Trier les lieux par pertinence (score wikipedia)
    lieux_tries = sorted(lieux, key=lambda x: x['score_pertinence'], reverse=True)

    # Créer un itinéraire optimisé
    itineraire_ville, temps_total_visite = creer_itineraire_visite_avec_trajet(lieux_tries, TEMPS_SUR_PLACE_MIN)

    # "Noter" cette destination par le nombre de lieux visitables (critère simple mais efficace)
    score_actuel = len(itineraire_ville)
    print(f"   -> Score : {score_actuel} activités possibles en {temps_total_visite} min.")

    if score_actuel > max_score:
        max_score = score_actuel
        meilleure_destination_info = dest_info # Store the full destination info
        meilleur_itineraire_visite = itineraire_ville # Store the best visit itinerary
    print("-" * 20)

# --- 4. Afficher la recommandation finale et le plan détaillé ---
if meilleure_destination_info:
    ville_recommandee = meilleure_destination_info[1]

    # Trouver le train aller idéal
    train_aller = trouver_train_ideal(VILLE_DEPART, ville_recommandee, HEURE_DEPART_SOUHAITEE)

    if not train_aller:
        print(f"\n❌ Désolé, aucun train aller trouvé depuis {VILLE_DEPART} vers {ville_recommandee} après {HEURE_DEPART_SOUHAITEE}.")
    else:
        print("\n\n=============================================")
        print("🏆 RECOMMANDATION N°1 🏆")
        print(f"La meilleure destination est : {ville_recommandee}")
        print("=============================================\n")
        print("🚆 Itinéraire Détaillé :")

        # Calcul des horaires
        heure_depart_aller_dt = datetime.strptime(train_aller[3], '%H:%M:%S')
        heure_arrivee_aller_dt = datetime.strptime(train_aller[4], '%H:%M:%S')
        temps_trajet_aller_td = heure_arrivee_aller_dt - heure_depart_aller_dt
        if temps_trajet_aller_td.total_seconds() < 0: # Handle overnight journeys
             temps_trajet_aller_td += timedelta(days=1)
        temps_trajet_aller_min = int(temps_trajet_aller_td.total_seconds() / 60)


        print(f"  1. Train Aller : Départ de {train_aller[0]} à {train_aller[3]} ({heure_depart_aller_dt.strftime('%H:%M')}), arrivée à {train_aller[1]} à {train_aller[4]} ({heure_arrivee_aller_dt.strftime('%H:%M')})")
        print(f"     Durée du trajet : {train_aller[2]}")


        # Plan de visite sur place
        heure_debut_visite_dt = heure_arrivee_aller_dt
        heure_actuelle_dt = heure_debut_visite_dt

        print(f"\n  2. Visite de {ville_recommandee} : (Estimation sur place : {TEMPS_SUR_PLACE_MIN} min)")
        if meilleur_itineraire_visite:
            for i, lieu in enumerate(meilleur_itineraire_visite):
                if i > 0:
                    # Temps de trajet à pied depuis le lieu précédent
                    temps_trajet_a_pied_min = lieu.get('trajet_depuis_precedent', 0)
                    heure_arrivee_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_trajet_a_pied_min)
                    print(f"     -> Trajet à pied : ~{temps_trajet_a_pied_min} min (Arrivée estimée : {heure_arrivee_lieu_dt.strftime('%H:%M')})")
                    heure_actuelle_dt = heure_arrivee_lieu_dt

                # Temps de visite du lieu actuel
                temps_visite_lieu_min = lieu['temps_visite_min']
                heure_fin_visite_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_visite_lieu_min)
                print(f"     -> {i+1}. Visite de {lieu['nom']} ({temps_visite_lieu_min} min). (Fin estimée : {heure_fin_visite_lieu_dt.strftime('%H:%M')})")
                heure_actuelle_dt = heure_fin_visite_lieu_dt

            heure_fin_visite_totale_dt = heure_actuelle_dt
        else:
             print("     Aucun itinéraire de visite détaillé trouvé pour cette destination dans le temps imparti.")
             # If no visit itinerary is found, the end of the visit is just the arrival time + buffer
             heure_fin_visite_totale_dt = heure_arrivee_aller_dt + timedelta(minutes=30) # Add a small buffer


        # Trouver le train retour idéal
        heure_min_depart_retour_str = heure_fin_visite_totale_dt.strftime('%H:%M:%S')
        train_retour = trouver_train_ideal(ville_recommandee, VILLE_DEPART, heure_min_depart_retour_str)

        if train_retour:
             heure_depart_retour_dt = datetime.strptime(train_retour[3], '%H:%M:%S')
             heure_arrivee_retour_dt = datetime.strptime(train_retour[4], '%H:%M:%S')
             temps_trajet_retour_td = heure_arrivee_retour_dt - heure_depart_retour_dt
             if temps_trajet_retour_td.total_seconds() < 0: # Handle overnight journeys
                  temps_trajet_retour_td += timedelta(days=1)

             print(f"\n  3. Train Retour : Départ de {train_retour[0]} à {train_retour[3]} ({heure_depart_retour_dt.strftime('%H:%M')}), arrivée à {train_retour[1]} à {train_retour[4]} ({heure_arrivee_retour_dt.strftime('%H:%M')})")
             print(f"     Durée du trajet : {train_retour[2]}")

             # Calcul du temps total
             temps_total_td = heure_arrivee_retour_dt - heure_depart_aller_dt
             if temps_total_td.total_seconds() < 0: # Handle cases spanning midnight
                 temps_total_td += timedelta(days=1)

             heures, remainder = divmod(temps_total_td.total_seconds(), 3600)
             minutes, seconds = divmod(remainder, 60)
             print(f"\nTemps total estimé pour l'ensemble du voyage : {int(heures)}h {int(minutes)}min {int(seconds)}s")


        else:
             print(f"\n  3. Aucun train retour trouvé depuis {ville_recommandee} vers {VILLE_DEPART} après {heure_min_depart_retour_str}.")


        # --- 5. Générer et afficher la carte finale ---
        print("\n🗺️  Génération de la carte...")
        carte_finale = generer_carte_recommandation(VILLE_DEPART, destinations_candidates, meilleur_itineraire_visite, ville_recommandee, geolocator)
        display(carte_finale)

else:
    print("\n❌ Désolé, aucune destination n'a été trouvée correspondant à tous vos critères.")