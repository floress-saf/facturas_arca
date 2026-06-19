"""
App Flask - Emisión de Facturas Electrónicas con ARCA (Homologación)
"""
import os
import hashlib
import datetime
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, jsonify, request, session, redirect, send_file
import mysql.connector
from wsfe import fe_comp_ultimo_autorizado, fe_cae_solicitar
from generar_pdf import generar_pdf_factura
from db import get_conexion, crear_tabla_emitidas, insertar_factura_emitida

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


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        if not session.get("es_admin"):
            return "Acceso denegado", 403
        return f(*args, **kwargs)
    return decorated
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
        cur.execute("SELECT * FROM usuariosarca WHERE cuil = %s AND password = %s", (cuil, password_md5))
        user = cur.fetchone()
        cur.close(); conn.close()
    except Exception:
        return render_template("login.html", error="Error de conexión.")

    if not user:
        return render_template("login.html", error="CUIL o contraseña incorrectos.")

    session["user_id"] = user["id"]
    session["cuil"] = user["cuil"]
    session["nombre"] = user.get("nombre", "").strip()
    session["cuit_emisor"] = user.get("cuit_emisor", "").strip()
    session["nombre_emisor"] = user.get("nombre_emisor", "").strip()
    session["cond_iva"] = user.get("cond_iva", "Responsable Inscripto").strip()
    session["domicilio"] = user.get("domicilio", "").strip()
    session["pto_vta"] = user.get("pto_vta", 1)
    session["es_admin"] = user.get("es_admin", 0)
    session["cert_path"] = user.get("cert_path", "").strip()
    session["key_path"] = user.get("key_path", "").strip()
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
        if session.get("es_admin"):
            cur.execute("SELECT COUNT(*) FROM factura_emitida")
        else:
            cur.execute("SELECT COUNT(*) FROM factura_emitida WHERE usuario = %s", (session.get("cuil", ""),))
        total = cur.fetchone()[0]
        cur.close(); conn.close()
    except Exception:
        pass
    return render_template("index.html", total=total)


@app.route("/emitir", methods=["GET"])
@login_required
def emitir():
    conceptos_usuario = _get_conceptos_usuario()
    return render_template("emitir.html", tipos=TIPOS_COMPROBANTE, alicuotas=ALICUOTAS_IVA, conceptos_usuario=conceptos_usuario)


@app.route("/emitir/lote", methods=["GET"])
@login_required
def emitir_lote():
    conceptos_usuario = _get_conceptos_usuario()
    return render_template("emitir_lote.html", tipos=TIPOS_COMPROBANTE, alicuotas=ALICUOTAS_IVA, conceptos_usuario=conceptos_usuario)


