import sqlite3
import csv
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

# Connexion à la base (elle sera créée si elle n'existe pas)
conn = sqlite3.connect("tgvmax.db")
cur = conn.cursor()



def SQL(a):
    cur.execute(a)
    return(cur.fetchall())



#print(SQL("SELECT DISTINCT Destination, SUBSTRING(TIMEDIFF(Heure_arrivee, Heure_depart), 12) FROM tgvmax_trajets WHERE Origine LIKE 'COMMERCY'"))

#temps de trajet entre chaque départ et destination
#print(SQL("SELECT DISTINCT Origine,Destination, SUBSTRING(TIMEDIFF(Heure_arrivee, Heure_depart), 12) FROM tgvmax_trajets"))






#ville de départ destination disponible, horaire, temps de trajets
 

ville_dep=input("ville de départ : ")

pattern_1 = f"%{ville_dep}%"


sql = """
SELECT DISTINCT Origine, Destination,
       TIME(Heure_depart) AS heure_depart,
       TIME(Heure_arrivee) AS heure_arrivee,
       SUBSTRING(TIMEDIFF(Heure_arrivee, Heure_depart),12) AS duree
FROM tgvmax_trajets
WHERE LOWER(Origine) LIKE LOWER(?)
"""
cur.execute(sql, (pattern_1,))
resultats = cur.fetchall()
print(resultats)


#ville de départ et ville d'arriver

ville_dest=input("destination : ")
print(ville_dep,ville_dest)

pattern_2 = f"%{ville_dest}%"

sql = """
SELECT DISTINCT Origine, Destination,
       TIME(Heure_depart) AS heure_depart,
       TIME(Heure_arrivee) AS heure_arrivee,
       SUBSTRING(TIMEDIFF(Heure_arrivee, Heure_depart),12) AS duree
FROM tgvmax_trajets
WHERE LOWER(Origine) LIKE LOWER(?) AND LOWER(Destination) LIKE LOWER(?)
"""
cur.execute(sql, (pattern_1,pattern_2))
resultats2 = cur.fetchall()
print(resultats2)

#détermination de la distance entre ville_dep et ville_dest
# Création d'un géocodeur (utilise OpenStreetMap)
geolocator = Nominatim(user_agent="distance_calculator")

# Recherche des coordonnées GPS
loc_dep = geolocator.geocode(ville_dep)
loc_dest = geolocator.geocode(ville_dest)

if loc_dep and loc_dest:
    coord_dep = (loc_dep.latitude, loc_dep.longitude)
    coord_dest = (loc_dest.latitude, loc_dest.longitude)

    # Calcul de la distance à vol d'oiseau
    distance_km = geodesic(coord_dep, coord_dest).kilometers

    print(f"Distance entre {ville_dep} et {ville_dest} : {distance_km:.2f} km")
else:
    print("Impossible de trouver une ou les deux villes.")
