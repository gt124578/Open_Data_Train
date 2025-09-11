# ==============================================================================
# BLOC 1 : INSTALLATION, IMPORTS ET CONFIGURATION
# ==============================================================================

print("🚀 Installation des bibliothèques...")
!pip install gradio geopy folium pandas gradio-folium -q
print("✅ Installation terminée.")

import sqlite3
import pandas as pd
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import folium
import requests
import re
from datetime import datetime, timedelta
import gradio as gr
import gradio_folium as grf # Import gradio_folium

# --- Connexion à la base de données ---
# Assurez-vous que votre Drive est monté et que le chemin est correct
db_path = "/content/drive/MyDrive/Colab Notebook/SNCF/tgvmax.db"
try:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    cur = conn.cursor()
    print("✅ Connexion à la base de données réussie.")
except Exception as e:
    print(f"❌ Erreur de connexion à la base de données : {e}")

# --- Initialisation du géocodeur ---
geolocator = Nominatim(user_agent="mon_appli_itineraire_gradio", timeout=10)

# ==============================================================================
# BLOC 2 : VOS FONCTIONS UTILITAIRES ET PRINCIPALES (INCHANGÉES)
# ==============================================================================
# (Toutes vos fonctions de l'étape précédente sont recopiées ici)

def clean_city_name(city_name):
    if not isinstance(city_name, str):
        return ""
    cleaned_name = re.sub(r'\s*\([^)]*\)$', '', city_name).strip()
    cleaned_name = re.sub(r'\s*(ST JEAN|MATABIAU|VILLE BOURBON|ST CHARLES|PART DIEU|SAINT LAUD|MONTPARNASSE|EST|NORD|LYON|AUSTERLITZ)\s*$', '', cleaned_name, flags=re.IGNORECASE).strip()
    if cleaned_name.lower() == "toulouse":
        return "TOULOUSE"
    return cleaned_name

def estimer_temps_visite(tags):
    if tags.get('tourism') == 'museum' or tags.get('historic') == 'castle':
        return 120
    if tags.get('historic') in ['cathedral', 'church']:
        return 45
    if tags.get('tourism') == 'attraction' or tags.get('historic') == 'monument':
        return 30
    if tags.get('leisure') == 'park':
        return 60
    return 20

def calculer_temps_trajet_a_pied(coords1, coords2, vitesse_kmh=4.5):
    if not coords1 or not coords2:
        return 0
    try:
        distance_km = geodesic(coords1, coords2).kilometers
    except ValueError:
        return 0
    return round((distance_km / vitesse_kmh) * 60)

def trouver_destinations_par_temps(ville_depart, temps_trajet_max_str):
    pattern = f"%{clean_city_name(ville_depart)}%"
    sql = "SELECT Origine, Destination, strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree, TIME(Heure_depart) AS heure_depart, TIME(Heure_arrivee) AS heure_arrivee FROM tgvmax_trajets WHERE LOWER(Origine) LIKE LOWER(?) AND duree > '00:00:00' AND duree <= ? ORDER BY duree"
    cur.execute(sql, (pattern, temps_trajet_max_str))
    return cur.fetchall()

def get_lieux_touristiques(nom_ville):
    nom_ville_nettoye = clean_city_name(nom_ville)
    try:
        location = geolocator.geocode(nom_ville_nettoye, exactly_one=True)
        if not location or 'boundingbox' not in location.raw:
            return []
    except Exception:
        return []
    bbox = location.raw['boundingbox']
    s, n, w, e = [float(x) for x in bbox]
    overpass_url = "http://overpass-api.de/api/interpreter"
    overpass_query = f"""[out:json][timeout:25];(node["tourism"~"museum|attraction|gallery|viewpoint"]({s},{w},{n},{e});way["tourism"~"museum|attraction|gallery|viewpoint"]({s},{w},{n},{e});node["historic"~"castle|monument|ruins|cathedral|church"]({s},{w},{n},{e});way["historic"~"castle|monument|ruins|cathedral|church"]({s},{w},{n},{e}););out center;"""
    try:
        response = requests.get(overpass_url, params={'data': overpass_query})
        data = response.json()
        lieux = []
        for element in data.get('elements', []):
            tags = element.get('tags', {})
            if 'name' not in tags:
                continue
            lat, lon = (element.get('lat'), element.get('lon')) if element['type'] == 'node' else (element.get('center', {}).get('lat'), element.get('center', {}).get('lon'))
            if not lat or not lon:
                continue
            lieux.append({'nom': tags['name'], 'latitude': lat, 'longitude': lon, 'tags': tags, 'temps_visite_min': estimer_temps_visite(tags), 'score_pertinence': 1 if 'wikipedia' in tags else 0})
        return lieux
    except requests.exceptions.RequestException:
        return []

