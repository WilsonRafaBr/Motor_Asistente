#!/usr/bin/env python3
"""
ASISTENTE - Motor de Productividad
Pilares: Score Urgencia · Buffer 15min · Límite 6h · Multi-select contexto · UUID fix
+ Módulo de Repaso Espaciado (BD Maestra de Estudio → Task Hub → Email)
"""

import json, logging, os, smtplib, sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

def _fix_enc():
    for n in ("stdout","stderr"):
        s = getattr(sys,n,None)
        if s and hasattr(s,"reconfigure"):
            try: s.reconfigure(encoding="utf-8")
            except: pass
_fix_enc()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
print("OK Variables cargadas" if os.getenv("NOTION_API_KEY") else "ERROR: Variables no encontradas")

# ── Zona horaria ──────────────────────────────────────────────────────────────
def get_tz(name:str):
    try: return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name=="America/Guayaquil": return timezone(timedelta(hours=-5),name=name)
        return timezone.utc

# ── Colores calendario ────────────────────────────────────────────────────────
CAT_COLOR = {"🧠 Estudio":"9","💪 Gym":"2","🇩🇪 Alemania":"5",
             "🎥 Divulgación":"6","🔬 Investigación":"3","🌊 Sandbox":"8"}

# ── Configuración ─────────────────────────────────────────────────────────────
class Config:
    GOOGLE_CREDENTIALS_JSON    = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    GOOGLE_CALENDAR_ID         = os.environ.get("GOOGLE_CALENDAR_ID")
    GOOGLE_CALENDAR_IDS        = os.environ.get("GOOGLE_CALENDAR_IDS")
    GOOGLE_OAUTH_CLIENT_ID     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    GOOGLE_OAUTH_TOKEN_URI     = os.environ.get("GOOGLE_OAUTH_TOKEN_URI","https://oauth2.googleapis.com/token")
    NOTION_API_KEY     = os.environ.get("NOTION_API_KEY")
    NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
    NOTION_VERSION     = os.environ.get("NOTION_VERSION","2025-09-03")
    TASK_HUB_URL       = os.environ.get("TASK_HUB_URL","https://www.notion.so/")
    SMTP_SERVER  = os.environ.get("SMTP_SERVER","smtp.gmail.com")
    SMTP_PORT    = int(os.environ.get("SMTP_PORT",587))
    EMAIL_FROM   = os.environ.get("EMAIL_FROM")
    EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
    EMAIL_TO     = os.environ.get("EMAIL_TO", os.environ.get("EMAIL_FROM"))
    TIMEZONE     = os.environ.get("TIMEZONE","America/Guayaquil")

    # BD Maestra de Estudio (Repaso Espaciado)
    REPASO_DB_ID = os.environ.get("REPASO_DB_ID", "b169fe4e77ed4c4ba2e452979efbbd1b")

    WORKDAY_START  = 6
    WORKDAY_END    = 22
    LUNCH_START    = 13
    LUNCH_END      = 14
    MIN_SESSION    = 45
    BUFFER_MINUTES = 15
    MAX_STUDY_HOURS = 6

    @classmethod
    def calendar_ids(cls) -> List[str]:
        raw = cls.GOOGLE_CALENDAR_ID or cls.GOOGLE_CALENDAR_IDS
        if raw:
            ids = [x.strip() for x in raw.split(",") if x.strip()]
            if ids: return ids
        return ["ALL"]

    @classmethod
    def validate(cls) -> bool:
        req = ["NOTION_API_KEY","NOTION_DATABASE_ID","EMAIL_FROM","EMAIL_PASSWORD","EMAIL_TO"]
        missing = [k for k in req if not getattr(cls,k)]
        has_oauth = all([cls.GOOGLE_OAUTH_CLIENT_ID, cls.GOOGLE_OAUTH_CLIENT_SECRET,
                         cls.GOOGLE_OAUTH_REFRESH_TOKEN, cls.GOOGLE_OAUTH_TOKEN_URI])
        if not has_oauth and not cls.GOOGLE_CREDENTIALS_JSON:
            missing.append("GOOGLE_AUTH")
        if missing:
            logger.error("Variables faltantes: %s", ", ".join(missing)); return False
        logger.info("Configuracion validada"); return True

# ── UUID helpers ──────────────────────────────────────────────────────────────
def _to_uuid(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return raw
    s = raw.strip()
    for sep in ("?","#"): s = s.split(sep)[0]
    if "/" in s: s = s.rstrip("/").split("/")[-1]
    s = s.replace("-","")
    hex_chars = "".join(c for c in s if c in "0123456789abcdefABCDEF")
    if len(hex_chars) < 32:
        return raw
    h = hex_chars[-32:]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

def _notion_error(r: requests.Response) -> str:
    try:
        d = r.json()
        return f"{d.get('code','')}: {d.get('message','')}" if d.get("code") else d.get("message", f"HTTP {r.status_code}")
    except: return f"HTTP {r.status_code}"

# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO DE REPASO ESPACIADO
# Lee la BD Maestra de Estudio, crea tareas urgentes en el Task Hub,
# y devuelve un bloque HTML para inyectar en el email diario.
# ══════════════════════════════════════════════════════════════════════════════

# Minutos estimados por nivel de dominio
_DURACION_REPASO = {"1": 30, "2": 25, "3": 20, "4": 15, "5": 10}

def _repaso_headers() -> dict:
    return {
        "Authorization": f"Bearer {Config.NOTION_API_KEY}",
        "Notion-Version": Config.NOTION_VERSION,
        "Content-Type": "application/json",
    }

def _resolve_repaso_ds() -> str:
    """Resuelve el data source ID de la BD Maestra de Estudio."""
    db_id = _to_uuid(Config.REPASO_DB_ID)
    h = _repaso_headers()
    r = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=h, timeout=20)
    if r.status_code == 200:
        sources = r.json().get("data_sources", [])
        if sources:
            ds = sources[0]["id"]
            logger.info("Repaso DS resuelto: %s", ds)
            return ds
        return db_id
    # fallback: intentar como data_source directo
    r2 = requests.get(f"https://api.notion.com/v1/data_sources/{db_id}", headers=h, timeout=20)
    if r2.status_code == 200:
        return db_id
    raise RuntimeError(f"No se pudo resolver BD Repaso: {_notion_error(r)}")

