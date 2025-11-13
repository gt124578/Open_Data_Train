

# ==============================================================================
# BLOC 1 : INSTALLATION, IMPORTS ET CONFIGURATION
# ==============================================================================

import sqlite3
import pandas as pd
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import folium
import requests
import re
from datetime import datetime, timedelta
import gradio as gr
import gradio_folium as grf

# --- Connexion à la base de données ---
db_path = r"/content/drive/MyDrive/Colab Notebook/SNCF/tgvmax.db"
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
    # Remove "(intramuros)" and any leading/trailing whitespace
    cleaned_name = re.sub(r'\s*\([^)]*\)$', '', city_name).strip()
    # Be less aggressive in removing station names, specifically keep "ST CHARLES"
    cleaned_name = re.sub(r'\s*(MATABIAU|VILLE BOURBON|PART DIEU|SAINT LAUD|MONTPARNASSE|EST|NORD|LYON|AUSTERLITZ)\s*$', '', cleaned_name, flags=re.IGNORECASE).strip()
    # Specifically handle "TOULOUSE MATABIAU" and "MARSEILLE ST CHARLES"
    if cleaned_name.lower() == "toulouse":
        return "TOULOUSE MATABIAU" # Use full station name if that's how it is in the database
    if cleaned_name.lower() == "marseille":
         return "MARSEILLE ST CHARLES" # Use full station name
    # Remove "ST JEAN" only if it's at the end and not part of a larger name like "ST JEAN DE LUZ"
    cleaned_name = re.sub(r'\s*ST JEAN$', '', cleaned_name, flags=re.IGNORECASE).strip()

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
    """
    Trouve les destinations possibles pour une durée de trajet donnée.
    NOTE : This function still relies on time duration only, not specific dates from the database.
    If you need to filter destinations based on trains available *after* a specific date,
    this function would need significant modification to include date filtering in the SQL query.
    """
    pattern = f"%{clean_city_name(ville_depart)}%"
    # The query still filters only on time duration
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
            AND duree > '00:00:00'
            AND duree <= ?
        ORDER BY duree
    """
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
        response.raise_for_status()
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

def trouver_train_ideal(ville_depart, ville_arrivee, date_min_depart, heure_min_depart_str):
    """
    Trouve le premier train disponible after a given date and time for a direct journey.
    Args:
        ville_depart (str): The departure city.
        ville_arrivee (str): The arrival city.
        date_min_depart (datetime.date): The minimum departure date.
        heure_min_depart_str (str): The minimum departure time in HH:MM:SS format.

    Returns:
        tuple: (origine, destination, duree, heure_depart, heure_arrivee) or None.
    """
    pattern_dep = f"%{clean_city_name(ville_depart)}%"
    pattern_arr = f"%{clean_city_name(ville_arrivee)}%"

    # Format the date for SQL query
    date_str = date_min_depart.strftime('%Y-%m-%d')


    sql = """
    SELECT Origine, Destination,
           strftime('%H:%M:%S', (julianday(Heure_arrivee) - julianday(Heure_depart)) * 86400, 'unixepoch') AS duree,
           TIME(Heure_depart) AS heure_depart,
           TIME(Heure_arrivee) AS heure_arrivee
    FROM tgvmax_trajets
    WHERE LOWER(Origine) LIKE LOWER(?)
      AND LOWER(Destination) LIKE LOWER(?)
      AND (
           (DATE(Heure_depart) > ?) -- Departure date is after the minimum date
           OR
           (DATE(Heure_depart) = ? AND TIME(Heure_depart) >= ?) -- Or departure date is the same and time is after or equal
          )
    ORDER BY Heure_depart ASC -- Order by the full datetime
    LIMIT 1
    """

    print(f"DEBUG: SQL Query for trouver_train_ideal: {sql}")
    print(f"DEBUG: Parameters: ('{pattern_dep}', '{pattern_arr}', '{date_str}', '{date_str}', '{heure_min_depart_str}')")

    try:
        cur.execute(sql, (pattern_dep, pattern_arr, date_str, date_str, heure_min_depart_str))
        result = cur.fetchone()
        print(f"DEBUG: Query Result: {result}")
        return result
    except sqlite3.Error as e:
        print(f"Database error in trouver_train_ideal: {e}")
        return None
    except Exception as e:
        print(f"An error occurred in trouver_train_ideal: {e}")
        return None


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

def trouver_escapade(ville_depart, date_heure_depart_souhaitee_dt, temps_trajet_max, temps_sur_place_heures, progress=gr.Progress()):
    """
    Cette fonction unique prend toutes les entrées de l'utilisateur et retourne
    les sorties formatées pour l'interface Gradio.
    """
    progress(0, desc="Starting search...")

    # Add a check for None and provide a default value if necessary
    if date_heure_depart_souhaitee_dt is None:
        return "### Erreur: Veuillez sélectionner une date et une heure de départ.", None

    # Conversion et préparation des entrées
    temps_sur_place_min = int(temps_sur_place_heures * 60)

    progress(0.1, desc="Finding potential destinations...")
    # Pass only the time constraint to trouver_destinations_par_temps as it doesn't use the full datetime
    destinations_candidates = trouver_destinations_par_temps(ville_depart, temps_trajet_max)

    destinations_uniques_dict = {dest[1]: dest for dest in reversed(destinations_candidates)}
    destinations_uniques_list = list(destinations_uniques_dict.values())

    # Filter destinations to keep only those with a valid round trip
    valid_destinations = []
    for dest_info in destinations_uniques_list:
        ville_arrivee = dest_info[1]
        # Check for a valid "aller" train
        train_aller = trouver_train_ideal(ville_depart, ville_arrivee, date_heure_depart_souhaitee_dt.date(), date_heure_depart_souhaitee_dt.strftime('%H:%M:%S'))

        if train_aller:
            # Calculate estimated end of visit time
            heure_arrivee_aller_dt = datetime.combine(date_heure_depart_souhaitee_dt.date(), datetime.strptime(train_aller[4], '%H:%M:%S').time())
            if train_aller[4] < train_aller[3]: # Handle overnight arrival
                heure_arrivee_aller_dt += timedelta(days=1)
            heure_fin_visite_totale_dt = heure_arrivee_aller_dt + timedelta(minutes=temps_sur_place_min)

            # Check for a valid "retour" train on the same day
            train_retour = trouver_train_ideal(ville_arrivee, ville_depart, heure_fin_visite_totale_dt.date(), heure_fin_visite_totale_dt.strftime('%H:%M:%S'))

            # If no train is found on the same day, check for the first train the next day
            if not train_retour:
                 jour_suivant_dt = heure_fin_visite_totale_dt + timedelta(days=1)
                 train_retour = trouver_train_ideal(ville_arrivee, ville_depart, jour_suivant_dt.date(), jour_suivant_dt.replace(hour=0, minute=0, second=0).strftime('%H:%M:%S'))


            if train_retour:
                # If both aller and retour trains are found, add the destination to valid_destinations
                valid_destinations.append((dest_info, train_aller, train_retour))
            else:
                print(f"DEBUG: No return train found for {ville_arrivee}")
        else:
            print(f"DEBUG: No outbound train found for {ville_arrivee}")


    meilleure_destination_info, meilleur_itineraire_visite, max_score = None, [], -1
    best_train_aller, best_train_retour = None, None

    total_valid_destinations = len(valid_destinations)
    for i, (dest_info, train_aller, train_retour) in enumerate(valid_destinations):
        ville_arrivee = dest_info[1]
        progress((i + 1) / total_valid_destinations * 0.8 + 0.1, desc=f"Analyzing {ville_arrivee}...")

        lieux = get_lieux_touristiques(ville_arrivee)
        lieux_tries = sorted(lieux, key=lambda x: x['score_pertinence'], reverse=True)
        itineraire_ville, _ = creer_itineraire_visite_avec_trajet(lieux_tries, temps_sur_place_min)
        score_actuel = len(itineraire_ville)

        if score_actuel > max_score:
            max_score = score_actuel
            meilleure_destination_info = dest_info
            meilleur_itineraire_visite = itineraire_ville
            best_train_aller = train_aller
            best_train_retour = train_retour


    progress(0.9, desc="Formatting results...")

    if not meilleure_destination_info:
        resultat_md = "### Désolé, aucune destination trouvée...\n" \
                      "Aucune destination ne correspond à tous vos critères (trajet aller-retour possible et temps sur place suffisant pour au moins 1 activité)."
        return resultat_md, None

    ville_recommandee = meilleure_destination_info[1]
    train_aller = best_train_aller
    train_retour = best_train_retour


    resultat_md = f"## 🏆 Votre Escapade Recommandée : **{ville_recommandee}**\n---\n"


    resultat_md += "### 🚆 Itinéraire Détaillé\n"
    # Display the full date and time for clarity
    resultat_md += f"**1. Train Aller**\n- Départ de **{train_aller[0]}** le {date_heure_depart_souhaitee_dt.strftime('%Y-%m-%d')} à **{train_aller[3]}**\n- Arrivée à **{train_aller[1]}** à **{train_aller[4]}** ({date_heure_depart_souhaitee_dt.strftime('%Y-%m-%d') if train_aller[4] >= train_aller[3] else (date_heure_depart_souhaitee_dt + timedelta(days=1)).strftime('%Y-%m-%d')})\n- *Durée : {train_aller[2]}*\n\n"

    resultat_md += "**2. Visite sur Place**\n"
    if meilleur_itineraire_visite:
        # Calculate the actual arrival datetime for the first train
        # Assuming the date of arrival is the same as the departure date for simplicity within the travel time limit
        heure_arrivee_aller_dt = datetime.combine(date_heure_depart_souhaitee_dt.date(), datetime.strptime(train_aller[4], '%H:%M:%S').time())
        # Adjust for overnight arrival if necessary
        if train_aller[4] < train_aller[3]:
             heure_arrivee_aller_dt += timedelta(days=1)


        heure_actuelle_dt = heure_arrivee_aller_dt

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
         resultat_md += "     Aucun itinéraire de visite détaillé trouvé pour cette destination dans le temps imparti.\n"
         # If no visit itinerary is found, the end of the visit is just the arrival time + buffer
         heure_arrivee_aller_dt = datetime.combine(date_heure_depart_souhaitee_dt.date(), datetime.strptime(train_aller[4], '%H:%M:%S').time())
         # Adjust for overnight arrival if necessary
         if train_aller[4] < train_aller[3]:
             heure_arrivee_aller_dt += timedelta(days=1)
         heure_fin_visite_totale_dt = heure_arrivee_aller_dt + timedelta(minutes=30) # Add a small buffer



    # Trouver le train retour idéal
    # The return train should be on the same date or the next day depending on the end of visit time
    heure_min_depart_retour_dt = heure_fin_visite_totale_dt
    # Check for a valid "retour" train on the same day
    train_retour = trouver_train_ideal(ville_recommandee, ville_depart, heure_min_depart_retour_dt.date(), heure_min_depart_retour_dt.strftime('%H:%M:%S'))

    # If no train is found on the same day after the visit, check for the first train the next day
    if not train_retour:
         jour_suivant_dt = heure_fin_visite_totale_dt + timedelta(days=1)
         train_retour = trouver_train_ideal(ville_recommandee, ville_depart, jour_suivant_dt.date(), jour_suivant_dt.replace(hour=0, minute=0, second=0).strftime('%H:%M:%S'))
         if train_retour:
              resultat_md += f"\n*Note: Aucun train retour trouvé le jour même. Recherche du premier train le lendemain.*"


    resultat_md += "\n**3. Train Retour**\n"
    if train_retour:
        # Calculate the actual departure datetime for the return train
        heure_depart_retour_dt = datetime.combine(heure_fin_visite_totale_dt.date(), datetime.strptime(train_retour[3], '%H:%M:%S').time())
        # Adjust for next day departure if the returned time is earlier than the end of visit time
        if heure_depart_retour_dt < heure_fin_visite_totale_dt:
             heure_depart_retour_dt += timedelta(days=1)

        # Calculate the actual arrival datetime for the return train
        heure_arrivee_retour_dt = datetime.combine(heure_depart_retour_dt.date(), datetime.strptime(train_retour[4], '%H:%M:%S').time())
        # Adjust for overnight arrival
        if heure_arrivee_retour_dt < heure_depart_retour_dt:
             heure_arrivee_retour_dt += timedelta(days=1)


        resultat_md += f"- Départ de **{train_retour[0]}** le {heure_depart_retour_dt.strftime('%Y-%m-%d')} à **{train_retour[3]}** ({heure_depart_retour_dt.strftime('%H:%M')})\n- Arrivée à **{train_retour[1]}** à **{train_retour[4]}** ({heure_arrivee_retour_dt.strftime('%Y-%m-%d %H:%M')})\n- *Durée : {train_retour[2]}*\n"

        # Calcul du temps total
        heure_depart_aller_dt = datetime.combine(date_heure_depart_souhaitee_dt.date(), datetime.strptime(train_aller[3], '%H:%M:%S').time())
        # Adjust for overnight departure of the first train if necessary
        if train_aller[3] < date_heure_depart_souhaitee_dt.strftime('%H:%M:%S'):
             heure_depart_aller_dt += timedelta(days=1)


        temps_total_td = heure_arrivee_retour_dt - heure_depart_aller_dt
        # Handle cases spanning midnight over multiple days
        # If the total duration is negative, it means the end is on the next day or later
        if temps_total_td.total_seconds() < 0:
            temps_total_td += timedelta(days=((heure_arrivee_retour_dt.date() - heure_depart_aller_dt.date()).days))


        heures, remainder = divmod(temps_total_td.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)
        resultat_md += f"\n**Temps total estimé pour l'ensemble du voyage : {int(heures)}h {int(minutes)}min {int(seconds)}s**"


    else:
         resultat_md += f"- *Aucun train retour trouvé depuis {ville_recommandee} vers {ville_depart} après {heure_fin_visite_totale_dt.strftime('%Y-%m-%d %H:%M:%S')}."


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
            # Use gr.DateTime for date and time input
            heure_depart_input = gr.DateTime(label="🕗 Date et heure de départ souhaitées", value=datetime(2024, 9, 8, 8, 0, 0), type="pydatetime")
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

    # Update examples to use datetime objects
    gr.Examples(
        examples=[
            ["LYON (gares)", datetime(2024, 9, 9, 9, 0, 0), "01:30:00", 4],
            ["BORDEAUX ST JEAN", datetime(2024, 9, 10, 7, 30, 0), "02:00:00", 8],
            ["MARSEILLE ST CHARLES", datetime(2024, 9, 11, 10, 0, 0), "01:45:00", 5],
            ["MARMANDE", datetime(2024, 9, 12, 9, 0, 0), "02:00:00", 5]
        ],
        inputs=[ville_depart_input, heure_depart_input, temps_trajet_max_input, temps_sur_place_input],
    )

print("🚀 Lancement de l'interface Gradio...")
# share=True crée un lien public temporaire pour partager votre application
demo.launch(debug=True, share=True)
