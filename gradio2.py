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
from datetime import datetime, timedelta, date # <-- MODIFIÉ : Ajout de 'date'
import gradio as gr
import gradio_folium as grf

# --- Connexion à la base de données ---
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
# BLOC 2 : VOS FONCTIONS UTILITAIRES ET PRINCIPALES (MODIFIÉES)
# ==============================================================================

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

# <-- MODIFIÉ : La fonction accepte maintenant une date de départ
def trouver_destinations_par_temps(ville_depart, date_depart_str, temps_trajet_max_str):
    """
    Trouve les destinations possibles pour une date et une durée de trajet données.
    NOTE : La colonne contenant la date/heure dans la BDD doit s'appeler 'Heure_depart'.
    La fonction DATE() de SQLite extrait la date de ce champ.
    """
    pattern = f"%{clean_city_name(ville_depart)}%"
    # La requête filtre maintenant aussi sur la date de départ
    sql = """
        SELECT
            Origine,
            Destination,
            strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree,
            TIME(Heure_depart) AS heure_depart,
            TIME(Heure_arrivee) AS heure_arrivee
        FROM tgvmax_trajets
        WHERE
            LOWER(Origine) LIKE LOWER(?)
            AND DATE(Heure_depart) = ?
            AND duree > '00:00:00'
            AND duree <= ?
        ORDER BY duree
    """
    cur.execute(sql, (pattern, date_depart_str, temps_trajet_max_str))
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

# <-- MODIFIÉ : La fonction accepte maintenant une date
def trouver_train_ideal(ville_depart, ville_arrivee, date_str, heure_min_depart_str):
    """
    Trouve le premier train disponible après une heure et une date données.
    """
    pattern_dep, pattern_arr = f"%{clean_city_name(ville_depart)}%", f"%{clean_city_name(ville_arrivee)}%"
    sql = """
        SELECT
            Origine, Destination,
            strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree,
            TIME(Heure_depart) AS heure_depart,
            TIME(Heure_arrivee) AS heure_arrivee
        FROM tgvmax_trajets
        WHERE
            LOWER(Origine) LIKE LOWER(?)
            AND LOWER(Destination) LIKE LOWER(?)
            AND DATE(Heure_depart) = ?
            AND TIME(Heure_depart) >= ?
        ORDER BY TIME(Heure_depart) ASC
        LIMIT 1
    """
    cur.execute(sql, (pattern_dep, pattern_arr, date_str, heure_min_depart_str))
    return cur.fetchone()

def generer_carte_recommandation(ville_depart, destinations, itineraire_choisi, ville_choisie):
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

    return m

# ==============================================================================
# BLOC 3 : LA FONCTION PRINCIPALE POUR GRADIO (MODIFIÉE)
# ==============================================================================