@app.route("/emitir/ultimo", methods=["GET"])
@login_required
def ultimo_comprobante():
    """Consulta el último comprobante autorizado para un punto de venta y tipo."""
    pto_vta = request.args.get("pto_vta", 1, type=int)
    tipo_cmp = request.args.get("tipo_cmp", 1, type=int)
    cuit_emisor = session.get("cuit_emisor", "")
    cert_path = session.get("cert_path") or (f"certs/{cuit_emisor}/certificado.crt" if cuit_emisor else None)
    key_path = session.get("key_path") or (f"certs/{cuit_emisor}/privada.key" if cuit_emisor else None)
    try:
        ultimo = fe_comp_ultimo_autorizado(pto_vta, tipo_cmp, cuit=int(cuit_emisor) if cuit_emisor else None, cert_path=cert_path, key_path=key_path)
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
    concepto = data.get("concepto", "")
    # Para ARCA siempre se envía concepto_id=1 (Productos)
    # El texto del concepto se guarda en la DB
    concepto_id = 1
    doc_tipo = int(data.get("doc_tipo", 80))
    moneda = data.get("moneda", "PES")
    iva_id = int(data.get("iva_id", 5))  # 5 = 21%

    # Fecha de hoy
    fecha = datetime.date.today().strftime("%Y%m%d")

    # Obtener siguiente número
    cuit_emisor = session.get("cuit_emisor", "")
    cert_path = session.get("cert_path") or (f"certs/{cuit_emisor}/certificado.crt" if cuit_emisor else None)
    key_path = session.get("key_path") or (f"certs/{cuit_emisor}/privada.key" if cuit_emisor else None)
    cuit_int = int(cuit_emisor) if cuit_emisor else None

    try:
        ultimo = fe_comp_ultimo_autorizado(pto_vta, tipo_cmp, cuit=cuit_int, cert_path=cert_path, key_path=key_path)
        nro_cmp = ultimo + 1
    except Exception as e:
        return jsonify({"error": f"Error al consultar último comprobante: {e}"}), 500

    # Armar datos para ARCA
    # Mapeo de condición IVA receptor a ID de ARCA
    COND_IVA_MAP = {
        "Responsable Inscripto": 1,
        "Monotributista": 6,
        "Exento": 4,
        "Consumidor Final": 5,
    }
    cond_iva_receptor = data.get("cond_iva_receptor", "Consumidor Final")
    cond_iva_receptor_id = COND_IVA_MAP.get(cond_iva_receptor, 5)

    datos_wsfe = {
        "tipo_cmp": tipo_cmp,
        "pto_vta": pto_vta,
        "nro_cmp": nro_cmp,
        "fecha": fecha,
        "concepto": concepto_id,
        "doc_tipo": doc_tipo,
        "doc_nro": doc_nro,
        "importe_total": importe_total,
        "importe_neto": importe_neto,
        "importe_iva": importe_iva,
        "importe_exento": importe_exento,
        "importe_no_gravado": 0,
        "moneda_id": moneda,
        "moneda_cotiz": 1,
        "condicion_iva_receptor_id": cond_iva_receptor_id,
        "iva_items": [{"id": iva_id, "base_imp": importe_neto, "importe": importe_iva}] if importe_iva > 0 else []
    }

    # Agregar fechas de servicio si corresponde
    if concepto_id in (2, 3):
        datos_wsfe["fch_serv_desde"] = data.get("fch_serv_desde", fecha)
        datos_wsfe["fch_serv_hasta"] = data.get("fch_serv_hasta", fecha)
        datos_wsfe["fch_vto_pago"] = data.get("fch_vto_pago", fecha)

    # Solicitar CAE
    try:
        resultado = fe_cae_solicitar(datos_wsfe, cuit=cuit_int, cert_path=cert_path, key_path=key_path)
    except Exception as e:
        return jsonify({"error": f"Error de comunicación con ARCA: {e}"}), 500

    if not resultado["ok"]:
        return jsonify({"error": f"ARCA rechazó el comprobante: {'; '.join(resultado['errores'])}"}), 400

    cae = resultado["cae"]
    cae_vto = resultado["cae_vto"]

    # Generar PDF
    datos_pdf = {
        "tipo_cmp": tipo_cmp,
        "tipo_cmp_nombre": TIPOS_COMPROBANTE.get(tipo_cmp, "COMPROBANTE"),
        "pto_vta": pto_vta,
        "nro_cmp": nro_cmp,
        "fecha": fecha,
        "cuit_emisor": cuit_emisor or os.getenv("AFIP_CUIT", "20237241275"),
        "nombre_emisor": session.get("nombre_emisor", ""),
        "domicilio_emisor": session.get("domicilio", ""),
        "cond_iva_emisor": session.get("cond_iva", "Responsable Inscripto"),
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
    page     = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    buscar   = request.args.get("q", "").strip()

    per_page = min(max(per_page, 10), 100)
    page     = max(page, 1)
    offset   = (page - 1) * per_page

    # Filtro por usuario: admin ve todo, otros solo sus facturas
    es_admin = session.get("es_admin", 0)
    cuil = session.get("cuil", "")

    registros = []
    total = 0
    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)

        if buscar:
            like = f"%{buscar}%"
            if es_admin:
                filtro = """WHERE nombre_receptor LIKE %s OR cae LIKE %s OR doc_nro LIKE %s
                            OR fecha LIKE %s OR nro_cmp LIKE %s"""
                params = (like, like, like, like, like)
            else:
                filtro = """WHERE usuario = %s AND (nombre_receptor LIKE %s OR cae LIKE %s OR doc_nro LIKE %s
                            OR fecha LIKE %s OR nro_cmp LIKE %s)"""
                params = (cuil, like, like, like, like, like)
            cur.execute(f"SELECT COUNT(*) as total FROM factura_emitida {filtro}", params)
            total = cur.fetchone()["total"]
            cur.execute(f"SELECT * FROM factura_emitida {filtro} ORDER BY id DESC LIMIT %s OFFSET %s", params + (per_page, offset))
        else:
            if es_admin:
                cur.execute("SELECT COUNT(*) as total FROM factura_emitida")
                total = cur.fetchone()["total"]
                cur.execute("SELECT * FROM factura_emitida ORDER BY id DESC LIMIT %s OFFSET %s", (per_page, offset))
            else:
                cur.execute("SELECT COUNT(*) as total FROM factura_emitida WHERE usuario = %s", (cuil,))
                total = cur.fetchone()["total"]
                cur.execute("SELECT * FROM factura_emitida WHERE usuario = %s ORDER BY id DESC LIMIT %s OFFSET %s", (cuil, per_page, offset))

        registros = cur.fetchall()
        cur.close(); conn.close()
    except Exception:
        pass

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    return render_template("emitidas.html", registros=registros, tipos=TIPOS_COMPROBANTE,
                           page=page, per_page=per_page, total=total,
                           total_pages=total_pages, buscar=buscar)


