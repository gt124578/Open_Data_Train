# ==============================================================================
# BLOC 1 : INSTALLATION, IMPORTS ET CONFIGURATION
# ==============================================================================
print("🚀 Installation des bibliothèques...")
!pip install gradio geopy folium pandas -q
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
def clean_city_name(city_name):
    if not isinstance(city_name, str): return ""
    cleaned_name = re.sub(r'\s*\([^)]*\)$', '', city_name).strip()
    cleaned_name = re.sub(r'\s*(ST JEAN|MATABIAU|VILLE BOURBON|ST CHARLES|PART DIEU|SAINT LAUD|MONTPARNASSE|EST|NORD|LYON|AUSTERLITZ)\s*$', '', cleaned_name, flags=re.IGNORECASE).strip()
    if cleaned_name.lower() == "toulouse": return "TOULOUSE"
    return cleaned_name

def estimer_temps_visite(tags):
    if tags.get('tourism') == 'museum' or tags.get('historic') == 'castle': return 120
    if tags.get('historic') in ['cathedral', 'church']: return 45
    if tags.get('tourism') == 'attraction' or tags.get('historic') == 'monument': return 30
    if tags.get('leisure') == 'park': return 60
    return 20

def calculer_temps_trajet_a_pied(coords1, coords2, vitesse_kmh=4.5):
    if not coords1 or not coords2: return 0
    try:
        distance_km = geodesic(coords1, coords2).kilometers
    except ValueError: return 0
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
        if not location or 'boundingbox' not in location.raw: return []
    except Exception: return []
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
            if 'name' not in tags: continue
            lat, lon = (element.get('lat'), element.get('lon')) if element['type'] == 'node' else (element.get('center', {}).get('lat'), element.get('center', {}).get('lon'))
            if not lat or not lon: continue
            lieux.append({'nom': tags['name'], 'latitude': lat, 'longitude': lon, 'tags': tags, 'temps_visite_min': estimer_temps_visite(tags), 'score_pertinence': 1 if 'wikipedia' in tags else 0})
        return lieux
    except requests.exceptions.RequestException: return []

def creer_itineraire_visite_avec_trajet(lieux_tries, temps_disponible_min):
    itineraire, temps_total = [], 0
    if not lieux_tries: return [], 0
    premier_lieu = lieux_tries[0]
    if premier_lieu['temps_visite_min'] <= temps_disponible_min:
        itineraire.append(premier_lieu)
        temps_total += premier_lieu['temps_visite_min']
    else: return [], 0
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
    pattern_dep, pattern_arr = f"%{clean_city_name(ville_depart)}%", f"%{clean_city_name(ville_arrivee)}%"
    sql = "SELECT Origine, Destination, strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree, TIME(Heure_depart) AS heure_depart, TIME(Heure_arrivee) AS heure_arrivee FROM tgvmax_trajets WHERE LOWER(Origine) LIKE LOWER(?) AND LOWER(Destination) LIKE LOWER(?) AND TIME(Heure_depart) >= ? ORDER BY TIME(Heure_depart) ASC LIMIT 1"
    cur.execute(sql, (pattern_dep, pattern_arr, heure_min_depart_str))
    return cur.fetchone()

