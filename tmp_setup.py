import mysql.connector
conn = mysql.connector.connect(host='localhost', user='root', password='', database='comprobantes')
cur = conn.cursor()

# Agregar columnas
columnas = [
    "ALTER TABLE usuarios ADD COLUMN cuit_emisor VARCHAR(11) DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN nombre_emisor VARCHAR(100) DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN cond_iva VARCHAR(30) DEFAULT 'Responsable Inscripto'",
    "ALTER TABLE usuarios ADD COLUMN domicilio VARCHAR(200) DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN pto_vta INT DEFAULT 1",
    "ALTER TABLE usuarios ADD COLUMN es_admin TINYINT(1) DEFAULT 0",
]

for sql in columnas:
    try:
        cur.execute(sql)
    except Exception as e:
        print(f"  Skip: {e}")

conn.commit()

# Marcar tu usuario como admin
cur.execute("UPDATE usuarios SET cuit_emisor='20237241275', nombre_emisor='FLORES SERGIO', es_admin=1 WHERE cuil='20237241275'")
conn.commit()
cur.close(); conn.close()
print("OK - Tabla actualizada")