@app.route("/emitidas/pdf/<int:tipo>/<int:pto_vta>/<int:nro_cmp>")
@login_required
def descargar_pdf(tipo, pto_vta, nro_cmp):
    pdf_filename = f"{tipo}_{pto_vta}_{nro_cmp}.pdf"
    pdf_path = os.path.join(PDF_FOLDER, pdf_filename)
    if not os.path.isfile(pdf_path):
        return jsonify({"error": "PDF no encontrado."}), 404
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=False,
                     download_name=pdf_filename)


# --- CRUD Usuarios (solo admin) ---
@app.route("/usuarios")
@admin_required
def listar_usuarios():
    usuarios = []
    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM usuariosarca ORDER BY nombre")
        usuarios = cur.fetchall()
        cur.close(); conn.close()
    except Exception:
        pass
    return render_template("usuarios.html", usuarios=usuarios)


@app.route("/usuarios/nuevo", methods=["GET", "POST"])
@admin_required
def nuevo_usuario():
    if request.method == "GET":
        return render_template("usuario_form.html", usuario=None)

    data = request.form
    cuil = data.get("cuil", "").strip()
    nombre = data.get("nombre", "").strip()
    password = data.get("password", "").strip()
    cuit_emisor = data.get("cuit_emisor", "").strip()
    nombre_emisor = data.get("nombre_emisor", "").strip()
    cond_iva = data.get("cond_iva", "Responsable Inscripto")
    domicilio = data.get("domicilio", "").strip()
    pto_vta = int(data.get("pto_vta", 1))
    es_admin = 1 if data.get("es_admin") else 0
    email = data.get("email", "").strip().lower()

    if not cuil or not nombre or not password:
        return render_template("usuario_form.html", usuario=None, error="CUIL, nombre y contraseña son obligatorios.")

    password_md5 = hashlib.md5(password.encode()).hexdigest()

    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usuariosarca (cuil, nombre, password, email, cuit_emisor, nombre_emisor, cond_iva, domicilio, pto_vta, es_admin)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (cuil, nombre, password_md5, email, cuit_emisor, nombre_emisor, cond_iva, domicilio, pto_vta, es_admin))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        return render_template("usuario_form.html", usuario=None, error=f"Error: {e}")

    # Guardar certificados si se subieron
    cert_error = _guardar_certificados(cuit_emisor, request.files)
    if cert_error:
        return render_template("usuario_form.html", usuario=None, error=f"Usuario creado pero error en certificados: {cert_error}")

    return redirect("/usuarios")


