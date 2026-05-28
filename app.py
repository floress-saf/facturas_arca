"""
App Flask - Emisión de Facturas Electrónicas con ARCA (Homologación)
"""
import os
import hashlib
import datetime
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, send_file
from dotenv import load_dotenv
import mysql.connector
from wsfe import fe_comp_ultimo_autorizado, fe_cae_solicitar
from generar_pdf import generar_pdf_factura
from db import get_conexion, crear_tabla_emitidas, insertar_factura_emitida

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.secret_key = os.getenv("SECRET_KEY", "osa-facturas-arca-2026")

PDF_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdfs")
os.makedirs(PDF_FOLDER, exist_ok=True)

# Crear tabla al iniciar
crear_tabla_emitidas()

# --- Tipos de comprobante ---
TIPOS_COMPROBANTE = {
    1: "Factura A", 2: "Nota de Débito A", 3: "Nota de Crédito A",
    6: "Factura B", 7: "Nota de Débito B", 8: "Nota de Crédito B",
    11: "Factura C", 12: "Nota de Débito C", 13: "Nota de Crédito C",
}

# --- Alícuotas IVA ---
ALICUOTAS_IVA = {
    3: {"desc": "0%", "porcentaje": 0},
    4: {"desc": "10.5%", "porcentaje": 10.5},
    5: {"desc": "21%", "porcentaje": 21},
    6: {"desc": "27%", "porcentaje": 27},
}


# --- Auth ---
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if "user_id" in session:
            return redirect("/")
        return render_template("login.html")

    cuil = request.form.get("cuil", "").strip()
    password = request.form.get("password", "").strip()

    if not cuil or not password:
        return render_template("login.html", error="Completá todos los campos.")

    password_md5 = hashlib.md5(password.encode()).hexdigest()

    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM usuarios WHERE cuil = %s AND password = %s", (cuil, password_md5))
        user = cur.fetchone()
        cur.close(); conn.close()
    except Exception:
        return render_template("login.html", error="Error de conexión.")

    if not user:
        return render_template("login.html", error="CUIL o contraseña incorrectos.")

    session["user_id"] = user["id"]
    session["cuil"] = user["cuil"]
    session["nombre"] = user.get("nombre", "").strip()
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --- Rutas principales ---
@app.route("/")
@login_required
def index():
    total = 0
    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM factura_emitida")
        total = cur.fetchone()[0]
        cur.close(); conn.close()
    except Exception:
        pass
    return render_template("index.html", total=total)


@app.route("/emitir", methods=["GET"])
@login_required
def emitir():
    return render_template("emitir.html", tipos=TIPOS_COMPROBANTE, alicuotas=ALICUOTAS_IVA)


@app.route("/emitir/ultimo", methods=["GET"])
@login_required
def ultimo_comprobante():
    """Consulta el último comprobante autorizado para un punto de venta y tipo."""
    pto_vta = request.args.get("pto_vta", 1, type=int)
    tipo_cmp = request.args.get("tipo_cmp", 1, type=int)
    try:
        ultimo = fe_comp_ultimo_autorizado(pto_vta, tipo_cmp)
        return jsonify({"ok": True, "ultimo": ultimo, "siguiente": ultimo + 1})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/emitir/solicitar", methods=["POST"])