def generer_carte_recommandation(ville_depart, destinations, itineraire_choisi, ville_choisie):
    try:
        loc_depart = geolocator.geocode(clean_city_name(ville_depart))
        m = folium.Map(location=[loc_depart.latitude, loc_depart.longitude], zoom_start=7)
    except:
        m = folium.Map(location=[46.2276, 2.2137], zoom_start=5)
    if loc_depart: folium.Marker(location=[loc_depart.latitude, loc_depart.longitude], popup=f"<b>Départ : {ville_depart}</b>", icon=folium.Icon(color='red', icon='train', prefix='fa')).add_to(m)
    try:
        loc_choisie = geolocator.geocode(clean_city_name(ville_choisie))
        if loc_choisie: folium.Circle(location=[loc_choisie.latitude, loc_choisie.longitude], radius=8000, color='red', fill=True, fill_color='red', fill_opacity=0.2).add_to(m)
    except: pass
    for dest in destinations:
        if dest[1] != ville_choisie:
            try:
                loc_dest = geolocator.geocode(clean_city_name(dest[1]))
                if loc_dest: folium.Marker(location=[loc_dest.latitude, loc_dest.longitude], popup=f"<i>{dest[1]}</i><br>Durée : {dest[2]}", icon=folium.Icon(color='blue', icon='info-sign')).add_to(m)
            except: continue
    for i, lieu in enumerate(itineraire_choisi):
        popup_html = f"<b>{i+1}. {lieu['nom']}</b><br>Visite: {lieu['temps_visite_min']} min"
        if 'trajet_depuis_precedent' in lieu: popup_html = f"Trajet: {lieu['trajet_depuis_precedent']} min<br>" + popup_html
        folium.Marker(location=[lieu['latitude'], lieu['longitude']], popup=popup_html, icon=folium.Icon(color='green', icon='camera', prefix='fa')).add_to(m)
    return m

# ==============================================================================
# BLOC 3 : LA FONCTION PRINCIPALE POUR GRADIO
# ==============================================================================

def trouver_escapade(ville_depart, heure_depart_souhaitee_str, temps_trajet_max, temps_sur_place_heures, progress=gr.Progress(track_tqdm=True)):
    """
    Cette fonction unique prend toutes les entrées de l'utilisateur et retourne
    les sorties formatées pour l'interface Gradio.
    """
    progress(0, desc="Début de la recherche...")
    temps_sur_place_min = int(temps_sur_place_heures * 60)
    
    try:
        datetime.strptime(heure_depart_souhaitee_str, "%H:%M")
    except (ValueError, TypeError):
        return "### Erreur : Format d'heure invalide. Veuillez utiliser HH:MM.", None

    progress(0.1, desc="Recherche des destinations possibles...")
    destinations_candidates = trouver_destinations_par_temps(ville_depart, temps_trajet_max)
    destinations_uniques_dict = {dest[1]: dest for dest in reversed(destinations_candidates)}
    destinations_uniques_list = list(destinations_uniques_dict.values())

    meilleure_destination_info, meilleur_itineraire_visite, max_score = None, [], -1

    for dest_info in gr.tqdm(destinations_uniques_list, desc="Analyse des destinations"):
        lieux = get_lieux_touristiques(dest_info[1])
        if not lieux: continue
        lieux_tries = sorted(lieux, key=lambda x: x['score_pertinence'], reverse=True)
        itineraire_ville, _ = creer_itineraire_visite_avec_trajet(lieux_tries, temps_sur_place_min)
        score_actuel = len(itineraire_ville)
        if score_actuel > max_score:
            max_score, meilleure_destination_info, meilleur_itineraire_visite = score_actuel, dest_info, itineraire_ville

    progress(0.9, desc="Formatage des résultats...")
    if not meilleure_destination_info:
        return "### Désolé, aucune destination trouvée...", None

    ville_recommandee = meilleure_destination_info[1]
    train_aller = trouver_train_ideal(ville_depart, ville_recommandee, heure_depart_souhaitee_str)

    if not train_aller:
        return f"### Destination trouvée : {ville_recommandee}, mais...", None

    resultat_md = f"## 🏆 Votre Escapade Recommandée : **{ville_recommandee}**\n---\n"
    resultat_md += "### 🚆 Itinéraire Détaillé\n"
    resultat_md += f"**1. Train Aller**\n- Départ de **{train_aller[0]}** à **{train_aller[3]}**\n- Arrivée à **{train_aller[1]}** à **{train_aller[4]}**\n- *Durée : {train_aller[2]}*\n\n"
    resultat_md += "**2. Visite sur Place**\n"
    
    heure_arrivee_aller_dt = datetime.strptime(train_aller[4], '%H:%M:%S')
    heure_actuelle_dt = heure_arrivee_aller_dt
    for i, lieu in enumerate(meilleur_itineraire_visite):
        if i > 0:
            temps_trajet_a_pied_min = lieu.get('trajet_depuis_precedent', 0)
            heure_arrivee_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_trajet_a_pied_min)
            resultat_md += f"- *🚶 Trajet : ~{temps_trajet_a_pied_min} min (Arrivée estimée : {heure_arrivee_lieu_dt.strftime('%H:%M')})*\n"
            heure_actuelle_dt = heure_arrivee_lieu_dt
        temps_visite_lieu_min = lieu['temps_visite_min']
        heure_fin_visite_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_visite_lieu_min)
        resultat_md += f"- 🏛️ Visite de **{lieu['nom']}** ({temps_visite_lieu_min} min). (Fin : {heure_fin_visite_lieu_dt.strftime('%H:%M')})\n"
        heure_actuelle_dt = heure_fin_visite_lieu_dt
    
    heure_fin_visite_totale_dt = heure_actuelle_dt
    heure_min_depart_retour_str = heure_fin_visite_totale_dt.strftime('%H:%M:%S')
    train_retour = trouver_train_ideal(ville_recommandee, ville_depart, heure_min_depart_retour_str)

    resultat_md += "\n**3. Train Retour**\n"
    if train_retour:
        resultat_md += f"- Départ de **{train_retour[0]}** à **{train_retour[3]}**\n- Arrivée à **{train_retour[1]}** à **{train_retour[4]}**\n- *Durée : {train_retour[2]}*\n"
    else:
        resultat_md += f"- *Aucun train retour trouvé après {heure_min_depart_retour_str}.*"

    progress(0.95, desc="Génération de la carte...")
    carte_finale = generer_carte_recommandation(ville_depart, destinations_candidates, meilleur_itineraire_visite, ville_recommandee)
    
    # === LA CORRECTION EST ICI ===
    # On convertit la carte en une chaîne HTML au lieu de la sauvegarder dans un fichier
    map_html_content = carte_finale._repr_html_()
    
    progress(1.0, desc="Terminé !")

    # On retourne le contenu HTML directement
    return resultat_md, map_html_content

