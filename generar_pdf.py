"""
Generador de PDF de factura electrónica con QR de ARCA.
"""
import os
import io
import base64
import json
import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib import colors


def generar_qr_afip(datos_factura):
    """Genera la URL del QR según especificación de ARCA."""
    qr_data = {
        "ver": 1,
        "fecha": datos_factura["fecha"],
        "cuit": int(datos_factura["cuit_emisor"]),
        "ptoVta": int(datos_factura["pto_vta"]),
        "tipoCmp": int(datos_factura["tipo_cmp"]),
        "nroCmp": int(datos_factura["nro_cmp"]),
        "importe": float(datos_factura["importe_total"]),
        "moneda": datos_factura.get("moneda", "PES"),
        "ctz": float(datos_factura.get("cotizacion", 1)),
        "tipoDocRec": int(datos_factura.get("doc_tipo", 80)),
        "nroDocRec": int(datos_factura["doc_nro"]),
        "tipoCodAut": "E",
        "codAut": int(datos_factura["cae"]),
    }
    json_str = json.dumps(qr_data, separators=(',', ':'))
    b64 = base64.b64encode(json_str.encode()).decode()
    url = f"https://www.afip.gob.ar/fe/qr/?p={b64}"
    return url


def generar_pdf_factura(datos_factura, output_path=None):
    """
    Genera un PDF de factura electrónica.
    
    datos_factura = {
        "tipo_cmp": 1,
        "tipo_cmp_nombre": "FACTURA A",
        "pto_vta": 1,
        "nro_cmp": 1,
        "fecha": "20260528",
        "cuit_emisor": "20237241275",
        "nombre_emisor": "FLORES SERGIO",
        "domicilio_emisor": "...",
        "cond_iva_emisor": "Responsable Inscripto",
        "doc_tipo": 80,
        "doc_nro": "",
        "nombre_receptor": "",
        "domicilio_receptor": "...",
        "cond_iva_receptor": "Consumidor Final",
        "importe_neto": 826.45,
        "importe_iva": 173.55,
        "importe_total": 1000.00,
        "cae": "74123456789012",
        "cae_vto": "20260607",
        "moneda": "PES",
        "items": [
            {"descripcion": "Servicio profesional", "cantidad": 1, "precio": 826.45, "subtotal": 826.45}
        ]
    }
    """
    if output_path is None:
        output_path = io.BytesIO()

    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    # --- Encabezado ---
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30*mm, height - 20*mm, datos_factura.get("nombre_emisor", ""))
    
    # Tipo de comprobante
    c.setFont("Helvetica-Bold", 16)
    tipo_nombre = datos_factura.get("tipo_cmp_nombre", "FACTURA")
    c.drawCentredString(width/2, height - 20*mm, tipo_nombre)

    # Letra del comprobante
    letra = tipo_nombre[-1] if tipo_nombre else ""
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width/2, height - 32*mm, letra)

    # Número de comprobante
    c.setFont("Helvetica", 10)
    pto_vta = str(datos_factura["pto_vta"]).zfill(5)
    nro_cmp = str(datos_factura["nro_cmp"]).zfill(8)
    c.drawRightString(width - 30*mm, height - 20*mm, f"Comp. Nro: {pto_vta}-{nro_cmp}")

    # Fecha
    fecha_raw = datos_factura["fecha"]
    if len(fecha_raw) == 8:
        fecha_fmt = f"{fecha_raw[6:8]}/{fecha_raw[4:6]}/{fecha_raw[:4]}"
    else:
        fecha_fmt = fecha_raw
    c.drawRightString(width - 30*mm, height - 26*mm, f"Fecha: {fecha_fmt}")

    # --- Datos del emisor ---
    y = height - 45*mm
    c.setFont("Helvetica", 9)
    c.drawString(30*mm, y, f"CUIT: {datos_factura['cuit_emisor']}")
    c.drawString(30*mm, y - 4*mm, f"Condición IVA: {datos_factura.get('cond_iva_emisor', '')}")
    c.drawString(30*mm, y - 8*mm, f"Domicilio: {datos_factura.get('domicilio_emisor', '')}")

    # --- Datos del receptor ---
    y -= 16*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(30*mm, y, "DATOS DEL RECEPTOR")
    c.setFont("Helvetica", 9)
    c.drawString(30*mm, y - 5*mm, f"Razón Social: {datos_factura.get('nombre_receptor', '')}")
    c.drawString(30*mm, y - 9*mm, f"CUIT: {datos_factura['doc_nro']}")
    c.drawString(30*mm, y - 13*mm, f"Condición IVA: {datos_factura.get('cond_iva_receptor', '')}")
    c.drawString(30*mm, y - 17*mm, f"Condición de Venta: {datos_factura.get('condicion_venta', '')}")

    # --- Detalle / Items ---
    y -= 25*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(30*mm, y, "Descripción")
    c.drawString(120*mm, y, "Cant.")
    c.drawString(140*mm, y, "Precio Unit.")
    c.drawString(170*mm, y, "Subtotal")
    c.line(30*mm, y - 2*mm, width - 30*mm, y - 2*mm)

    c.setFont("Helvetica", 9)
    y -= 7*mm
    for item in datos_factura.get("items", []):
        c.drawString(30*mm, y, str(item.get("descripcion", "")))
        c.drawString(120*mm, y, str(item.get("cantidad", 1)))
        c.drawRightString(165*mm, y, f"${item.get('precio', 0):.2f}")
        c.drawRightString(190*mm, y, f"${item.get('subtotal', 0):.2f}")
        y -= 5*mm

    # --- Totales ---
    y -= 10*mm
    c.line(30*mm, y + 3*mm, width - 30*mm, y + 3*mm)
    c.setFont("Helvetica", 10)
    c.drawString(130*mm, y, f"Subtotal: ${datos_factura.get('importe_neto', 0):.2f}")
    y -= 5*mm
    c.drawString(130*mm, y, f"IVA 21%: ${datos_factura.get('importe_iva', 0):.2f}")
    y -= 6*mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(130*mm, y, f"TOTAL: ${datos_factura['importe_total']:.2f}")

    # --- CAE y QR ---
    y -= 15*mm
    c.setFont("Helvetica", 9)
    cae_vto = datos_factura.get("cae_vto", "")
    if len(cae_vto) == 8:
        cae_vto_fmt = f"{cae_vto[6:8]}/{cae_vto[4:6]}/{cae_vto[:4]}"
    else:
        cae_vto_fmt = cae_vto
    c.drawString(30*mm, y, f"CAE: {datos_factura['cae']}")
    c.drawString(30*mm, y - 5*mm, f"Vto. CAE: {cae_vto_fmt}")

    # QR
    qr_url = generar_qr_afip(datos_factura)
    qr_img = qrcode.make(qr_url)
    qr_buffer = io.BytesIO()
    qr_img.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)

    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(qr_buffer), width - 60*mm, y - 30*mm, 35*mm, 35*mm)

    c.save()

    if isinstance(output_path, io.BytesIO):
        output_path.seek(0)
        return output_path

    return output_path