@login_required
def solicitar_cae():
    """Solicita CAE a ARCA y genera el PDF."""
    data = request.get_json()

    campos_requeridos = ["tipo_cmp", "pto_vta", "doc_nro", "importe_total"]
    for campo in campos_requeridos:
        if not data.get(campo):
            return jsonify({"error": f"El campo {campo} es obligatorio."}), 400

    tipo_cmp = int(data["tipo_cmp"])
    pto_vta = int(data["pto_vta"])
    doc_nro = int(data["doc_nro"])
    importe_total = float(data["importe_total"])
    importe_neto = float(data.get("importe_neto", importe_total))
    importe_iva = float(data.get("importe_iva", 0))
    importe_exento = float(data.get("importe_exento", 0))
    concepto = int(data.get("concepto", 1))
    doc_tipo = int(data.get("doc_tipo", 80))
    moneda = data.get("moneda", "PES")
    iva_id = int(data.get("iva_id", 5))  # 5 = 21%

    # Fecha de hoy
    fecha = datetime.date.today().strftime("%Y%m%d")

    # Obtener siguiente número
    try:
        ultimo = fe_comp_ultimo_autorizado(pto_vta, tipo_cmp)
        nro_cmp = ultimo + 1
    except Exception as e:
        return jsonify({"error": f"Error al consultar último comprobante: {e}"}), 500

    # Armar datos para ARCA
    datos_wsfe = {
        "tipo_cmp": tipo_cmp,
        "pto_vta": pto_vta,
        "nro_cmp": nro_cmp,
        "fecha": fecha,
        "concepto": concepto,
        "doc_tipo": doc_tipo,
        "doc_nro": doc_nro,
        "importe_total": importe_total,
        "importe_neto": importe_neto,
        "importe_iva": importe_iva,
        "importe_exento": importe_exento,
        "importe_no_gravado": 0,
        "moneda_id": moneda,
        "moneda_cotiz": 1,
        "iva_items": [{"id": iva_id, "base_imp": importe_neto, "importe": importe_iva}] if importe_iva > 0 else []
    }

    # Agregar fechas de servicio si corresponde
    if concepto in (2, 3):
        datos_wsfe["fch_serv_desde"] = data.get("fch_serv_desde", fecha)
        datos_wsfe["fch_serv_hasta"] = data.get("fch_serv_hasta", fecha)
        datos_wsfe["fch_vto_pago"] = data.get("fch_vto_pago", fecha)

    # Solicitar CAE
    try:
        resultado = fe_cae_solicitar(datos_wsfe)
    except Exception as e:
        return jsonify({"error": f"Error de comunicación con ARCA: {e}"}), 500

    if not resultado["ok"]:
        return jsonify({"error": f"ARCA rechazó el comprobante: {'; '.join(resultado['errores'])}"}), 400

    cae = resultado["cae"]
    cae_vto = resultado["cae_vto"]

    # Generar PDF
    cuit_emisor = os.getenv("AFIP_CUIT", "20237241275")
    datos_pdf = {
        "tipo_cmp": tipo_cmp,
        "tipo_cmp_nombre": TIPOS_COMPROBANTE.get(tipo_cmp, "COMPROBANTE"),
        "pto_vta": pto_vta,
        "nro_cmp": nro_cmp,
        "fecha": fecha,
        "cuit_emisor": cuit_emisor,
        "nombre_emisor": data.get("nombre_emisor", "FLORES SERGIO"),
        "domicilio_emisor": data.get("domicilio_emisor", ""),
        "cond_iva_emisor": data.get("cond_iva_emisor", "Responsable Inscripto"),
        "doc_tipo": doc_tipo,
        "doc_nro": doc_nro,
        "nombre_receptor": data.get("nombre_receptor", ""),
        "cond_iva_receptor": data.get("cond_iva_receptor", ""),
        "importe_neto": importe_neto,
        "importe_iva": importe_iva,
        "importe_total": importe_total,
        "cae": cae,
        "cae_vto": cae_vto,
        "moneda": moneda,
        "items": data.get("items", [{"descripcion": "Servicio", "cantidad": 1, "precio": importe_neto, "subtotal": importe_neto}])
    }

    pdf_filename = f"{tipo_cmp}_{pto_vta}_{nro_cmp}.pdf"
    pdf_path = os.path.join(PDF_FOLDER, pdf_filename)
    generar_pdf_factura(datos_pdf, pdf_path)

    # Guardar en DB
    insertar_factura_emitida({
        "tipo_cmp": tipo_cmp,
        "pto_vta": pto_vta,
        "nro_cmp": nro_cmp,
        "fecha": fecha,
        "concepto": concepto,
        "doc_tipo": doc_tipo,
        "doc_nro": doc_nro,
        "nombre_receptor": data.get("nombre_receptor", ""),
        "importe_neto": importe_neto,
        "importe_iva": importe_iva,
        "importe_exento": importe_exento,
        "importe_total": importe_total,
        "moneda": moneda,
        "cae": cae,
        "cae_vto": cae_vto,
        "resultado": "A",
        "archivo_pdf": pdf_path,
        "usuario": session.get("cuil", ""),
    })

    return jsonify({
        "ok": True,
        "cae": cae,
        "cae_vto": cae_vto,
        "nro_cmp": nro_cmp,
        "pdf": f"/emitidas/pdf/{tipo_cmp}/{pto_vta}/{nro_cmp}"
    })


@app.route("/emitidas")
@login_required
def emitidas():
    registros = []
    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT * FROM factura_emitida ORDER BY id DESC LIMIT 100
        """)
        registros = cur.fetchall()
        cur.close(); conn.close()
    except Exception:
        pass
    return render_template("emitidas.html", registros=registros, tipos=TIPOS_COMPROBANTE)


@app.route("/emitidas/pdf/<int:tipo>/<int:pto_vta>/<int:nro_cmp>")
@login_required
def descargar_pdf(tipo, pto_vta, nro_cmp):
    pdf_filename = f"{tipo}_{pto_vta}_{nro_cmp}.pdf"
    pdf_path = os.path.join(PDF_FOLDER, pdf_filename)
    if not os.path.isfile(pdf_path):
        return jsonify({"error": "PDF no encontrado."}), 404
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=False,
                     download_name=pdf_filename)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
