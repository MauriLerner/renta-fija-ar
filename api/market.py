import json
import os
import time
from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Cache simple en memoria (dura mientras la función esté "caliente" en Vercel)
# Para persistencia real entre invocaciones se usaría KV storage, pero
# para precios de cierre esto es suficiente — Vercel reutiliza instancias.
_cache = {}
CACHE_TTL = 60 * 60 * 6  # 6 horas (los precios de cierre no cambian en el día)

MAE_BASE = "https://api.mae.com.ar/MarketData/v1"
API_KEY = os.environ.get("MAE_API_KEY", "nuDX73vj2483KSUgvenkj9t50oA0vgvA4WcuRAER")

LECAP_PREFIXES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S0")
BONCAP_PREFIXES = ("T", "TT", "TY", "TX", "TZ")

ALLOWED_ORIGINS = ["*"]  # En producción podés restringir a tu dominio


def fetch_mae(endpoint: str) -> dict:
    """Llama a la API del MAE y devuelve el JSON."""
    url = f"{MAE_BASE}{endpoint}"
    req = Request(url, headers={"x-api-key": API_KEY})
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(f"MAE HTTP {e.code}: {e.reason}")
    except URLError as e:
        raise RuntimeError(f"MAE connection error: {e.reason}")


def get_cached(key: str, fetcher):
    """Devuelve datos del caché o los refresca si expiraron."""
    entry = _cache.get(key)
    now = time.time()
    if entry and (now - entry["ts"]) < CACHE_TTL:
        return entry["data"], entry["ts"]
    data = fetcher()
    _cache[key] = {"data": data, "ts": now}
    return data, now


def parse_titulos(raw_data) -> list:
    """Extrae y normaliza todos los títulos de todos los segmentos."""
    titulos = []
    if not isinstance(raw_data, dict):
        return titulos
    for seg in raw_data.get("segmento", []):
        seg_codigo = seg.get("segmentoCodigo", "")
        for t in seg.get("titulos", []):
            ticker = t.get("ticker", "")
            precio = t.get("precioCierreHoy", 0) or t.get("precioPromedioPonderado", 0)
            titulos.append({
                "ticker": ticker,
                "segmento": seg_codigo,
                "plazo": t.get("plazo", ""),
                "moneda": t.get("monedaCodigo", "$"),
                "precioCierre": t.get("precioCierreHoy", 0),
                "precioAyer": t.get("precioCierreAyer", 0),
                "precioProm": t.get("precioPromedioPonderado", 0),
                "precioUltimo": t.get("precioUltimo", 0),
                "variacion": t.get("variacion", 0),
                "precioMin": t.get("precioMinimo", 0),
                "cantidad": t.get("cantidad", 0),
                "monto": t.get("monto", 0),
                "fecha": t.get("fecha", ""),
            })
    return titulos


def classify_instrument(ticker: str) -> str:
    """Clasifica el instrumento según el ticker."""
    t = ticker.upper().split("/")[0]
    # LECAPs: S + número (ej S17A6, S29Y6)
    if len(t) >= 4 and t[0] == "S" and t[1].isdigit():
        return "LECAP"
    # BONCAPs: T + número o TT (ej TTD26, TTJ26, T30A7)
    if t.startswith("TT") or (t[0] == "T" and len(t) >= 4 and t[1].isdigit()):
        return "BONCAP"
    # Bonos CER: TZX, TZXM, etc.
    if t.startswith("TZX") or t.startswith("TZXM"):
        return "CER"
    # Bonos dólar linked: TX
    if t.startswith("TX") and not t.startswith("TZX"):
        return "DOLLAR_LINKED"
    # Soberanos USD: GD, AL
    if t.startswith("GD") or t.startswith("AL"):
        return "SOBERANO_USD"
    return "OTRO"


def get_resumen_final(fecha: str = None) -> dict:
    """Trae el reporte resumen final. Si no hay fecha, usa hoy."""
    from datetime import date, timedelta
    if not fecha:
        # Intentar con hoy; si no hay datos, el frontend puede pedir fecha específica
        fecha = date.today().strftime("%Y-%m-%d")

    cache_key = f"resumen_{fecha}"

    def fetcher():
        return fetch_mae(f"/mercado/boletin/ReporteResumenFinal?fecha={fecha}")

    data, cached_at = get_cached(cache_key, fetcher)
    titulos = parse_titulos(data)

    # Agregar clasificación y filtrar solo pesos (para LECAPs/BONCAPs)
    result = []
    for t in titulos:
        instrumento = classify_instrument(t["ticker"].split("/")[0])
        t["instrumento"] = instrumento
        result.append(t)

    return {
        "fecha": fecha,
        "cached_at": int(cached_at),
        "total": len(result),
        "titulos": result,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _handle(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        fecha = params.get("fecha", [None])[0]

        try:
            if not API_KEY:
                raise RuntimeError("MAE_API_KEY no configurada en variables de entorno")

            result = get_resumen_final(fecha)
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")

            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=21600")  # 6hs en CDN
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            error = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(500)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error)

    def log_message(self, format, *args):
        pass  # Silencia logs en Vercel