def _query_repaso_db(filtro: dict, ds_id: str) -> List[dict]:
    """Pagina completa sobre la BD Maestra con el filtro dado."""
    results = []
    cursor = None
    while True:
        body = {"page_size": 100, "filter": filtro}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/data_sources/{ds_id}/query",
            headers=_repaso_headers(), json=body, timeout=20,
        )
        if r.status_code >= 400:
            logger.error("Error consultando BD Repaso: %s", _notion_error(r))
            break
        data = r.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results

def _parse_repaso_page(page: dict) -> dict:
    props = page.get("properties", {})

    def sel(name):
        s = props.get(name, {}).get("select")
        return s["name"] if s else None

    def titulo():
        t = props.get("Tema", {}).get("title", [])
        return t[0]["plain_text"] if t else "Sin título"

    def formula_date():
        f = props.get("Próximo Repaso", {}).get("formula", {})
        d = f.get("date", {})
        return d.get("start") if isinstance(d, dict) else None

    return {
        "id": page["id"],
        "tema": titulo(),
        "materia": sel("Materia"),
        "semestre": sel("Semestre"),
        "nivel": sel("Nivel de Dominio"),
        "proximo_repaso": formula_date(),
    }

def _tarea_repaso_existe(hub_id: str, nombre: str, hoy_str: str) -> bool:
    """Evita duplicados: busca tarea con mismo nombre y fecha de hoy."""
    r = requests.post(
        f"https://api.notion.com/v1/databases/{_to_uuid(hub_id)}/query",
        headers=_repaso_headers(),
        json={
            "filter": {
                "and": [
                    {"property": "Name", "title": {"equals": nombre}},
                    {"property": "Due Date", "date": {"equals": hoy_str}},
                ]
            },
            "page_size": 1,
        },
        timeout=20,
    )
    if r.status_code >= 400:
        return False
    return len(r.json().get("results", [])) > 0

def _crear_tarea_repaso(hub_id: str, tema: dict, hoy_str: str, urgente: bool) -> bool:
    nombre = f"📖 Repasar: {tema['tema']}"
    if _tarea_repaso_existe(hub_id, nombre, hoy_str):
        logger.info("Repaso ya existe hoy: %s", tema['tema'])
        return False

    duracion = _DURACION_REPASO.get(tema["nivel"] or "3", 20)
    prioridad = "Alta" if urgente else "Media"
    contexto = f"[{tema['semestre']}] {tema['materia'] or ''} · Dominio {tema['nivel'] or '?'}/5"

    body = {
        "parent": {"database_id": _to_uuid(hub_id)},
        "properties": {
            "Name":             {"title": [{"text": {"content": nombre}}]},
            "Status":           {"status": {"name": "To Do"}},
            "Priority":         {"select": {"name": prioridad}},
            "Category":         {"select": {"name": "🧠 Estudio"}},
            "Due Date":         {"date": {"start": hoy_str}},
            "📍 Contexto":      {"multi_select": [{"name": "Casa"}]},
            "⏱️ Duración (min)": {"number": duracion},
            "✅ Sincronizada":   {"checkbox": False},
        },
    }
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=_repaso_headers(), json=body, timeout=20,
    )
    if r.status_code >= 400:
        logger.error("Error creando tarea repaso '%s': %s", nombre, _notion_error(r))
        return False
    logger.info("Tarea repaso creada: %s [%s]", nombre, prioridad)
    return True

def ejecutar_modulo_repaso(hub_id: str) -> str:
    """
    Punto de entrada del módulo. Devuelve un bloque HTML listo para
    inyectar en el email. Crea tareas urgentes en el Task Hub.
    """
    hoy = datetime.now(get_tz(Config.TIMEZONE)).date()
    hoy_str = hoy.isoformat()
    logger.info("=== Módulo Repaso Espaciado · %s ===", hoy_str)

    ds_id = _resolve_repaso_ds()

    # Urgentes: 4° semestre con Próximo Repaso <= hoy
    urgentes_raw = _query_repaso_db({
        "and": [
            {"property": "Semestre", "select": {"equals": "4°"}},
            {"property": "Próximo Repaso", "formula": {"date": {"on_or_before": hoy_str}}},
        ]
    }, ds_id)

    # Mantenimiento: semestres anteriores, dominio bajo (1 o 2), vencidos
    mant_raw = _query_repaso_db({
        "and": [
            {"property": "Semestre", "select": {"does_not_equal": "4°"}},
            {"property": "Próximo Repaso", "formula": {"date": {"on_or_before": hoy_str}}},
            {"or": [
                {"property": "Nivel de Dominio", "select": {"equals": "1"}},
                {"property": "Nivel de Dominio", "select": {"equals": "2"}},
            ]},
        ]
    }, ds_id)

    urgentes   = [_parse_repaso_page(p) for p in urgentes_raw]
    mantenimiento = [_parse_repaso_page(p) for p in mant_raw]

    logger.info("Urgentes: %d | Mantenimiento: %d", len(urgentes), len(mantenimiento))

    # Crear tareas urgentes en el Task Hub (mantenimiento solo va al email)
    for t in urgentes:
        _crear_tarea_repaso(hub_id, t, hoy_str, urgente=True)

    # Construir bloque HTML para el email
    return _build_repaso_html(urgentes, mantenimiento, hoy_str)