# <-- MODIFIÉ : Ajout de 'date_depart_souhaitee' comme premier argument
def trouver_escapade(ville_depart, date_depart_souhaitee, heure_depart_souhaitee_str, temps_trajet_max, temps_sur_place_heures, progress=gr.Progress()):
    progress(0, desc="Démarrage de la recherche...")

    # --- 1. Conversion et préparation des entrées ---
    temps_sur_place_min = int(temps_sur_place_heures * 60)
    # Gradio passe un objet datetime.datetime, on le convertit en string 'YYYY-MM-DD'
    date_depart_str = date_depart_souhaitee.strftime('%Y-%m-%d')
    heure_depart_str = heure_depart_souhaitee_str

    # --- 2. Recherche des destinations ---
    progress(0.1, desc="Recherche des destinations potentielles...")
    # <-- MODIFIÉ : On passe la date à la fonction de recherche
    destinations_candidates = trouver_destinations_par_temps(ville_depart, date_depart_str, temps_trajet_max)
    if not destinations_candidates:
        resultat_md = f"### Aucune destination trouvée\n" \
                      f"Aucun train ne correspond à vos critères pour la date du {date_depart_str}. Essayez une autre date."
        return resultat_md, None

    destinations_uniques_dict = {dest[1]: dest for dest in reversed(destinations_candidates)}
    destinations_uniques_list = list(destinations_uniques_dict.values())

    meilleure_destination_info, meilleur_itineraire_visite, max_score = None, [], -1

    total_destinations = len(destinations_uniques_list)
    for i, dest_info in enumerate(destinations_uniques_list):
        ville_arrivee = dest_info[1]
        progress_val = (i + 1) / total_destinations * 0.8 + 0.1
        progress(progress_val, desc=f"Analyse de {ville_arrivee}...")

        lieux = get_lieux_touristiques(ville_arrivee)
        if not lieux:
            continue
        lieux_tries = sorted(lieux, key=lambda x: x['score_pertinence'], reverse=True)
        itineraire_ville, _ = creer_itineraire_visite_avec_trajet(lieux_tries, temps_sur_place_min)
        score_actuel = len(itineraire_ville)
        if score_actuel > max_score:
            max_score, meilleure_destination_info, meilleur_itineraire_visite = score_actuel, dest_info, itineraire_ville

    progress(0.9, desc="Formatage des résultats...")
    # --- 3. Formatage des sorties ---
    if not meilleure_destination_info:
        resultat_md = "### Désolé, aucune destination trouvée...\n" \
                      "Aucune destination ne permet d'organiser une visite avec vos critères. Essayez d'augmenter le temps de trajet ou le temps sur place."
        return resultat_md, None

    ville_recommandee = meilleure_destination_info[1]
    # <-- MODIFIÉ : On passe la date pour trouver le train aller
    train_aller = trouver_train_ideal(ville_depart, ville_recommandee, date_depart_str, heure_depart_str)

    if not train_aller:
        resultat_md = f"### Destination trouvée : {ville_recommandee}, mais...\n" \
                      f"Désolé, aucun train aller trouvé depuis {ville_depart} après {heure_depart_str} le {date_depart_str}."
        return resultat_md, None

    # Construction du texte de résultat
    resultat_md = f"## 🏆 Votre Escapade Recommandée : **{ville_recommandee}**\n---\n"
    resultat_md += f"### 📅 Pour la journée du {datetime.strptime(date_depart_str, '%Y-%m-%d').strftime('%A %d %B %Y')}\n"
    resultat_md += "### 🚆 Itinéraire Détaillé\n"
    resultat_md += f"**1. Train Aller**\n- Départ de **{train_aller[0]}** à **{train_aller[3]}**\n- Arrivée à **{train_aller[1]}** à **{train_aller[4]}**\n- *Durée : {train_aller[2]}*\n\n"

    resultat_md += "**2. Visite sur Place**\n"
    # Combinez la date de départ et l'heure d'arrivée pour avoir un objet datetime complet
    heure_arrivee_aller_dt = datetime.strptime(f"{date_depart_str} {train_aller[4]}", '%Y-%m-%d %H:%M:%S')
    heure_actuelle_dt = heure_arrivee_aller_dt

    if meilleur_itineraire_visite:
        for i, lieu in enumerate(meilleur_itineraire_visite):
            if i > 0:
                temps_trajet_a_pied_min = lieu.get('trajet_depuis_precedent', 0)
                heure_arrivee_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_trajet_a_pied_min)
                resultat_md += f"- *🚶 Trajet à pied : ~{temps_trajet_a_pied_min} min (Arrivée estimée : {heure_arrivee_lieu_dt.strftime('%H:%M')})*\n"
                heure_actuelle_dt = heure_arrivee_lieu_dt

            temps_visite_lieu_min = lieu['temps_visite_min']
            heure_fin_visite_lieu_dt = heure_actuelle_dt + timedelta(minutes=temps_visite_lieu_min)
            resultat_md += f"- 🏛️ Visite de **{lieu['nom']}** ({temps_visite_lieu_min} min). (Fin estimée : {heure_fin_visite_lieu_dt.strftime('%H:%M')})\n"
            heure_actuelle_dt = heure_fin_visite_lieu_dt
        heure_fin_visite_totale_dt = heure_actuelle_dt
    else:
        resultat_md += "     Aucun itinéraire de visite détaillé trouvé.\n"
        heure_fin_visite_totale_dt = heure_actuelle_dt + timedelta(minutes=30)

    # Calcul du train retour
    # Le retour peut être le même jour ou le lendemain si les visites se terminent tard
    date_retour_str = heure_fin_visite_totale_dt.strftime('%Y-%m-%d')
    heure_min_depart_retour_str = heure_fin_visite_totale_dt.strftime('%H:%M:%S')
    # <-- MODIFIÉ : On passe la date de retour calculée
    train_retour = trouver_train_ideal(ville_recommandee, ville_depart, date_retour_str, heure_min_depart_retour_str)

    resultat_md += "\n**3. Train Retour**\n"
    if train_retour:
        # Combinez la date de retour et les heures pour des calculs précis
        heure_depart_retour_dt = datetime.strptime(f"{date_retour_str} {train_retour[3]}", '%Y-%m-%d %H:%M:%S')
        heure_arrivee_retour_dt = datetime.strptime(f"{date_retour_str} {train_retour[4]}", '%Y-%m-%d %H:%M:%S')

        # Gérer le cas où le train arrive le lendemain
        if heure_arrivee_retour_dt < heure_depart_retour_dt:
            heure_arrivee_retour_dt += timedelta(days=1)

        resultat_md += f"- Départ de **{train_retour[0]}** à **{train_retour[3]}**\n- Arrivée à **{train_retour[1]}** à **{train_retour[4]}**\n- *Durée : {train_retour[2]}*\n"

        # Calcul du temps total
        heure_depart_aller_dt = datetime.strptime(f"{date_depart_str} {train_aller[3]}", '%Y-%m-%d %H:%M:%S')
        temps_total_td = heure_arrivee_retour_dt - heure_depart_aller_dt
        heures, remainder = divmod(temps_total_td.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        resultat_md += f"\n**Temps total estimé pour l'escapade : {int(heures)}h {int(minutes)}min**"
    else:
        resultat_md += f"- *Aucun train retour trouvé depuis {ville_recommandee} vers {ville_depart} après {heure_min_depart_retour_str} le {date_retour_str}.*"

    progress(0.95, desc="Génération de la carte...")
    carte_finale = generer_carte_recommandation(ville_depart, destinations_candidates, meilleur_itineraire_visite, ville_recommandee)
    progress(1.0, desc="Terminé !")

    return resultat_md, carte_finale


# ==============================================================================
# BLOC 4 : CRÉATION ET LANCEMENT DE L'INTERFACE GRADIO (MODIFIÉE)
# ==============================================================================

with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🚄 Trouvez votre prochaine escapade en train")
    gr.Markdown("Entrez vos critères de voyage pour obtenir une recommandation de destination et un itinéraire complet.")

    with gr.Row():
        with gr.Column(scale=1):
            ville_depart_input = gr.Textbox(label="📍 Ville de départ", value="PARIS (intramuros)")
            # <-- MODIFIÉ : Ajout du sélecteur de date
            date_depart_input = gr.DatePicker(label="📅 Date de départ", value=date.today().strftime("%Y-%m-%d"))
            heure_depart_input = gr.Textbox(label="🕗 Heure de départ souhaitée (HH:MM:SS)", value="08:00:00")
            temps_trajet_max_input = gr.Textbox(label="🚆 Temps de trajet maximum (HH:MM:SS)", value="02:30:00")
            temps_sur_place_input = gr.Slider(label="⏳ Temps souhaité sur place (en heures)", minimum=1, maximum=12, step=0.5, value=6)
            btn = gr.Button("Trouver mon escapade !", variant="primary")

        with gr.Column(scale=2):
            resultat_output = gr.Markdown(label="Votre Itinéraire Recommandé")
            carte_output = grf.Folium(label="Carte du Voyage")

    # <-- MODIFIÉ : Ajout de date_depart_input dans la liste des entrées
    btn.click(fn=trouver_escapade,
              inputs=[ville_depart_input, date_depart_input, heure_depart_input, temps_trajet_max_input, temps_sur_place_input],
              outputs=[resultat_output, carte_output])

    # <-- MODIFIÉ : Ajout d'une date d'exemple (note: Gradio ne peut pas pré-remplir le DatePicker dans les exemples)
    # La date sera ignorée lors du clic sur l'exemple, mais la valeur par défaut du composant sera utilisée.
    gr.Examples(
        examples=[
            ["LYON (gares)", None, "09:00:00", "01:30:00", 4],
            ["BORDEAUX ST JEAN", None, "07:30:00", "02:00:00", 8],
            ["MARSEILLE ST CHARLES", None, "10:00:00", "01:45:00", 5],
            ["LILLE (intramuros)", None, "09:00:00", "02:00:00", 5]
        ],
        inputs=[ville_depart_input, date_depart_input, heure_depart_input, temps_trajet_max_input, temps_sur_place_input],
    )

print("🚀 Lancement de l'interface Gradio...")
demo.launch(debug=True, share=True)