# ==============================================================================
# BLOC 4 : CRÉATION ET LANCEMENT DE L'INTERFACE GRADIO
# ==============================================================================

with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🚄 Trouvez votre prochaine escapade en train")
    gr.Markdown("Entrez vos critères de voyage pour obtenir une recommandation de destination et un itinéraire complet.")

    with gr.Row():
        with gr.Column(scale=1):
            ville_depart_input = gr.Textbox(label="📍 Ville de départ", value="PARIS (intramuros)")
            heure_depart_input = gr.Textbox(label="🕗 Heure de départ souhaitée (HH:MM)", value="09:00", info="Format HH:MM")
            temps_trajet_max_input = gr.Textbox(label="🚆 Temps de trajet maximum", value="02:30:00", info="Format HH:MM:SS")
            temps_sur_place_input = gr.Slider(label="⏳ Temps souhaité sur place (en heures)", minimum=1, maximum=12, step=0.5, value=6)
            btn = gr.Button("Trouver mon escapade !", variant="primary")

        with gr.Column(scale=2):
            resultat_output = gr.Markdown(label="Votre Itinéraire Recommandé")
            carte_output = gr.HTML(label="Carte du Voyage")

    btn.click(fn=trouver_escapade,
              inputs=[ville_depart_input, heure_depart_input, temps_trajet_max_input, temps_sur_place_input],
              outputs=[resultat_output, carte_output])

    gr.Examples(
        examples=[
            ["LYON (gares)", "09:00", "01:30:00", 4],
            ["BORDEAUX ST JEAN", "07:30", "02:00:00", 8],
            ["MARSEILLE ST CHARLES", "10:00", "01:45:00", 5],
        ],
        inputs=[ville_depart_input, heure_depart_input, temps_trajet_max_input, temps_sur_place_input],
    )

print("🚀 Lancement de l'interface Gradio...")
demo.launch(debug=True, share=True)