def _build_repaso_html(urgentes: List[dict], mantenimiento: List[dict], hoy_str: str) -> str:
    if not urgentes and not mantenimiento:
        return """
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:20px;padding:20px 24px;margin-bottom:18px;">
          <p style="margin:0;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#16a34a;">🧠 Repaso Espaciado</p>
          <p style="margin:6px 0 0;font-size:15px;color:#166534;">Sin temas vencidos hoy. ¡Buen trabajo! 🎉</p>
        </div>"""

    def filas(temas):
        out = ""
        for t in temas:
            nivel = t["nivel"] or "?"
            bar_color = {"1":"#ef4444","2":"#f97316","3":"#eab308","4":"#22c55e","5":"#3b82f6"}.get(nivel, "#94a3b8")
            out += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;">
                <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{bar_color};margin-right:8px;"></span>
                <strong style="color:#0f172a;">{t['tema']}</strong>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;color:#64748b;font-size:13px;">{t['materia'] or '—'}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;text-align:center;">
                <span style="background:{bar_color};color:#fff;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700;">{nivel}/5</span>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;color:#94a3b8;font-size:12px;">{t['semestre'] or ''}</td>
            </tr>"""
        return out

    sec_urgentes = ""
    if urgentes:
        sec_urgentes = f"""
        <p style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#dc2626;margin:16px 0 6px;">
          🔴 Prioritario — 4° Ciclo ({len(urgentes)} tema{'s' if len(urgentes)>1 else ''})
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <thead><tr style="background:#fef2f2;">
            <th style="padding:8px 12px;text-align:left;color:#7f1d1d;font-size:11px;">Tema</th>
            <th style="padding:8px 12px;text-align:left;color:#7f1d1d;font-size:11px;">Materia</th>
            <th style="padding:8px 12px;text-align:center;color:#7f1d1d;font-size:11px;">Dominio</th>
            <th style="padding:8px 12px;color:#7f1d1d;font-size:11px;">Ciclo</th>
          </tr></thead>
          <tbody>{filas(urgentes)}</tbody>
        </table>"""

    sec_mant = ""
    if mantenimiento:
        sec_mant = f"""
        <p style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#d97706;margin:16px 0 6px;">
          🟡 Mantenimiento — Ciclos anteriores ({len(mantenimiento)} tema{'s' if len(mantenimiento)>1 else ''})
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <thead><tr style="background:#fffbeb;">
            <th style="padding:8px 12px;text-align:left;color:#78350f;font-size:11px;">Tema</th>
            <th style="padding:8px 12px;text-align:left;color:#78350f;font-size:11px;">Materia</th>
            <th style="padding:8px 12px;text-align:center;color:#78350f;font-size:11px;">Dominio</th>
            <th style="padding:8px 12px;color:#78350f;font-size:11px;">Ciclo</th>
          </tr></thead>
          <tbody>{filas(mantenimiento)}</tbody>
        </table>"""

    total = len(urgentes) + len(mantenimiento)
    return f"""
    <div style="background:#fff;border:1px solid #cbd5e1;border-radius:24px;padding:22px;
                box-shadow:0 12px 30px rgba(15,23,42,.05);margin-bottom:18px;">
      <p style="margin:0 0 4px;font-size:11px;font-weight:700;text-transform:uppercase;
                letter-spacing:1.2px;color:#64748b;">🧠 Repaso Espaciado · {hoy_str}</p>
      <p style="margin:0 0 12px;font-size:16px;font-weight:700;color:#0f172a;">
        {total} tema{'s' if total>1 else ''} para repasar hoy
        {'· ' + str(len(urgentes)) + ' tarea' + ('s' if len(urgentes)>1 else '') + ' creada' + ('s' if len(urgentes)>1 else '') + ' en Task Hub' if urgentes else ''}
      </p>
      {sec_urgentes}
      {sec_mant}
    </div>"""

# ══════════════════════════════════════════════════════════════════════════════
# FIN MÓDULO REPASO
# ══════════════════════════════════════════════════════════════════════════════

# ── Google Calendar ───────────────────────────────────────────────────────────
class CalendarClient:
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self):
        cfg = Config
        creds = None
        mode  = None

        if cfg.GOOGLE_CREDENTIALS_JSON:
            try:
                creds = Credentials.from_service_account_info(
                    json.loads(cfg.GOOGLE_CREDENTIALS_JSON), scopes=self.SCOPES)
                mode = "service_account"
                logger.info("Google Calendar: usando service account")
            except Exception as e:
                logger.warning("Service account falló, intentando OAuth: %s", e)
                creds = None

        if creds is None and all([cfg.GOOGLE_OAUTH_CLIENT_ID, cfg.GOOGLE_OAUTH_CLIENT_SECRET,
                                   cfg.GOOGLE_OAUTH_REFRESH_TOKEN, cfg.GOOGLE_OAUTH_TOKEN_URI]):
            try:
                creds = UserCredentials(
                    token=None, refresh_token=cfg.GOOGLE_OAUTH_REFRESH_TOKEN,
                    token_uri=cfg.GOOGLE_OAUTH_TOKEN_URI, client_id=cfg.GOOGLE_OAUTH_CLIENT_ID,
                    client_secret=cfg.GOOGLE_OAUTH_CLIENT_SECRET, scopes=self.SCOPES)
                creds.refresh(GoogleAuthRequest())
                mode = "oauth_user"
                logger.info("Google Calendar: usando OAuth")
            except Exception as e:
                logger.error("OAuth también falló: %s", e)
                creds = None

        if creds is None:
            raise RuntimeError(
                "No se pudo autenticar Google Calendar. "
                "Verifica GOOGLE_CREDENTIALS_JSON o regenera GOOGLE_OAUTH_REFRESH_TOKEN."
            )

        self._svc = build("calendar","v3",credentials=creds)
        logger.info("Google Calendar autenticado (%s)", mode)

    def list_calendars(self) -> Dict[str,Dict]:
        items, token = [], None
        while True:
            r = self._svc.calendarList().list(pageToken=token).execute()
            items.extend(r.get("items",[]))
            token = r.get("nextPageToken")
            if not token: break
        return {c["id"]:c for c in items}

    def get_write_calendar_id(self) -> str:
        selected = Config.calendar_ids()
        if selected != ["ALL"]:
            return selected[0]
        avail = self.list_calendars()
        if avail:
            return next(iter(avail.keys()))
        raise RuntimeError("La cuenta de Google no tiene calendarios visibles.")

    def get_events(self, cal_ids: List[str], hours:int=48) -> List[Dict]:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours)
        avail = self.list_calendars()
        selected = list(avail.keys()) if cal_ids==["ALL"] else cal_ids
        raw=[]
        for cid in selected:
            token=None
            while True:
                try:
                    res = self._svc.events().list(
                        calendarId=cid, timeMin=now.isoformat(), timeMax=horizon.isoformat(),
                        singleEvents=True, orderBy="startTime", pageToken=token,
                        fields="items(id,summary,start,end),nextPageToken").execute()
                except Exception as e:
                    logger.warning("Error calendario %s: %s", cid, e); break
                for ev in res.get("items",[]):
                    raw.append(self._norm(ev,cid))
                token = res.get("nextPageToken")
                if not token: break
        deduped = self._dedup(raw)
        logger.info("%d eventos en proximas %dh", len(deduped), hours)
        return deduped

    @staticmethod
    def _norm(ev:Dict, cid:str) -> Dict:
        def dt(v:str)->datetime:
            if "T" in v: return datetime.fromisoformat(v.replace("Z","+00:00"))
            return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
        s=ev["start"].get("dateTime",ev["start"].get("date"))
        e=ev["end"].get("dateTime",ev["end"].get("date"))
        sd,ed=dt(s),dt(e)
        return {"id":ev["id"],"cal_id":cid,"summary":ev.get("summary","Sin titulo"),
                "start":sd,"end":ed,"duration_min":int((ed-sd).total_seconds()/60),
                "all_day":"date" in ev["start"] and "dateTime" not in ev["start"]}

    @staticmethod
    def _dedup(evs:List[Dict]) -> List[Dict]:
        seen={}
        for ev in evs:
            k=(ev["summary"].lower().strip(),ev["start"].isoformat(),ev["end"].isoformat())
            if k not in seen: seen[k]=ev
        return sorted(seen.values(),key=lambda x:x["start"])

    def create_event(self, title:str, start_iso:str, end_iso:str,
                     desc:str="", color_id:Optional[str]=None, cal_id:Optional[str]=None) -> Optional[Dict]:
        tz_name=Config.TIMEZONE
        ltz=get_tz(tz_name)
        target_cal_id = cal_id or self.get_write_calendar_id()
        def loc(v:str)->str:
            dt=datetime.fromisoformat(v.replace("Z","+00:00"))
            dt=dt.replace(tzinfo=ltz) if dt.tzinfo is None else dt.astimezone(ltz)
            return dt.isoformat()
        body={"summary":title,"description":desc,
              "start":{"dateTime":loc(start_iso),"timeZone":tz_name},
              "end":{"dateTime":loc(end_iso),"timeZone":tz_name}}
        if color_id: body["colorId"]=color_id
        try:
            c=self._svc.events().insert(calendarId=target_cal_id,body=body).execute()
            logger.info("Evento creado: %s en calendario %s",c.get("id"),target_cal_id); return c
        except Exception as e:
            logger.error("Error creando evento en calendario %s: %s",target_cal_id,e); return None

# ── Notion Client ─────────────────────────────────────────────────────────────
class NotionClient:
    BASE="https://api.notion.com/v1"

    def __init__(self):
        self._h={"Authorization":f"Bearer {Config.NOTION_API_KEY}",
                 "Notion-Version":Config.NOTION_VERSION,"Content-Type":"application/json"}
        logger.info("Notion API v%s", Config.NOTION_VERSION)

    def _req(self,method:str,path:str,**kw)->requests.Response:
        return requests.request(method,f"{self.BASE}/{path}",headers=self._h,timeout=20,**kw)

    def _resolve_ds(self, db_id:str) -> str:
        nid = _to_uuid(db_id)
        r=self._req("GET",f"databases/{nid}")
        if r.status_code==200:
            sources=r.json().get("data_sources",[])
            if sources:
                ds=sources[0]["id"]; logger.info("Data source: %s",ds); return ds
            return nid
        r2=self._req("GET",f"data_sources/{nid}")
        if r2.status_code==200: return nid
        raise RuntimeError(f"No se pudo resolver DB/DS. DB:{_notion_error(r)} DS:{_notion_error(r2)}")

    @staticmethod
    def _formula_num(prop:Dict) -> Optional[float]:
        t=prop.get("type")
        if t=="formula":
            f=prop.get("formula") or {}; return f.get("number")
        if t=="number": return prop.get("number")
        return None

    @staticmethod
    def _plain(items:List[Dict]) -> str:
        return "".join(i.get("plain_text","") for i in items).strip()

    def get_pending_tasks(self, db_id:str) -> List[Dict]:
        ds_id=self._resolve_ds(db_id)
        payload={"page_size":100,"filter":{"property":"Status","status":{"does_not_equal":"Done"}}}
        pages=[]; cursor=None
        while True:
            body=dict(payload)
            if cursor: body["start_cursor"]=cursor
            r=self._req("POST",f"data_sources/{ds_id}/query",json=body)
            if r.status_code>=400: raise RuntimeError(f"Error Notion: {_notion_error(r)}")
            data=r.json()
            for page in data.get("results",[]):
                t=self._parse(page)
                if t: pages.append(t)
            if not data.get("has_more"): break
            cursor=data.get("next_cursor")

        pages.sort(key=lambda t:t["score_urgencia"],reverse=True)
        logger.info("%d tareas pendientes (orden por Score Urgencia):", len(pages))
        for t in pages:
            logger.info("  [score=%.0f] %s | sesion=%smin | restantes=%smin | due=%s",
                        t["score_urgencia"],t["title"],
                        t.get("session_duration"),t.get("tiempo_a_agendar"),t.get("due_date"))
        return pages

    def _parse(self, page:Dict) -> Optional[Dict]:
        props=page.get("properties",{})
        title   = self._plain(props.get("Name",{}).get("title",[])) or "Sin titulo"
        status  = (props.get("Status",{}).get("status") or {}).get("name","Not started")
        priority= (props.get("Priority",{}).get("select") or {}).get("name","Media")
        category= (props.get("Category",{}).get("select") or {}).get("name","General")
        due_date= (props.get("Due date",{}).get("date") or {}).get("start")

        ctx_items = props.get("📍 Contexto",{}).get("multi_select") or []
        contextos: List[str] = [i["name"] for i in ctx_items if i.get("name")]

        dur_prop=props.get("⏱️ Duración (min)",{})
        duracion_min:Optional[int]=None
        if dur_prop.get("type")=="number" and dur_prop.get("number") is not None:
            duracion_min=int(dur_prop["number"])

        val_r=self._formula_num(props.get("⏳ Minutos Restantes",{}))
        minutos_restantes:Optional[int]=max(int(val_r),0) if val_r is not None else None

        val_tb=self._formula_num(props.get("🔢 Total Bloques",{}))
        total_bloques:Optional[int]=int(val_tb) if val_tb and val_tb>0 else None

        bc_prop=props.get("🧩 Bloques Completados",{})
        bloques_completados:int=0
        if bc_prop.get("type")=="number" and bc_prop.get("number") is not None:
            bloques_completados=int(bc_prop["number"])

        val_s=self._formula_num(props.get("🎯 Score Urgencia",{}))
        score_urgencia:float=float(val_s) if val_s is not None else 0.0

        if duracion_min and total_bloques and total_bloques>0:
            session_duration=duracion_min//total_bloques
        elif duracion_min:
            session_duration=duracion_min
        else:
            session_duration=None

        tiempo_a_agendar=minutos_restantes if minutos_restantes is not None else duracion_min
        if not tiempo_a_agendar or tiempo_a_agendar<=0:
            logger.debug("Descartada (sin tiempo): %s",title); return None

        return {"id":page["id"],"title":title,"status":status,"priority":priority,
                "category":category,"contextos":contextos,"due_date":due_date,
                "duracion_min":duracion_min,"minutos_restantes":minutos_restantes,
                "total_bloques":total_bloques,"bloques_completados":bloques_completados,
                "session_duration":session_duration,"tiempo_a_agendar":tiempo_a_agendar,
                "score_urgencia":score_urgencia,
                "es_repaso": title.startswith("📖 Repasar:")}

    def mark_in_progress(self, page_id:str) -> bool:
        uid=_to_uuid(page_id)
        r=self._req("PATCH",f"pages/{uid}",
                    json={"properties":{"Status":{"status":{"name":"In progress"}}}})
        if r.status_code>=400:
            logger.error("Error Status→InProgress %s (%s): %s",page_id[:8],uid,_notion_error(r))
            return False
        logger.info("Status→In progress: %s",uid); return True

    def update_bloques(self, page_id:str, bloques:int) -> bool:
        uid=_to_uuid(page_id)
        r=self._req("PATCH",f"pages/{uid}",
                    json={"properties":{"🧩 Bloques Completados":{"number":bloques}}})
        if r.status_code>=400:
            logger.error("Error bloques %s (%s): %s",page_id[:8],uid,_notion_error(r))
            return False
        return True

# ── Huecos libres ─────────────────────────────────────────────────────────────
CAMPUS_KW={"clase","class","facultad","universidad","hospital",
           "rotacion","rotation","lab","laboratorio","seminario","curso"}

def _is_campus(ev:Dict)->bool:
    return any(k in ev["summary"].lower() for k in CAMPUS_KW)

def _merge(ivs:List[Dict])->List[Dict]:
    if not ivs: return []
    ordered=sorted(ivs,key=lambda x:x["start"])
    merged=[ordered[0].copy()]
    for iv in ordered[1:]:
        cur=merged[-1]
        if iv["start"]<=cur["end"]: cur["end"]=max(cur["end"],iv["end"])
        else: merged.append(iv.copy())
    return merged

def _infer_ctx(s,e,campus)->str:
    prev=next((c for c in reversed(campus) if c["end_local"]<=s),None)
    nxt =next((c for c in campus if c["start_local"]>=e),None)
    if prev and nxt and int((nxt["start_local"]-prev["end_local"]).total_seconds()/60)<=240:
        return "Facultad"
    if prev and not nxt: return "Casa"
    if nxt and not prev: return "Casa"
    return "flexible"

def find_free_slots(events:List[Dict], tz_name:str) -> List[Dict]:
    tz=get_tz(tz_name); now=datetime.now(tz); today=now.date()
    day_s=now.replace(hour=Config.WORKDAY_START,minute=0,second=0,microsecond=0)
    day_e=now.replace(hour=Config.WORKDAY_END,  minute=0,second=0,microsecond=0)
    if now>day_s: day_s=now.replace(second=0,microsecond=0)

    today_ev=[]
    for ev in events:
        s=ev["start"].astimezone(tz); e=ev["end"].astimezone(tz)
        if s.date()!=today and e.date()!=today: continue
        if e<=day_s or s>=day_e: continue
        today_ev.append({**ev,"start_local":max(s,day_s),"end_local":min(e,day_e)})
    today_ev.sort(key=lambda x:x["start_local"])
    campus=[e for e in today_ev if _is_campus(e)]

    occupied=[{"start":e["start_local"],"end":e["end_local"]} for e in today_ev]
    lunch_s=now.replace(hour=Config.LUNCH_START,minute=0,second=0,microsecond=0)
    lunch_e=now.replace(hour=Config.LUNCH_END,  minute=0,second=0,microsecond=0)
    occupied.append({"start":lunch_s,"end":lunch_e})

    transporte_slots:List[Dict]=[]
    if campus:
        first,last=campus[0],campus[-1]

        t_pre_s=max(day_s, first["start_local"]-timedelta(hours=1))
        t_pre_e=first["start_local"]
        dur_pre=int((t_pre_e-t_pre_s).total_seconds()/60)
        if dur_pre>0:
            transporte_slots.append({
                "start":t_pre_s,"end":t_pre_e,"duration_min":dur_pre,
                "label":f"{t_pre_s.strftime('%H:%M')} - {t_pre_e.strftime('%H:%M')} [Transporte]",
                "context":"Transporte",
            })
            occupied.append({"start":t_pre_s,"end":t_pre_e})

        t_post_s=last["end_local"]
        t_post_e=min(day_e, last["end_local"]+timedelta(hours=1))
        dur_post=int((t_post_e-t_post_s).total_seconds()/60)
        if dur_post>0:
            transporte_slots.append({
                "start":t_post_s,"end":t_post_e,"duration_min":dur_post,
                "label":f"{t_post_s.strftime('%H:%M')} - {t_post_e.strftime('%H:%M')} [Transporte]",
                "context":"Transporte",
            })
            occupied.append({"start":t_post_s,"end":t_post_e})

    merged=_merge([iv for iv in occupied if iv["end"]>day_s and iv["start"]<day_e])
    regular_slots:List[Dict]=[]; cursor=day_s
    for iv in merged:
        if iv["start"]>cursor:
            dur=int((iv["start"]-cursor).total_seconds()/60)
            if dur>=Config.MIN_SESSION:
                ctx=_infer_ctx(cursor,iv["start"],campus)
                regular_slots.append({
                    "start":cursor,"end":iv["start"],"duration_min":dur,
                    "label":f"{cursor.strftime('%H:%M')} - {iv['start'].strftime('%H:%M')}",
                    "context":ctx,
                })
        cursor=max(cursor,iv["end"])
    if cursor<day_e:
        dur=int((day_e-cursor).total_seconds()/60)
        if dur>=Config.MIN_SESSION:
            ctx=_infer_ctx(cursor,day_e,campus)
            regular_slots.append({
                "start":cursor,"end":day_e,"duration_min":dur,
                "label":f"{cursor.strftime('%H:%M')} - {day_e.strftime('%H:%M')}",
                "context":ctx,
            })

    all_slots=sorted(regular_slots+transporte_slots, key=lambda x:x["start"])

    logger.info("Slots generados:")
    for sl in all_slots:
        logger.info("  %s (%dmin) [%s]",sl["label"],sl["duration_min"],sl["context"])
    return all_slots


def _ctx_ok(task_contextos:List[str], slot_ctx:str) -> bool:
    slot_norm = slot_ctx.lower()
    task_norms = [tc.lower() for tc in task_contextos]
    tiene_transporte = any("transporte" in tc for tc in task_norms)
    tiene_casa       = any("casa"       in tc for tc in task_norms)
    tiene_facultad   = any("facultad"   in tc for tc in task_norms)
    sin_contexto     = not task_contextos

    if slot_norm == "transporte":
        return tiene_transporte
    if sin_contexto:
        return True
    if slot_norm == "casa":
        return tiene_casa
    if slot_norm == "facultad":
        return tiene_facultad
    if slot_norm == "flexible":
        return tiene_casa or tiene_facultad
    return True

# ── Scheduler ─────────────────────────────────────────────────────────────────
class Scheduler:
    def assign(self, tasks:List[Dict], free_slots:List[Dict]) -> Tuple[List[Dict],List[Dict]]:
        slots=[{"start":s["start"],"end":s["end"],"duration_min":s["duration_min"],
                "label":s["label"],"context":s["context"]} for s in free_slots]

        scheduled:List[Dict]=[]
        unscheduled:List[Dict]=[]
        total_study_min=0
        max_study_min=Config.MAX_STUDY_HOURS*60
        cursor_ultimo:Optional[datetime]=None

        pendientes_segunda:List[Dict]=[]
        for task in tasks:
            if total_study_min>=max_study_min:
                logger.info("Limite %.0fh alcanzado: %s",Config.MAX_STUDY_HOURS,task["title"])
                unscheduled.append(task); continue

            sessions=self._fit(task,slots,cursor_ultimo,max_study_min-total_study_min)
            if sessions:
                scheduled.extend(sessions)
                total_study_min+=sum(s["duration_min"] for s in sessions)
                cursor_ultimo=sessions[-1]["end"]
                logger.info("  Agendada [fase1]: %s → %d sesiones",task["title"],len(sessions))
            else:
                pendientes_segunda.append(task)
                logger.info("  Pendiente [fase2]: %s (sesion=%smin)",task["title"],task.get("session_duration"))

        for task in pendientes_segunda:
            if total_study_min>=max_study_min:
                unscheduled.append(task); continue

            sessions=self._fit(task,slots,cursor_ultimo,max_study_min-total_study_min)
            if sessions:
                scheduled.extend(sessions)
                total_study_min+=sum(s["duration_min"] for s in sessions)
                cursor_ultimo=sessions[-1]["end"]
                logger.info("  Agendada [fase2]: %s → %d sesiones",task["title"],len(sessions))
            else:
                unscheduled.append(task)
                logger.info("  Sin hueco: %s",task["title"])

        return scheduled, unscheduled

    def _fit(self, task:Dict, slots:List[Dict],
             cursor_ultimo:Optional[datetime], presupuesto:int) -> List[Dict]:
        session_dur=task.get("session_duration")
        if not session_dur:
            return []
        es_repaso=task.get("es_repaso", False)
        if not es_repaso and session_dur<Config.MIN_SESSION:
            logger.info("Micro-bloque omitido (%dmin): %s",session_dur,task["title"])
            return []

        tiempo_restante=min(task["tiempo_a_agendar"],presupuesto)
        bloque_num=task["bloques_completados"]
        sessions:List[Dict]=[]

        for slot in slots:
            if tiempo_restante<=0:
                break
            if not _ctx_ok(task.get("contextos",[]),slot["context"]):
                continue

            slot_start=slot["start"]
            if cursor_ultimo is not None:
                earliest=cursor_ultimo+timedelta(minutes=Config.BUFFER_MINUTES)
                if earliest>=slot["end"]:
                    continue
                if earliest>slot_start:
                    slot_start=earliest

            slot_avail=int((slot["end"]-slot_start).total_seconds()/60)

            if slot_avail<session_dur:
                continue

            import math
            n_caben=math.floor((slot_avail+Config.BUFFER_MINUTES)/
                               (session_dur+Config.BUFFER_MINUTES))
            n_caben=max(n_caben,1)
            n_necesarias=math.ceil(tiempo_restante/session_dur)
            n_usar=min(n_caben,n_necesarias)

            cursor=slot_start
            bloques_en_slot=0
            sess_end=None
            for i in range(n_usar):
                if tiempo_restante<=0:
                    break
                dur=min(session_dur,tiempo_restante)
                if not es_repaso and dur<Config.MIN_SESSION:
                    break
                sess_end=cursor+timedelta(minutes=dur)
                bloque_num+=1
                sessions.append({
                    "task_id":task["id"],"task_title":task["title"],
                    "category":task["category"],"priority":task["priority"],
                    "contextos":task.get("contextos",[]),"score":task["score_urgencia"],
                    "start":cursor,"end":sess_end,"duration_min":dur,
                    "label":f"{cursor.strftime('%H:%M')} - {sess_end.strftime('%H:%M')}",
                    "bloque_numero":bloque_num,"total_bloques":task["total_bloques"],
                    "bloques_completados_finales":bloque_num,
                })
                bloques_en_slot+=1
                tiempo_restante-=dur
                cursor=sess_end
                if i<n_usar-1 and tiempo_restante>0:
                    cursor=cursor+timedelta(minutes=Config.BUFFER_MINUTES)

            if bloques_en_slot>0 and sess_end:
                slot["start"]=cursor
                slot["duration_min"]=max(0,int((slot["end"]-cursor).total_seconds()/60))
                cursor_ultimo=sess_end

        return sessions

# ── Email ─────────────────────────────────────────────────────────────────────
class EmailBuilder:
    @staticmethod
    def build(scheduled:List[Dict], unscheduled:List[Dict],
              all_events:List[Dict], timestamp:datetime,
              tz_name:str, hub_url:str,
              repaso_html:str="") -> str:          # ← nuevo parámetro
        tz=get_tz(tz_name)
        local_ts=timestamp.astimezone(tz).strftime("%d/%m/%Y %H:%M")
        today=datetime.now(tz).date(); tomorrow=today+timedelta(days=1)
        today_ev=[e for e in all_events if e["start"].astimezone(tz).date()==today]
        tom_ev  =[e for e in all_events if e["start"].astimezone(tz).date()==tomorrow]

        grouped:Dict[str,List[Dict]]={}
        for s in scheduled: grouped.setdefault(s["task_title"],[]).append(s)

        cards=""
        for title,sess in list(grouped.items())[:6]:
            f=sess[0]
            pc={"Alta":"#ef4444","Media":"#f97316","Baja":"#64748b"}.get(f["priority"],"#2563eb")
            nums=[str(s["bloque_numero"]) for s in sess] if f["total_bloques"] else []
            bl_str=f"Bloque(s) {', '.join(nums)}/{f['total_bloques']}" if nums else ""
            ctx_str=", ".join(f["contextos"]) if f.get("contextos") else "Flexible"
            labels=" | ".join(s["label"] for s in sess)
            total_min=sum(s["duration_min"] for s in sess)
            cards+=f"""<tr><td style="padding-bottom:14px;">
              <div style="background:#f8fbff;border:1px solid #dbeafe;border-radius:18px;padding:18px 22px;">
                <div style="font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:{pc};margin-bottom:6px;">{f['priority']} · {f['category']} · {ctx_str}</div>
                <div style="font-size:17px;font-weight:700;color:#0f172a;margin-bottom:6px;">{title}</div>
                <div style="font-size:14px;color:#334155;margin-bottom:4px;">{labels} ({total_min} min)</div>
                {f'<div style="font-size:13px;color:#64748b;">{bl_str}</div>' if bl_str else ""}
              </div></td></tr>"""

        unsch=""
        for t in unscheduled[:5]:
            ctx_str=", ".join(t.get("contextos",[])) or "Flexible"
            unsch+=f"""<tr><td style="padding-bottom:10px;">
              <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;padding:12px 18px;">
                <div style="font-size:14px;font-weight:600;color:#7c2d12;">{t['title']}</div>
                <div style="font-size:12px;color:#9a3412;">Sin hueco · Score {t['score_urgencia']:.0f} · Sesión {t.get('session_duration','?')}min · {ctx_str}</div>
              </div></td></tr>"""

        def ev_rows(evs,cols=3):
            rows=""
            for e in evs[:8]:
                s=e["start"].astimezone(tz).strftime("%H:%M")
                en=e["end"].astimezone(tz).strftime("%H:%M")
                rows+=f"""<tr>
                  <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;"><strong>{s}</strong>→<strong>{en}</strong></td>
                  <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;">{e['summary']}</td>
                  <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;text-align:center;">{e['duration_min']}min</td>
                </tr>"""
            return rows or f'<tr><td colspan="{cols}" style="padding:14px;text-align:center;color:#94a3b8;">Sin eventos.</td></tr>'

        total_min=sum(s["duration_min"] for s in scheduled)
        return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
     line-height:1.6;color:#0f172a;background:linear-gradient(180deg,#eaf4ff 0%,#f8fafc 48%,#f4f0ff 100%);margin:0;padding:0;}}
.w{{max-width:720px;margin:0 auto;padding:24px 16px 40px;}}
.hero{{background:radial-gradient(circle at top left,#cfe7ff 0%,#fff 38%,#f8fafc 100%);
      border:1px solid #bfdbfe;border-radius:28px;padding:32px 28px;
      box-shadow:0 18px 40px rgba(15,23,42,.08);margin-bottom:18px;}}
.panel{{background:rgba(255,255,255,.96);border:1px solid #cbd5e1;border-radius:24px;
        padding:22px;box-shadow:0 12px 30px rgba(15,23,42,.05);margin-bottom:18px;}}
.sec{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:#64748b;margin:0 0 12px;}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th{{background:#f8fafc;text-align:left;padding:10px 12px;color:#334155;border-bottom:1px solid #e2e8f0;}}
.mc{{background:#fff;border:1px solid #cbd5e1;border-radius:16px;padding:16px;}}
.mv{{font-size:24px;font-weight:700;color:#0f172a;margin-bottom:4px;}}
.ml{{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;}}
</style></head><body><div class="w">
  <div class="hero">
    <div style="display:inline-block;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;
                color:#2563eb;background:#eff6ff;border:1px solid #bfdbfe;border-radius:999px;
                padding:5px 10px;margin-bottom:16px;">Asistente diario</div>
    <h1 style="font-size:32px;line-height:1.1;letter-spacing:-1px;margin:0 0 8px;color:#0f172a;">Plan del dia listo.</h1>
    <p style="margin:0;font-size:14px;color:#475569;">Score Urgencia · Buffer {Config.BUFFER_MINUTES}min · Max {Config.MAX_STUDY_HOURS}h · {local_ts}</p>
    <table role="presentation" style="margin-top:22px;"><tr>
      <td style="width:33%;padding-right:8px;vertical-align:top;"><div class="mc"><div class="mv">{len(grouped)}</div><div class="ml">Tareas agendadas</div></div></td>
      <td style="width:33%;padding:0 4px;vertical-align:top;"><div class="mc"><div class="mv">{total_min}</div><div class="ml">Min netos</div></div></td>
      <td style="width:33%;padding-left:8px;vertical-align:top;"><div class="mc"><div class="mv">{len(unscheduled)}</div><div class="ml">Sin hueco hoy</div></div></td>
    </tr></table>
  </div>
  {repaso_html}
  <div class="panel"><p class="sec">Sesiones del dia</p>
    <table role="presentation">{cards if cards else '<tr><td><div style="padding:20px;color:#64748b;text-align:center;">Sin sesiones.</div></td></tr>'}</table>
  </div>
  {'<div class="panel"><p class="sec">Tareas sin hueco hoy</p><table role="presentation">'+unsch+'</table></div>' if unsch else ""}
  <div class="panel"><p class="sec">Agenda de hoy</p>
    <table><thead><tr><th>Hora</th><th>Evento</th><th>Dur.</th></tr></thead>
    <tbody>{ev_rows(today_ev)}</tbody></table>
  </div>
  <div class="panel"><p class="sec">Agenda de manana</p>
    <table><thead><tr><th>Hora</th><th>Evento</th><th>Dur.</th></tr></thead>
    <tbody>{ev_rows(tom_ev)}</tbody></table>
  </div>
  <div style="text-align:center;margin-top:20px;">
    <a href="{hub_url}" style="display:inline-block;background:#0f172a;color:#fff;text-decoration:none;
                               font-weight:700;font-size:14px;padding:13px 22px;border-radius:14px;">Abrir Task Hub</a>
  </div>
  <div style="text-align:center;color:#94a3b8;font-size:12px;padding-top:12px;">
    Asistente · {len(scheduled)} sesiones · {len(all_events)} eventos
  </div>
</div></body></html>"""

