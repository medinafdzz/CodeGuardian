from mysql.connector import connection


def connect_audit_database():
    return connection.MySQLConnection(
        host="audit.internal",
        user="audit_reader",
        password="",
        database="mixed_audit",
    )