def creer_itineraire_visite_avec_trajet(lieux_tries, temps_disponible_min):
    itineraire, temps_total = [], 0
    if not lieux_tries:
        return [], 0
    premier_lieu = lieux_tries[0]
    if premier_lieu['temps_visite_min'] <= temps_disponible_min:
        itineraire.append(premier_lieu)
        temps_total += premier_lieu['temps_visite_min']
    else:
        return [], 0
    for lieu_candidat in lieux_tries[1:]:
        dernier_lieu = itineraire[-1]
        coords1, coords2 = (dernier_lieu['latitude'], dernier_lieu['longitude']), (lieu_candidat['latitude'], lieu_candidat['longitude'])
        temps_trajet = calculer_temps_trajet_a_pied(coords1, coords2)
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
    pattern_dep, pattern_arr = f"%{clean_city_name(ville_depart)}%", f"%{clean_city_name(ville_arrivee)}%"
    sql = "SELECT Origine, Destination, strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree, TIME(Heure_depart) AS heure_depart, TIME(Heure_arrivee) AS heure_arrivee FROM tgvmax_trajets WHERE LOWER(Origine) LIKE LOWER(?) AND LOWER(Destination) LIKE LOWER(?) AND TIME(Heure_depart) >= ? ORDER BY TIME(Heure_depart) ASC LIMIT 1"
    cur.execute(sql, (pattern_dep, pattern_arr, heure_min_depart_str))
    return cur.fetchone()

def generer_carte_recommandation(ville_depart, destinations, itineraire_choisi, ville_choisie):
    """Génère la carte Folium finale avec toutes les informations."""
    ville_depart_nettoyee = clean_city_name(ville_depart)
    try:
        loc_depart = geolocator.geocode(ville_depart_nettoyee)
        m = folium.Map(location=[loc_depart.latitude, loc_depart.longitude], zoom_start=7)
    except:
        m = folium.Map(location=[46.2276, 2.2137], zoom_start=5)

    if loc_depart:
        folium.Marker(location=[loc_depart.latitude, loc_depart.longitude], popup=f"<b>Départ : {ville_depart}</b>", icon=folium.Icon(color='red', icon='train', prefix='fa')).add_to(m)

    try:
        loc_choisie = geolocator.geocode(clean_city_name(ville_choisie))
        if loc_choisie:
            folium.Circle(location=[loc_choisie.latitude, loc_choisie.longitude], radius=8000, color='red', fill=True, fill_color='red', fill_opacity=0.2).add_to(m)
    except:
        pass

    for dest in destinations:
        if dest[1] != ville_choisie:
            try:
                loc_dest = geolocator.geocode(clean_city_name(dest[1]))
                if loc_dest:
                    folium.Marker(location=[loc_dest.latitude, loc_dest.longitude], popup=f"<i>{dest[1]}</i><br>Durée : {dest[2]}", icon=folium.Icon(color='blue', icon='info-sign')).add_to(m)
            except:
                continue

    for i, lieu in enumerate(itineraire_choisi):
        popup_html = f"<b>{i+1}. {lieu['nom']}</b><br>Visite: {lieu['temps_visite_min']} min"
        if 'trajet_depuis_precedent' in lieu:
            popup_html = f"Trajet: {lieu['trajet_depuis_precedent']} min<br>" + popup_html
        folium.Marker(location=[lieu['latitude'], lieu['longitude']], popup=popup_html, icon=folium.Icon(color='green', icon='camera', prefix='fa')).add_to(m)

    # Return the Folium map object instead of saving it
    return m

# ==============================================================================
# BLOC 3 : LA FONCTION PRINCIPALE POUR GRADIO
# ==============================================================================