# ── Email Sender ──────────────────────────────────────────────────────────────
class EmailSender:
    @staticmethod
    def send(subject:str,html:str)->bool:
        try:
            msg=MIMEMultipart("alternative")
            msg["Subject"]=subject; msg["From"]=Config.EMAIL_FROM; msg["To"]=Config.EMAIL_TO
            msg.attach(MIMEText(html,"html","utf-8"))
            with smtplib.SMTP(Config.SMTP_SERVER,Config.SMTP_PORT) as srv:
                srv.starttls(); srv.login(Config.EMAIL_FROM,Config.EMAIL_PASSWORD)
                srv.send_message(msg)
            logger.info("Email enviado a %s",Config.EMAIL_TO); return True
        except Exception as e:
            logger.error("Error email: %s",e); return False

# ── Orquestador ───────────────────────────────────────────────────────────────
class Asistente:
    def run(self)->int:
        print("\n"+"="*70+"\nASISTENTE - Motor de Productividad")
        print(f"Ejecucion: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"+"="*70+"\n")
        if not Config.validate(): return 1

        # 1. Calendario
        cal=CalendarClient()
        events=cal.get_events(Config.calendar_ids(),hours=48)

        # 2. Módulo Repaso Espaciado
        #    Crea tareas urgentes en el Task Hub ANTES de leer las tareas,
        #    para que el scheduler las vea y las incluya en el plan del día.
        repaso_html = ""
        try:
            repaso_html = ejecutar_modulo_repaso(Config.NOTION_DATABASE_ID)
            logger.info("Módulo Repaso OK")
        except Exception as e:
            logger.error("Módulo Repaso falló (no crítico): %s", e)
            repaso_html = ""

        # 3. Tareas Task Hub (ya incluye las de repaso recién creadas)
        notion=NotionClient()
        tasks=notion.get_pending_tasks(Config.NOTION_DATABASE_ID)

        # 4. Huecos
        free_slots=find_free_slots(events,Config.TIMEZONE)
        logger.info("%d huecos libres:",len(free_slots))
        for sl in free_slots:
            logger.info("  %s (%dmin) [%s]",sl["label"],sl["duration_min"],sl["context"])

        # 5. Asignación
        scheduler=Scheduler()
        scheduled,unscheduled=scheduler.assign(tasks,free_slots)
        logger.info("%d sesiones / %d sin hueco",len(scheduled),len(unscheduled))

        # 6. Crear eventos Google Calendar
        agendadas:Dict[str,int]={}
        for sess in scheduled:
            bl=f" ({sess['bloque_numero']}/{sess['total_bloques']})" if sess["total_bloques"] else ""
            title=f"[Asistente] {sess['task_title']}{bl}"
            ctx_str=", ".join(sess.get("contextos",[])) or "-"
            desc=(f"Categoria: {sess['category']}\nPrioridad: {sess['priority']}\n"
                  f"Contexto: {ctx_str}\nScore: {sess['score']:.0f}\nDuracion: {sess['duration_min']} min")
            ok=cal.create_event(title,sess["start"].isoformat(),
                                sess["end"].isoformat(),desc,
                                color_id=CAT_COLOR.get(sess["category"]))
            if ok:
                prev=agendadas.get(sess["task_id"],0)
                agendadas[sess["task_id"]]=max(prev,sess["bloques_completados_finales"])
        logger.info("%d eventos creados",len(agendadas))

        # 7. Sincronizar Notion
        for task_id,bloque_final in agendadas.items():
            notion.mark_in_progress(task_id)
            notion.update_bloques(task_id,bloque_final)

        # 8. Email (con bloque de repaso inyectado)
        now=datetime.now(timezone.utc)
        html=EmailBuilder.build(scheduled,unscheduled,events,now,
                                Config.TIMEZONE,Config.TASK_HUB_URL,
                                repaso_html=repaso_html)
        subj=(f"Asistente · {now.astimezone(get_tz(Config.TIMEZONE)).strftime('%d/%m/%Y')} · "
              f"{len(agendadas)} tareas")
        if not EmailSender.send(subj,html): return 1

        print(f"\n{'='*70}\nASISTENTE OK · {len(agendadas)} agendadas · {len(unscheduled)} sin hueco\n{'='*70}\n")
        return 0

if __name__=="__main__":
    sys.exit(Asistente().run())
