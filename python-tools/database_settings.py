from mysql.connector import connection


import os

def connect_audit_database():
    return connection.MySQLConnection(
        host="audit.internal",
        user="audit_reader",
        password=os.environ["AUDIT_DB_PASSWORD"],
        database="mixed_audit",
    )
