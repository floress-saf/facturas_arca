"""
Módulo de base de datos para facturas emitidas.
"""
import os
import mysql.connector
import logging

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "comprobantes"),
}


def get_conexion():
    return mysql.connector.connect(**DB_CONFIG)


def crear_tabla_emitidas():
    sql = """
        CREATE TABLE IF NOT EXISTS factura_emitida (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            tipo_cmp        INT            NOT NULL,
            pto_vta         INT            NOT NULL,
            nro_cmp         BIGINT         NOT NULL,
            fecha           VARCHAR(10)    NOT NULL,
            concepto        INT            DEFAULT 1,
            doc_tipo        INT            DEFAULT 80,
            doc_nro         BIGINT         NOT NULL,
            nombre_receptor VARCHAR(100)   DEFAULT '',
            importe_neto    DECIMAL(15,2)  DEFAULT 0,
            importe_iva     DECIMAL(15,2)  DEFAULT 0,
            importe_exento  DECIMAL(15,2)  DEFAULT 0,
            importe_total   DECIMAL(15,2)  DEFAULT 0,
            moneda          VARCHAR(5)     DEFAULT 'PES',
            cae             VARCHAR(14)    DEFAULT '',
            cae_vto         VARCHAR(8)     DEFAULT '',
            resultado       VARCHAR(2)     DEFAULT '',
            archivo_pdf     VARCHAR(500)   DEFAULT '',
            usuario         VARCHAR(11)    DEFAULT '',
            created_at      TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_comprobante (tipo_cmp, pto_vta, nro_cmp)
        )
    """
    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        cur.close(); conn.close()
        log.info("Tabla 'factura_emitida' verificada/creada.")
    except mysql.connector.Error as e:
        log.error(f"Error al crear tabla factura_emitida: {e}")


def insertar_factura_emitida(datos):
    sql = """
        INSERT INTO factura_emitida
            (tipo_cmp, pto_vta, nro_cmp, fecha, concepto, doc_tipo, doc_nro,
             nombre_receptor, importe_neto, importe_iva, importe_exento,
             importe_total, moneda, cae, cae_vto, resultado, archivo_pdf, usuario)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute(sql, (
            datos["tipo_cmp"],
            datos["pto_vta"],
            datos["nro_cmp"],
            datos["fecha"],
            datos.get("concepto", 1),
            datos.get("doc_tipo", 80),
            datos["doc_nro"],
            datos.get("nombre_receptor", ""),
            datos.get("importe_neto", 0),
            datos.get("importe_iva", 0),
            datos.get("importe_exento", 0),
            datos["importe_total"],
            datos.get("moneda", "PES"),
            datos.get("cae", ""),
            datos.get("cae_vto", ""),
            datos.get("resultado", ""),
            datos.get("archivo_pdf", ""),
            datos.get("usuario", ""),
        ))
        conn.commit()
        cur.close(); conn.close()
        return True
    except mysql.connector.Error as e:
        log.error(f"Error al insertar factura emitida: {e}")
        return False