@app.route("/usuarios/editar/<int:id>", methods=["GET", "POST"])
@admin_required
def editar_usuario(id):
    conn = get_conexion()
    cur = conn.cursor(dictionary=True)

    if request.method == "GET":
        cur.execute("SELECT * FROM usuariosarca WHERE id = %s", (id,))
        usuario = cur.fetchone()
        cur.close(); conn.close()
        if not usuario:
            return redirect("/usuarios")
        return render_template("usuario_form.html", usuario=usuario)

    data = request.form
    nombre = data.get("nombre", "").strip()
    cuit_emisor = data.get("cuit_emisor", "").strip()
    nombre_emisor = data.get("nombre_emisor", "").strip()
    cond_iva = data.get("cond_iva", "Responsable Inscripto")
    domicilio = data.get("domicilio", "").strip()
    pto_vta = int(data.get("pto_vta", 1))
    es_admin = 1 if data.get("es_admin") else 0
    password = data.get("password", "").strip()
    email = data.get("email", "").strip().lower()

    try:
        if password:
            password_md5 = hashlib.md5(password.encode()).hexdigest()
            cur.execute("""
                UPDATE usuariosarca SET nombre=%s, password=%s, email=%s, cuit_emisor=%s, nombre_emisor=%s,
                    cond_iva=%s, domicilio=%s, pto_vta=%s, es_admin=%s WHERE id=%s
            """, (nombre, password_md5, email, cuit_emisor, nombre_emisor, cond_iva, domicilio, pto_vta, es_admin, id))
        else:
            cur.execute("""
                UPDATE usuariosarca SET nombre=%s, email=%s, cuit_emisor=%s, nombre_emisor=%s,
                    cond_iva=%s, domicilio=%s, pto_vta=%s, es_admin=%s WHERE id=%s
            """, (nombre, email, cuit_emisor, nombre_emisor, cond_iva, domicilio, pto_vta, es_admin, id))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        cur.close(); conn.close()
        return render_template("usuario_form.html", usuario=data, error=f"Error: {e}")

    # Guardar certificados si se subieron
    _guardar_certificados(cuit_emisor, request.files)

    return redirect("/usuarios")


@app.route("/usuarios/eliminar/<int:id>", methods=["POST"])
@admin_required
def eliminar_usuario(id):
    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute("DELETE FROM usuariosarca WHERE id = %s AND es_admin = 0", (id,))
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
    return redirect("/usuarios")