def trouver_escapade(ville_depart, heure_depart_souhaitee_str, temps_trajet_max, temps_sur_place_heures, progress=gr.Progress()):
    """
    Cette fonction unique prend toutes les entrées de l'utilisateur et retourne
    les sorties formatées pour l'interface Gradio.
    """
    progress(0, desc="Starting search...")

    # Conversion et préparation des entrées
    temps_sur_place_min = int(temps_sur_place_heures * 60)
    # Use the directly provided HH:MM:SS string
    heure_depart_str = heure_depart_souhaitee_str

    # --- 2. Exécuter votre logique de recherche ---
    progress(0.1, desc="Finding potential destinations...")
    destinations_candidates = trouver_destinations_par_temps(ville_depart, temps_trajet_max)
    destinations_uniques_dict = {dest[1]: dest for dest in reversed(destinations_candidates)}
    destinations_uniques_list = list(destinations_uniques_dict.values())

    meilleure_destination_info, meilleur_itineraire_visite, max_score = None, [], -1

    total_destinations = len(destinations_uniques_list)
    for i, dest_info in enumerate(destinations_uniques_list):
        ville_arrivee = dest_info[1] # Get the destination name
        progress((i + 1) / total_destinations * 0.8 + 0.1, desc=f"Analyzing {ville_arrivee}...") # Progress from 0.1 to 0.9

        lieux = get_lieux_touristiques(ville_arrivee)
        if not lieux:
            continue
        lieux_tries = sorted(lieux, key=lambda x: x['score_pertinence'], reverse=True)
        itineraire_ville, _ = creer_itineraire_visite_avec_trajet(lieux_tries, temps_sur_place_min)
        score_actuel = len(itineraire_ville)
        if score_actuel > max_score:
            max_score, meilleure_destination_info, meilleur_itineraire_visite = score_actuel, dest_info, itineraire_ville

    progress(0.9, desc="Formatting results...")
    # --- 3. Formater les sorties pour Gradio ---
    if not meilleure_destination_info:
        resultat_md = "### Désolé, aucune destination trouvée...\n" \
                      "Aucune destination ne correspond à tous vos critères. Essayez d'augmenter le temps de trajet ou le temps sur place."
        return resultat_md, None

    ville_recommandee = meilleure_destination_info[1]
    train_aller = trouver_train_ideal(ville_depart, ville_recommandee, heure_depart_str)

    if not train_aller:
        resultat_md = f"### Destination trouvée: {ville_recommandee}, mais...\n" \
                      f"Désolé, aucun train aller trouvé depuis {ville_depart} après {heure_depart_str}."
        return resultat_md, None

    # Construction du texte de résultat en Markdown
    resultat_md = f"## 🏆 Votre Escapade Recommandée : **{ville_recommandee}**\n---\n"

    # Itinéraire détaillé
    resultat_md += "### 🚆 Itinéraire Détaillé\n"
    resultat_md += f"**1. Train Aller**\n- Départ de **{train_aller[0]}** à **{train_aller[3]}**\n- Arrivée à **{train_aller[1]}** à **{train_aller[4]}**\n- *Durée : {train_aller[2]}*\n\n"

    resultat_md += "**2. Visite sur Place**\n"
    if meilleur_itineraire_visite:
        heure_arrivee_aller_dt = datetime.strptime(train_aller[4], '%H:%M:%S')
        heure_actuelle_dt = heure_arrivee_aller_dt

        for i, lieu in enumerate(meilleur_itineraire_visite):
            if i > 0:
                temps_trajet_a_pied_min = lieu.get('trajet_depuis_precedent', 0)
                # Ensure addition with timedelta
                heure_arrivee_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_trajet_a_pied_min)
                resultat_md += f"- *🚶 Trajet à pied : ~{temps_trajet_a_pied_min} min (Arrivée estimée : {heure_arrivee_lieu_dt.strftime('%H:%M')})*\n"
                heure_actuelle_dt = heure_arrivee_lieu_dt

            temps_visite_lieu_min = lieu['temps_visite_min']
             # Ensure addition with timedelta
            heure_fin_visite_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_visite_lieu_min)
            resultat_md += f"- 🏛️ Visite de **{lieu['nom']}** ({temps_visite_lieu_min} min). (Fin estimée : {heure_fin_visite_lieu_dt.strftime('%H:%M')})\n"
            heure_actuelle_dt = heure_fin_visite_lieu_dt

        heure_fin_visite_totale_dt = heure_actuelle_dt
    else:
         resultat_md += "     Aucun itinéraire de visite détaillé trouvé pour cette destination dans le temps imparti.\n"
         # If no visit itinerary is found, the end of the visit is just the arrival time + buffer
         heure_arrivee_aller_dt = datetime.strptime(train_aller[4], '%H:%M:%S')
         heure_fin_visite_totale_dt = heure_arrivee_aller_dt + timedelta(minutes=30) # Add a small buffer


    # Calcul du train retour
    heure_min_depart_retour_str = heure_fin_visite_totale_dt.strftime('%H:%M:%S')
    train_retour = trouver_train_ideal(ville_recommandee, ville_depart, heure_min_depart_retour_str)


    resultat_md += "\n**3. Train Retour**\n"
    if train_retour:
        heure_depart_retour_dt = datetime.strptime(train_retour[3], '%H:%M:%S')
        heure_arrivee_retour_dt = datetime.strptime(train_retour[4], '%H:%M:%S')
        temps_trajet_retour_td = heure_arrivee_retour_dt - heure_depart_retour_dt
        if temps_trajet_retour_td.total_seconds() < 0: # Handle overnight journeys
             temps_trajet_retour_td += timedelta(days=1)

        resultat_md += f"- Départ de **{train_retour[0]}** à **{train_retour[3]}** ({heure_depart_retour_dt.strftime('%H:%M')})\n- Arrivée à **{train_retour[1]}** à **{train_retour[4]}** ({heure_arrivee_retour_dt.strftime('%H:%M')})\n- *Durée : {train_retour[2]}*\n"

        # Calcul du temps total
        heure_depart_aller_dt = datetime.strptime(train_aller[3], '%H:%M:%S') # Use departure time of the first train
        temps_total_td = heure_arrivee_retour_dt - heure_depart_aller_dt
        if temps_total_td.total_seconds() < 0: # Handle cases spanning midnight
            temps_total_td += timedelta(days=1)


        heures, remainder = divmod(temps_total_td.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)
        resultat_md += f"\n**Temps total estimé pour l'ensemble du voyage : {int(heures)}h {int(minutes)}min {int(seconds)}s**"


    else:
         resultat_md += f"- *Aucun train retour trouvé depuis {ville_recommandee} vers {ville_depart} après {heure_min_depart_retour_str}.*"


    progress(0.95, desc="Generating map...")
    # Génération de la carte (returns Folium map object)
    carte_finale = generer_carte_recommandation(ville_depart, destinations_candidates, meilleur_itineraire_visite, ville_recommandee)

    progress(1.0, desc="Done!")

    # Return the Markdown result and the Folium map object
    return resultat_md, carte_finale


