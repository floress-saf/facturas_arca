"""
Módulo de Facturación Electrónica - WSFEv1 (ARCA/AFIP)
Homologación (testing)

Servicios utilizados:
- WSAA: Autenticación (obtener Token y Sign)
- WSFEv1: Facturación Electrónica versión 1
"""
import os
import base64
import datetime
import logging
import requests
from lxml import etree
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7 as pkcs7_mod
from cryptography import x509

log = logging.getLogger(__name__)

# URLs de Homologación (testing)
WSAA_URL = "https://wsaahomo.afip.gov.ar/ws/services/LoginCms"
WSFE_URL = "https://wswhomo.afip.gov.ar/wsfev1/service.asmx"

# Cache del token
_token_cache = {"token": None, "sign": None, "expira": None}


def _crear_tra(servicio="wsfe"):
    """Crea el Ticket de Requerimiento de Acceso (TRA) XML."""
    ahora = datetime.datetime.now(datetime.timezone.utc)
    tra = etree.Element("loginTicketRequest", version="1.0")
    header = etree.SubElement(tra, "header")
    etree.SubElement(header, "uniqueId").text = str(int(ahora.timestamp()))
    etree.SubElement(header, "generationTime").text = (ahora - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S-00:00")
    etree.SubElement(header, "expirationTime").text = (ahora + datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S-00:00")
    etree.SubElement(tra, "service").text = servicio
    return etree.tostring(tra, xml_declaration=True, encoding="UTF-8")


def _firmar_tra(tra_xml, cert_path, key_path):
    """Firma el TRA con PKCS#7."""
    with open(key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())

    opciones = [pkcs7_mod.PKCS7Options.Binary, pkcs7_mod.PKCS7Options.NoAttributes]
    firmado = (
        pkcs7_mod.PKCS7SignatureBuilder()
        .set_data(tra_xml)
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, opciones)
    )
    return base64.b64encode(firmado).decode("utf-8")


def _login_wsaa(cert_path, key_path):
    """Obtiene Token y Sign del WSAA para wsfe."""
    tra_xml = _crear_tra("wsfe")
    cms = _firmar_tra(tra_xml, cert_path, key_path)

    soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                      xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov">
    <soapenv:Body>
        <wsaa:loginCms>
            <wsaa:in0>{cms}</wsaa:in0>
        </wsaa:loginCms>
    </soapenv:Body>
    </soapenv:Envelope>"""

    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""}

    # Reintentos (el servidor de homologación es inestable)
    import time
    max_intentos = 3
    for intento in range(max_intentos):
        try:
            resp = requests.post(WSAA_URL, data=soap_body.encode("utf-8"), headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            if intento < max_intentos - 1:
                log.warning(f"WSAA intento {intento + 1} falló: {e}. Reintentando en 3s...")
                time.sleep(3)
            else:
                raise Exception(f"WSAA no responde después de {max_intentos} intentos: {e}")

    tree = etree.fromstring(resp.content)
    login_resp = tree.find(".//{http://wsaa.view.sua.dvadac.desein.afip.gov}loginCmsReturn")
    if login_resp is None:
        raise Exception(f"No se pudo obtener loginCmsReturn: {resp.text[:300]}")

    ta_xml = etree.fromstring(login_resp.text.encode("utf-8"))
    token = ta_xml.find(".//token").text
    sign = ta_xml.find(".//sign").text
    expira = ta_xml.find(".//expirationTime").text
    return token, sign, expira


def obtener_credenciales():
    """Obtiene token/sign, usando cache si no expiró."""
    global _token_cache

    cert_path = os.getenv("AFIP_CERT", "certs/certificado.crt")
    key_path = os.getenv("AFIP_KEY", "certs/privada.key")

    if _token_cache["token"] and _token_cache["expira"]:
        try:
            expira = datetime.datetime.fromisoformat(_token_cache["expira"].replace("Z", "+00:00"))
            if datetime.datetime.now(datetime.timezone.utc) < expira - datetime.timedelta(minutes=2):
                return _token_cache["token"], _token_cache["sign"]
        except Exception:
            pass

    token, sign, expira = _login_wsaa(cert_path, key_path)
    _token_cache = {"token": token, "sign": sign, "expira": expira}
    return token, sign


def _soap_call(method, body_content):
    """Ejecuta una llamada SOAP al WSFEv1."""
    cuit = int(os.getenv("AFIP_CUIT", "20237241275"))
    token, sign = obtener_credenciales()

    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                      xmlns:ar="http://ar.gov.afip.dif.FEV1/">
    <soapenv:Body>
        <ar:{method}>
            <ar:Auth>
                <ar:Token>{token}</ar:Token>
                <ar:Sign>{sign}</ar:Sign>
                <ar:Cuit>{cuit}</ar:Cuit>
            </ar:Auth>
            {body_content}
        </ar:{method}>
    </soapenv:Body>
    </soapenv:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f"http://ar.gov.afip.dif.FEV1/{method}"
    }

    # Reintentos
    import time
    max_intentos = 3
    for intento in range(max_intentos):
        try:
            resp = requests.post(WSFE_URL, data=soap.encode("utf-8"), headers=headers, timeout=30)
            resp.raise_for_status()
            return etree.fromstring(resp.content)
        except requests.exceptions.RequestException as e:
            if intento < max_intentos - 1:
                log.warning(f"WSFEv1 intento {intento + 1} falló: {e}. Reintentando en 3s...")
                time.sleep(3)
            else:
                raise Exception(f"WSFEv1 no responde después de {max_intentos} intentos: {e}")


def fe_comp_ultimo_autorizado(pto_vta, tipo_cmp):
    """Obtiene el último número de comprobante autorizado."""
    body = f"""
        <ar:PtoVta>{pto_vta}</ar:PtoVta>
        <ar:CbteTipo>{tipo_cmp}</ar:CbteTipo>
    """
    tree = _soap_call("FECompUltimoAutorizado", body)
    nro = tree.find(".//{http://ar.gov.afip.dif.FEV1/}CbteNro")
    if nro is not None:
        return int(nro.text)
    # Buscar errores
    err = tree.find(".//{http://ar.gov.afip.dif.FEV1/}Msg")
    raise Exception(f"Error al consultar último comprobante: {err.text if err is not None else 'desconocido'}")


def fe_cae_solicitar(datos):
    """
    Solicita CAE para un comprobante.
    
    datos = {
        "tipo_cmp": 1,        # Tipo de comprobante
        "pto_vta": 1,         # Punto de venta
        "nro_cmp": 1,         # Número de comprobante
        "fecha": "20260528",  # Fecha YYYYMMDD
        "concepto": 1,        # 1=Productos, 2=Servicios, 3=Ambos
        "doc_tipo": 80,       # 80=CUIT, 96=DNI, 99=Consumidor Final
        "doc_nro": 30639371251,
        "importe_total": 1000.00,
        "importe_neto": 826.45,
        "importe_iva": 173.55,
        "importe_exento": 0,
        "importe_no_gravado": 0,
        "moneda_id": "PES",
        "moneda_cotiz": 1,
        "iva_items": [        # Lista de alícuotas de IVA
            {"id": 5, "base_imp": 826.45, "importe": 173.55}  # 5=21%
        ]
    }
    """
    # Construir XML de IVA
    iva_xml = ""
    if datos.get("iva_items"):
        iva_xml = "<ar:Iva>"
        for item in datos["iva_items"]:
            iva_xml += f"""
                <ar:AlicIva>
                    <ar:Id>{item['id']}</ar:Id>
                    <ar:BaseImp>{item['base_imp']:.2f}</ar:BaseImp>
                    <ar:Importe>{item['importe']:.2f}</ar:Importe>
                </ar:AlicIva>"""
        iva_xml += "</ar:Iva>"

    # Fechas de servicio (solo para concepto 2 o 3)
    fechas_servicio = ""
    if datos.get("concepto", 1) in (2, 3):
        fechas_servicio = f"""
            <ar:FchServDesde>{datos.get('fch_serv_desde', datos['fecha'])}</ar:FchServDesde>
            <ar:FchServHasta>{datos.get('fch_serv_hasta', datos['fecha'])}</ar:FchServHasta>
            <ar:FchVtoPago>{datos.get('fch_vto_pago', datos['fecha'])}</ar:FchVtoPago>
        """

    body = f"""
        <ar:FeCAEReq>
            <ar:FeCabReq>
                <ar:CantReg>1</ar:CantReg>
                <ar:PtoVta>{datos['pto_vta']}</ar:PtoVta>
                <ar:CbteTipo>{datos['tipo_cmp']}</ar:CbteTipo>
            </ar:FeCabReq>
            <ar:FeDetReq>
                <ar:FECAEDetRequest>
                    <ar:Concepto>{datos.get('concepto', 1)}</ar:Concepto>
                    <ar:DocTipo>{datos.get('doc_tipo', 80)}</ar:DocTipo>
                    <ar:DocNro>{datos['doc_nro']}</ar:DocNro>
                    <ar:CbteDesde>{datos['nro_cmp']}</ar:CbteDesde>
                    <ar:CbteHasta>{datos['nro_cmp']}</ar:CbteHasta>
                    <ar:CbteFch>{datos['fecha']}</ar:CbteFch>
                    <ar:ImpTotal>{datos['importe_total']:.2f}</ar:ImpTotal>
                    <ar:ImpTotConc>{datos.get('importe_no_gravado', 0):.2f}</ar:ImpTotConc>
                    <ar:ImpNeto>{datos['importe_neto']:.2f}</ar:ImpNeto>
                    <ar:ImpOpEx>{datos.get('importe_exento', 0):.2f}</ar:ImpOpEx>
                    <ar:ImpIVA>{datos.get('importe_iva', 0):.2f}</ar:ImpIVA>
                    <ar:ImpTrib>0.00</ar:ImpTrib>
                    <ar:MonId>{datos.get('moneda_id', 'PES')}</ar:MonId>
                    <ar:MonCotiz>{datos.get('moneda_cotiz', 1)}</ar:MonCotiz>
                    {fechas_servicio}
                    {iva_xml}
                    <ar:CondicionIVAReceptorId>{datos.get('condicion_iva_receptor_id', 5)}</ar:CondicionIVAReceptorId>
                </ar:FECAEDetRequest>
            </ar:FeDetReq>
        </ar:FeCAEReq>
    """

    tree = _soap_call("FECAESolicitar", body)
    ns = "{http://ar.gov.afip.dif.FEV1/}"

    # Parsear respuesta
    resultado = tree.find(f".//{ns}Resultado")
    cae = tree.find(f".//{ns}CAE")
    cae_vto = tree.find(f".//{ns}CAEFchVto")

    if resultado is not None and resultado.text == "A" and cae is not None:
        return {
            "ok": True,
            "cae": cae.text,
            "cae_vto": cae_vto.text if cae_vto is not None else "",
            "resultado": "A"
        }

    # Buscar errores/observaciones
    errores = []
    for obs in tree.findall(f".//{ns}Obs"):
        msg = obs.find(f"{ns}Msg")
        if msg is not None:
            errores.append(msg.text)
    for err in tree.findall(f".//{ns}Err"):
        msg = err.find(f"{ns}Msg")
        if msg is not None:
            errores.append(msg.text)

    return {
        "ok": False,
        "cae": "",
        "cae_vto": "",
        "resultado": resultado.text if resultado is not None else "R",
        "errores": errores or ["Error desconocido al solicitar CAE"]
    }


def fe_param_get_tipos_cbte():
    """Obtiene los tipos de comprobante habilitados."""
    tree = _soap_call("FEParamGetTiposCbte", "")
    ns = "{http://ar.gov.afip.dif.FEV1/}"
    tipos = []
    for cbte in tree.findall(f".//{ns}CbteTipo"):
        id_elem = cbte.find(f"{ns}Id")
        desc_elem = cbte.find(f"{ns}Desc")
        if id_elem is not None and desc_elem is not None:
            tipos.append({"id": int(id_elem.text), "desc": desc_elem.text})
    return tipos


def fe_param_get_tipos_iva():
    """Obtiene las alícuotas de IVA."""
    tree = _soap_call("FEParamGetTiposIva", "")
    ns = "{http://ar.gov.afip.dif.FEV1/}"
    tipos = []
    for iva in tree.findall(f".//{ns}IvaTipo"):
        id_elem = iva.find(f"{ns}Id")
        desc_elem = iva.find(f"{ns}Desc")
        if id_elem is not None and desc_elem is not None:
            tipos.append({"id": int(id_elem.text), "desc": desc_elem.text})
    return tipos
