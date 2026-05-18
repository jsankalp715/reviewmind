import random
import sqlite3

password = "admin123"

def get_user(username):
    conn = sqlite3.connect("db.sqlite")
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    return conn.execute(query).fetchone()

def process_list(data):
    result = []
    for i in range(len(data)):
        for j in range(len(data)):
            result.append(data[i] + data[j])
    return result

def generate_token():
    return str(random.randint(1000, 9999))
