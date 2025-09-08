import sqlite3
import csv


# Connexion à la base (elle sera créée si elle n'existe pas)
conn = sqlite3.connect("tgvmax.db")
cur = conn.cursor()

"""with open("tgvmax.csv", newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f, delimiter=';')
    print("Colonnes détectées :", reader.fieldnames)"""


# Suppression de l'ancienne table si elle existe 
cur.execute("DROP TABLE IF EXISTS tgvmax_trajets")

# Création de la nouvelle table avec la colonne Date
cur.execute("""
CREATE TABLE tgvmax_trajets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    Date TEXT,
    TRAIN_NO TEXT,
    Origine TEXT,
    Destination TEXT,
    Heure_depart TEXT,
    Heure_arrivee TEXT
)
""")
conn.commit()

def importer_csv(fichier):
    with open(fichier, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')
        for row in reader:
            cur.execute("""
            INSERT INTO tgvmax_trajets (
                "DATE", TRAIN_NO, Origine, Destination, Heure_depart, Heure_arrivee
            ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                row['\ufeffDATE'],
                row['TRAIN_NO'],
                row['Origine'],
                row['Destination'],
                row['Heure_depart'],
                row['Heure_arrivee']
            ))
    conn.commit()

#Nom de la base de données : tgvmax.csv (doit être dans le même fichier que le code)
importer_csv(r"tgvmax.csv")




def SQL(a):
    cur.execute(a)
    return(cur.fetchall())



print(SQL("SELECT DISTINCT DATE FROM tgvmax_trajets WHERE Origine LIKE 'COMMERCY'"))



#--------------------------------------------------------------------------------------------------