# ==============================================================================
# BLOC 4 : CRÉATION ET LANCEMENT DE L'INTERFACE GRADIO
# ==============================================================================

with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🚄 Trouvez votre prochaine escapade en train")
    gr.Markdown("Entrez vos critères de voyage pour obtenir une recommandation de destination et un itinéraire complet.")

    with gr.Row():
        with gr.Column(scale=1):
            ville_depart_input = gr.Textbox(label="📍 Ville de départ", value="PARIS (intramuros)")
            # Use gr.Textbox for time input
            heure_depart_input = gr.Textbox(label="🕗 Heure de départ souhaitée (HH:MM:SS)", value="08:00:00", info="Format HH:MM:SS")
            temps_trajet_max_input = gr.Textbox(label="🚆 Temps de trajet maximum", value="02:30:00", info="Format HH:MM:SS")
            temps_sur_place_input = gr.Slider(label="⏳ Temps souhaité sur place (en heures)", minimum=1, maximum=12, step=0.5, value=6)
            btn = gr.Button("Trouver mon escapade !", variant="primary")

        with gr.Column(scale=2):
            resultat_output = gr.Markdown(label="Votre Itinéraire Recommandé")
            # Use gradio_folium.Folium to display the map object
            carte_output = grf.Folium(label="Carte du Voyage")

    btn.click(fn=trouver_escapade,
              inputs=[ville_depart_input, heure_depart_input, temps_trajet_max_input, temps_sur_place_input],
              outputs=[resultat_output, carte_output]) # carte_output is now a Folium component

    gr.Examples(
        examples=[
            ["LYON (gares)", "09:00:00", "01:30:00", 4],
            ["BORDEAUX ST JEAN", "07:30:00", "02:00:00", 8],
            ["MARSEILLE ST CHARLES", "10:00:00", "01:45:00", 5],
            ["LILLE (intramuros)", "09:00:00", "02:00:00", 5]
        ],
        inputs=[ville_depart_input, heure_depart_input, temps_trajet_max_input, temps_sur_place_input],
    )

print("🚀 Lancement de l'interface Gradio...")
# share=True crée un lien public temporaire pour partager votre application
demo.launch(debug=True, share=True)