def _guardar_certificados(cuit_emisor, files):
    """Guarda los archivos de certificado y clave privada en certs/{cuit}/ con validación."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    if not cuit_emisor:
        return None

    cert_file = files.get("certificado")
    key_file = files.get("clave_privada")
    errores = []

    if not (cert_file and cert_file.filename) and not (key_file and key_file.filename):
        return None

    cert_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", cuit_emisor)
    os.makedirs(cert_dir, exist_ok=True)

    # Validar y guardar certificado
    cert_path_saved = ""
    if cert_file and cert_file.filename:
        cert_data = cert_file.read()
        try:
            x509.load_pem_x509_certificate(cert_data)
            cert_path_saved = os.path.join(cert_dir, "certificado.crt")
            with open(cert_path_saved, "wb") as f:
                f.write(cert_data)
        except Exception as e:
            errores.append(f"Certificado inválido: {e}")

    # Validar y guardar clave privada
    key_path_saved = ""
    if key_file and key_file.filename:
        key_data = key_file.read()
        try:
            serialization.load_pem_private_key(key_data, password=None)
            key_path_saved = os.path.join(cert_dir, "privada.key")
            with open(key_path_saved, "wb") as f:
                f.write(key_data)
        except Exception as e:
            errores.append(f"Clave privada inválida: {e}")

    # Guardar paths en la tabla usuariosarca
    if (cert_path_saved or key_path_saved) and not errores:
        try:
            conn = get_conexion()
            cur = conn.cursor()
            if cert_path_saved and key_path_saved:
                cur.execute("UPDATE usuariosarca SET cert_path=%s, key_path=%s WHERE cuit_emisor=%s",
                            (f"certs/{cuit_emisor}/certificado.crt", f"certs/{cuit_emisor}/privada.key", cuit_emisor))
            elif cert_path_saved:
                cur.execute("UPDATE usuariosarca SET cert_path=%s WHERE cuit_emisor=%s",
                            (f"certs/{cuit_emisor}/certificado.crt", cuit_emisor))
            elif key_path_saved:
                cur.execute("UPDATE usuariosarca SET key_path=%s WHERE cuit_emisor=%s",
                            (f"certs/{cuit_emisor}/privada.key", cuit_emisor))
            conn.commit()
            cur.close(); conn.close()
        except Exception:
            pass

    return "; ".join(errores) if errores else None


def _get_conceptos_usuario():
    """Obtiene los conceptos del usuario logueado."""
    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, concepto FROM conceptos_usuario WHERE usuario = %s ORDER BY concepto", (session.get("cuil", ""),))
        conceptos = cur.fetchall()
        cur.close(); conn.close()
        return conceptos
    except Exception:
        return []


# --- CRUD Conceptos por usuario ---
@app.route("/conceptos")
@login_required
def listar_conceptos():
    registros = []
    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)
        if session.get("es_admin"):
            cur.execute("SELECT * FROM conceptos_usuario ORDER BY usuario, concepto")
        else:
            cur.execute("SELECT * FROM conceptos_usuario WHERE usuario = %s ORDER BY concepto", (session.get("cuil", ""),))
        registros = cur.fetchall()
        cur.close(); conn.close()
    except Exception:
        pass
    return render_template("conceptos.html", active="conceptos", registros=registros)


@app.route("/conceptos/nuevo", methods=["GET", "POST"])
@login_required
def nuevo_concepto():
    if request.method == "GET":
        return render_template("concepto_form.html", active="conceptos", concepto=None)

    data = request.form
    concepto_txt = data.get("concepto", "").strip().upper()

    if not concepto_txt:
        return render_template("concepto_form.html", active="conceptos", concepto=None, error="El concepto es obligatorio.")

    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute("INSERT INTO conceptos_usuario (concepto, usuario) VALUES (%s, %s)", (concepto_txt, session.get("cuil", "")))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        return render_template("concepto_form.html", active="conceptos", concepto=None, error=f"Error: {e}")

    return redirect("/conceptos")


@app.route("/conceptos/editar/<int:id>", methods=["GET", "POST"])
@login_required
def editar_concepto(id):
    conn = get_conexion()
    cur = conn.cursor(dictionary=True)

    if request.method == "GET":
        cur.execute("SELECT * FROM conceptos_usuario WHERE id = %s", (id,))
        concepto = cur.fetchone()
        cur.close(); conn.close()
        if not concepto:
            return redirect("/conceptos")
        if not session.get("es_admin") and concepto.get("usuario") != session.get("cuil", ""):
            return "Acceso denegado", 403
        return render_template("concepto_form.html", active="conceptos", concepto=concepto)

    data = request.form
    concepto_txt = data.get("concepto", "").strip().upper()

    try:
        if session.get("es_admin"):
            cur.execute("UPDATE conceptos_usuario SET concepto = %s WHERE id = %s", (concepto_txt, id))
        else:
            cur.execute("UPDATE conceptos_usuario SET concepto = %s WHERE id = %s AND usuario = %s", (concepto_txt, id, session.get("cuil", "")))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        cur.close(); conn.close()
        return render_template("concepto_form.html", active="conceptos", concepto={"id": id, "concepto": concepto_txt}, error=f"Error: {e}")

    return redirect("/conceptos")


@app.route("/conceptos/eliminar/<int:id>", methods=["POST"])
@login_required
def eliminar_concepto(id):
    try:
        conn = get_conexion()
        cur = conn.cursor()
        if session.get("es_admin"):
            cur.execute("DELETE FROM conceptos_usuario WHERE id = %s", (id,))
        else:
            cur.execute("DELETE FROM conceptos_usuario WHERE id = %s AND usuario = %s", (id, session.get("cuil", "")))
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
    return redirect("/conceptos")


# --- CRUD Condición de Venta (solo admin) ---
@app.route("/condiciones")
@admin_required
def listar_condiciones():
    registros = []
    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM condicion_venta ORDER BY condicion")
        registros = cur.fetchall()
        cur.close(); conn.close()
    except Exception:
        pass
    return render_template("condiciones.html", active="condiciones", registros=registros)


@app.route("/condiciones/nuevo", methods=["GET", "POST"])
@admin_required
def nueva_condicion():
    if request.method == "GET":
        return render_template("condicion_form.html", active="condiciones", condicion=None)

    data = request.form
    condicion_txt = data.get("condicion", "").strip().upper()

    if not condicion_txt:
        return render_template("condicion_form.html", active="condiciones", condicion=None, error="La condición es obligatoria.")

    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute("INSERT INTO condicion_venta (condicion) VALUES (%s)", (condicion_txt,))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        return render_template("condicion_form.html", active="condiciones", condicion=None, error=f"Error: {e}")

    return redirect("/condiciones")


@app.route("/condiciones/editar/<int:id>", methods=["GET", "POST"])
@admin_required
def editar_condicion(id):
    conn = get_conexion()
    cur = conn.cursor(dictionary=True)

    if request.method == "GET":
        cur.execute("SELECT * FROM condicion_venta WHERE id = %s", (id,))
        condicion = cur.fetchone()
        cur.close(); conn.close()
        if not condicion:
            return redirect("/condiciones")
        return render_template("condicion_form.html", active="condiciones", condicion=condicion)

    data = request.form
    condicion_txt = data.get("condicion", "").strip().upper()

    try:
        cur.execute("UPDATE condicion_venta SET condicion = %s WHERE id = %s", (condicion_txt, id))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        cur.close(); conn.close()
        return render_template("condicion_form.html", active="condiciones", condicion={"id": id, "condicion": condicion_txt}, error=f"Error: {e}")

    return redirect("/condiciones")


@app.route("/condiciones/eliminar/<int:id>", methods=["POST"])
@admin_required
def eliminar_condicion(id):
    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute("DELETE FROM condicion_venta WHERE id = %s", (id,))
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
    return redirect("/condiciones")


# --- CRUD Clientes (cada usuario ve los suyos) ---
@app.route("/clientes")
@login_required
def listar_clientes():
    registros = []
    try:
        conn = get_conexion()
        cur = conn.cursor(dictionary=True)
        if session.get("es_admin"):
            cur.execute("SELECT * FROM clientes ORDER BY razsoc")
        else:
            cur.execute("SELECT * FROM clientes WHERE usuario = %s ORDER BY razsoc", (session.get("cuil", ""),))
        registros = cur.fetchall()
        cur.close(); conn.close()
    except Exception:
        pass
    return render_template("clientes.html", active="clientes", registros=registros)


@app.route("/clientes/nuevo", methods=["GET", "POST"])
@login_required
def nuevo_cliente():
    if request.method == "GET":
        return render_template("cliente_form.html", active="clientes", cliente=None)

    data = request.form
    cuit = data.get("cuit", "").strip()
    razsoc = data.get("razsoc", "").strip().upper()
    mail = data.get("mail", "").strip().lower()
    condicion_iva = data.get("condicion_iva", "Consumidor Final")

    if not cuit or not razsoc:
        return render_template("cliente_form.html", active="clientes", cliente=None, error="CUIT y Razón Social son obligatorios.")

    try:
        conn = get_conexion()
        cur = conn.cursor()
        cur.execute("INSERT INTO clientes (cuit, razsoc, mail, condicion_iva, usuario) VALUES (%s, %s, %s, %s, %s)",
                    (cuit, razsoc, mail, condicion_iva, session.get("cuil", "")))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        return render_template("cliente_form.html", active="clientes", cliente=None, error=f"Error: {e}")

    return redirect("/clientes")


@app.route("/clientes/editar/<int:id>", methods=["GET", "POST"])
@login_required
def editar_cliente(id):
    conn = get_conexion()
    cur = conn.cursor(dictionary=True)

    if request.method == "GET":
        cur.execute("SELECT * FROM clientes WHERE id = %s", (id,))
        cliente = cur.fetchone()
        cur.close(); conn.close()
        if not cliente:
            return redirect("/clientes")
        if not session.get("es_admin") and cliente.get("usuario") != session.get("cuil", ""):
            return "Acceso denegado", 403
        return render_template("cliente_form.html", active="clientes", cliente=cliente)

    data = request.form
    cuit = data.get("cuit", "").strip()
    razsoc = data.get("razsoc", "").strip().upper()
    mail = data.get("mail", "").strip().lower()
    condicion_iva = data.get("condicion_iva", "Consumidor Final")

    try:
        if session.get("es_admin"):
            cur.execute("UPDATE clientes SET cuit=%s, razsoc=%s, mail=%s, condicion_iva=%s WHERE id=%s",
                        (cuit, razsoc, mail, condicion_iva, id))
        else:
            cur.execute("UPDATE clientes SET cuit=%s, razsoc=%s, mail=%s, condicion_iva=%s WHERE id=%s AND usuario=%s",
                        (cuit, razsoc, mail, condicion_iva, id, session.get("cuil", "")))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        cur.close(); conn.close()
        return render_template("cliente_form.html", active="clientes", cliente=data, error=f"Error: {e}")

    return redirect("/clientes")


@app.route("/clientes/eliminar/<int:id>", methods=["POST"])
@login_required
def eliminar_cliente(id):
    try:
        conn = get_conexion()
        cur = conn.cursor()
        if session.get("es_admin"):
            cur.execute("DELETE FROM clientes WHERE id = %s", (id,))
        else:
            cur.execute("DELETE FROM clientes WHERE id = %s AND usuario = %s", (id, session.get("cuil", "")))
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
    return redirect("/clientes")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
