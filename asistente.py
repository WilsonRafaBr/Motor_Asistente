#!/usr/bin/env python3
"""
ASISTENTE - Motor de Productividad
===================================
Arquitectura simplificada basada en 6 pilares:
1. Autoridad de Tiempos: Notion fórmulas son la fuente de verdad (solo lectura).
2. Jerarquía por Score Urgencia: ordena y asigna huecos por score desc.
3. Sin micro-bloques: duración mínima de sesión = duración_total / total_bloques.
4. Eliminación de página Motor de Sugerencias: resultado final = calendario.
5. Planificación integral: todas las tareas pendientes, no solo In progress.
6. Sincronización de Status: marca In progress solo lo que se agendó hoy.
"""

import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


# ---------------------------------------------------------------------------
# Consola y logging
# ---------------------------------------------------------------------------

def _fix_encoding():
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass


_fix_encoding()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

if os.getenv("NOTION_API_KEY"):
    print("OK Variables de entorno cargadas")
else:
    print("ERROR: No se encuentran las variables de entorno")


# ---------------------------------------------------------------------------
# Zona horaria
# ---------------------------------------------------------------------------

def get_tz(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "America/Guayaquil":
            return timezone(timedelta(hours=-5), name="America/Guayaquil")
        return timezone.utc


# ---------------------------------------------------------------------------
# Colores Google Calendar por categoría
# ---------------------------------------------------------------------------

CATEGORY_COLOR_MAP: Dict[str, str] = {
    "🧠 Estudio":       "9",
    "💪 Gym":           "2",
    "🇩🇪 Alemania":    "5",
    "🎥 Divulgación":   "6",
    "🔬 Investigación": "3",
    "🌊 Sandbox":       "8",
}


# ---------------------------------------------------------------------------
# Configuración centralizada
# ---------------------------------------------------------------------------

class Config:
    GOOGLE_CREDENTIALS_JSON    = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    GOOGLE_CALENDAR_IDS        = os.environ.get("GOOGLE_CALENDAR_IDS")
    GOOGLE_OAUTH_CLIENT_ID     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    GOOGLE_OAUTH_TOKEN_URI     = os.environ.get("GOOGLE_OAUTH_TOKEN_URI",
                                                 "https://oauth2.googleapis.com/token")

    NOTION_API_KEY     = os.environ.get("NOTION_API_KEY")
    NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
    NOTION_VERSION     = os.environ.get("NOTION_VERSION", "2025-09-03")
    TASK_HUB_URL       = os.environ.get("TASK_HUB_URL", "https://www.notion.so/")

    SMTP_SERVER    = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT      = int(os.environ.get("SMTP_PORT", 587))
    EMAIL_FROM     = os.environ.get("EMAIL_FROM")
    EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
    EMAIL_TO       = os.environ.get("EMAIL_TO", os.environ.get("EMAIL_FROM"))

    TIMEZONE = os.environ.get("TIMEZONE", "America/Guayaquil")

    # Planificación
    WORKDAY_START_HOUR  = 6
    WORKDAY_END_HOUR    = 22
    LUNCH_START_HOUR    = 13
    LUNCH_END_HOUR      = 14
    MIN_SESSION_MINUTES = 45   # Pilar 3: umbral mínimo por sesión
    MAX_FOCUS_MINUTES   = 120  # tope de foco continuo (no se usa para bloquear, solo para info)

    @classmethod
    def calendar_ids(cls) -> List[str]:
        raw = cls.GOOGLE_CALENDAR_IDS
        if raw:
            ids = [x.strip() for x in raw.split(",") if x.strip()]
            if ids:
                return ids
        return ["ALL"]

    @classmethod
    def validate(cls) -> bool:
        required = ["NOTION_API_KEY", "NOTION_DATABASE_ID",
                    "EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO"]
        missing = [k for k in required if not getattr(cls, k)]
        has_oauth = all([cls.GOOGLE_OAUTH_CLIENT_ID, cls.GOOGLE_OAUTH_CLIENT_SECRET,
                         cls.GOOGLE_OAUTH_REFRESH_TOKEN, cls.GOOGLE_OAUTH_TOKEN_URI])
        has_sa = bool(cls.GOOGLE_CREDENTIALS_JSON)
        if not has_oauth and not has_sa:
            missing.append("GOOGLE_AUTH (OAuth o Service Account)")
        if missing:
            logger.error("Variables faltantes: %s", ", ".join(missing))
            return False
        logger.info("Configuracion validada")
        return True


# ---------------------------------------------------------------------------
# Helpers Notion
# ---------------------------------------------------------------------------

def _normalize_notion_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return raw
    s = raw.strip()
    for sep in ("?", "#"):
        s = s.split(sep)[0]
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    if "-" in s and len(s) > 32:
        s = s.split("-")[-1]
    return s.replace("-", "")


def _notion_error(resp: requests.Response) -> str:
    try:
        d = resp.json()
        return f"{d.get('code','')}: {d.get('message','')}" if d.get("code") else d.get("message", f"HTTP {resp.status_code}")
    except ValueError:
        return resp.text.strip() or f"HTTP {resp.status_code}"


# ---------------------------------------------------------------------------
# Google Calendar Client
# ---------------------------------------------------------------------------

class CalendarClient:
    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self):
        cfg = Config
        try:
            if all([cfg.GOOGLE_OAUTH_CLIENT_ID, cfg.GOOGLE_OAUTH_CLIENT_SECRET,
                    cfg.GOOGLE_OAUTH_REFRESH_TOKEN, cfg.GOOGLE_OAUTH_TOKEN_URI]):
                creds = UserCredentials(
                    token=None,
                    refresh_token=cfg.GOOGLE_OAUTH_REFRESH_TOKEN,
                    token_uri=cfg.GOOGLE_OAUTH_TOKEN_URI,
                    client_id=cfg.GOOGLE_OAUTH_CLIENT_ID,
                    client_secret=cfg.GOOGLE_OAUTH_CLIENT_SECRET,
                    scopes=self.SCOPES,
                )
                creds.refresh(GoogleAuthRequest())
                mode = "oauth_user"
            else:
                info = json.loads(cfg.GOOGLE_CREDENTIALS_JSON)
                creds = Credentials.from_service_account_info(info, scopes=self.SCOPES)
                mode = "service_account"
            self._svc = build("calendar", "v3", credentials=creds)
            logger.info("Google Calendar autenticado (%s)", mode)
        except Exception as exc:
            logger.error("Error autenticando Google Calendar: %s", exc)
            raise

    def list_calendars(self) -> Dict[str, Dict]:
        items, token = [], None
        while True:
            resp = self._svc.calendarList().list(pageToken=token).execute()
            items.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return {c["id"]: c for c in items}

    def get_events(self, calendar_ids: List[str], hours: int = 48) -> List[Dict]:
        now     = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours)
        avail   = self.list_calendars()
        selected = list(avail.keys()) if calendar_ids == ["ALL"] else calendar_ids

        raw: List[Dict] = []
        for cal_id in selected:
            token = None
            while True:
                try:
                    result = (
                        self._svc.events()
                        .list(
                            calendarId=cal_id,
                            timeMin=now.isoformat(),
                            timeMax=horizon.isoformat(),
                            singleEvents=True,
                            orderBy="startTime",
                            pageToken=token,
                            fields="items(id,summary,start,end),nextPageToken",
                        )
                        .execute()
                    )
                except Exception as exc:
                    logger.warning("Error leyendo calendario %s: %s", cal_id, exc)
                    break
                for ev in result.get("items", []):
                    raw.append(self._normalize(ev, cal_id))
                token = result.get("nextPageToken")
                if not token:
                    break

        deduped = self._dedupe(raw)
        logger.info("%d eventos en proximas %dh (%d calendarios)", len(deduped), hours, len(selected))
        return deduped

    @staticmethod
    def _normalize(ev: Dict, cal_id: str) -> Dict:
        def to_dt(val: str) -> datetime:
            if "T" in val:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            return datetime.fromisoformat(val).replace(tzinfo=timezone.utc)

        s = ev["start"].get("dateTime", ev["start"].get("date"))
        e = ev["end"].get("dateTime",   ev["end"].get("date"))
        start_dt = to_dt(s)
        end_dt   = to_dt(e)
        return {
            "id": ev["id"],
            "cal_id": cal_id,
            "summary": ev.get("summary", "Sin titulo"),
            "start": start_dt,
            "end":   end_dt,
            "duration_min": int((end_dt - start_dt).total_seconds() / 60),
            "all_day": "date" in ev["start"] and "dateTime" not in ev["start"],
        }

    @staticmethod
    def _dedupe(events: List[Dict]) -> List[Dict]:
        seen: Dict[tuple, Dict] = {}
        for ev in events:
            key = (ev["summary"].lower().strip(), ev["start"].isoformat(), ev["end"].isoformat())
            if key not in seen:
                seen[key] = ev
        return sorted(seen.values(), key=lambda x: x["start"])

    def create_event(
        self,
        title: str,
        start_iso: str,
        end_iso: str,
        description: str = "",
        color_id: Optional[str] = None,
        cal_id: str = "primary",
    ) -> Optional[Dict]:
        tz_name  = Config.TIMEZONE
        local_tz = get_tz(tz_name)

        def localize(val: str) -> str:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz)
            else:
                dt = dt.astimezone(local_tz)
            return dt.isoformat()

        body: Dict = {
            "summary": title,
            "description": description,
            "start": {"dateTime": localize(start_iso), "timeZone": tz_name},
            "end":   {"dateTime": localize(end_iso),   "timeZone": tz_name},
        }
        if color_id:
            body["colorId"] = color_id
        try:
            created = self._svc.events().insert(calendarId=cal_id, body=body).execute()
            logger.info("Evento creado: %s", created.get("id"))
            return created
        except Exception as exc:
            logger.error("Error creando evento: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Notion Client
# ---------------------------------------------------------------------------

class NotionClient:
    BASE = "https://api.notion.com/v1"

    def __init__(self):
        self._h = {
            "Authorization":  f"Bearer {Config.NOTION_API_KEY}",
            "Notion-Version": Config.NOTION_VERSION,
            "Content-Type":   "application/json",
        }
        logger.info("Notion API inicializado (v%s)", Config.NOTION_VERSION)

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        return requests.request(method, f"{self.BASE}/{path}",
                                headers=self._h, timeout=20, **kw)

    def _resolve_ds(self, db_id: str) -> str:
        nid = _normalize_notion_id(db_id)
        r = self._req("GET", f"databases/{nid}")
        if r.status_code == 200:
            sources = r.json().get("data_sources", [])
            if sources:
                ds = sources[0]["id"]
                logger.info("Data source: %s", ds)
                return ds
            return nid
        r2 = self._req("GET", f"data_sources/{nid}")
        if r2.status_code == 200:
            return nid
        raise RuntimeError(f"No se pudo resolver DB/DS. DB:{_notion_error(r)} DS:{_notion_error(r2)}")

    @staticmethod
    def _formula_number(prop: Dict) -> Optional[float]:
        t = prop.get("type")
        if t == "formula":
            f = prop.get("formula") or {}
            return f.get("number")
        if t == "number":
            return prop.get("number")
        return None

    @staticmethod
    def _plain(items: List[Dict]) -> str:
        return "".join(i.get("plain_text", "") for i in items).strip()

    def get_pending_tasks(self, db_id: str) -> List[Dict]:
        """
        Lee todas las tareas con Status != Done.
        Las fórmulas se leen directamente; nunca se recalculan (Pilar 1).
        Ordena por Score Urgencia descendente (Pilar 2).
        """
        ds_id = self._resolve_ds(db_id)
        payload = {
            "page_size": 100,
            "filter": {
                "property": "Status",
                "status": {"does_not_equal": "Done"},
            },
        }
        pages: List[Dict] = []
        cursor = None
        while True:
            body = dict(payload)
            if cursor:
                body["start_cursor"] = cursor
            r = self._req("POST", f"data_sources/{ds_id}/query", json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"Error Notion: {_notion_error(r)}")
            data = r.json()
            for page in data.get("results", []):
                task = self._parse(page)
                if task:
                    pages.append(task)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        # Pilar 2: Score Urgencia descendente
        pages.sort(key=lambda t: t["score_urgencia"], reverse=True)
        logger.info("%d tareas pendientes", len(pages))
        for t in pages:
            logger.info("  [%.0f] %s | sesion=%s min | restantes=%s min | status=%s",
                        t["score_urgencia"], t["title"],
                        t.get("session_duration"), t.get("tiempo_a_agendar"),
                        t["status"])
        return pages

    def _parse(self, page: Dict) -> Optional[Dict]:
        props = page.get("properties", {})

        title    = self._plain(props.get("Name", {}).get("title", [])) or "Sin titulo"
        status   = (props.get("Status", {}).get("status") or {}).get("name", "Not started")
        priority = (props.get("Priority", {}).get("select") or {}).get("name", "Media")
        category = (props.get("Category", {}).get("select") or {}).get("name", "General")
        contexto = (props.get("📍 Contexto", {}).get("select") or {}).get("name")
        due_date = (props.get("Due Date", {}).get("date") or {}).get("start")

        # ---- PILAR 1: campos de fórmula = solo lectura ----

        # Duración total (number, escribible)
        dur_prop = props.get("⏱️ Duración (min)", {})
        duracion_min: Optional[int] = None
        if dur_prop.get("type") == "number" and dur_prop.get("number") is not None:
            duracion_min = int(dur_prop["number"])

        # Minutos Restantes (fórmula, solo lectura)
        val_r = self._formula_number(props.get("⏳ Minutos Restantes", {}))
        minutos_restantes: Optional[int] = None
        if val_r is not None:
            minutos_restantes = max(int(val_r), 0)

        # Total Bloques (fórmula, solo lectura) — Pilar 1: formula.number
        val_tb = self._formula_number(props.get("🔢 Total Bloques", {}))
        total_bloques: Optional[int] = None
        if val_tb is not None and val_tb > 0:
            total_bloques = int(val_tb)

        # Bloques Completados (number, escribible)
        bc_prop = props.get("🧩 Bloques Completados", {})
        bloques_completados: int = 0
        if bc_prop.get("type") == "number" and bc_prop.get("number") is not None:
            bloques_completados = int(bc_prop["number"])

        # Score Urgencia (fórmula, solo lectura) — clave de priorización
        val_s = self._formula_number(props.get("🎯 Score Urgencia", {}))
        score_urgencia: float = float(val_s) if val_s is not None else 0.0

        # ---- Pilar 3: duración de sesión = duración_total / total_bloques ----
        if duracion_min is not None and total_bloques is not None and total_bloques > 0:
            session_duration = duracion_min // total_bloques
        elif duracion_min is not None:
            session_duration = duracion_min
        else:
            session_duration = None

        # Tiempo a agendar hoy
        tiempo_a_agendar = minutos_restantes if minutos_restantes is not None else duracion_min

        if not tiempo_a_agendar or tiempo_a_agendar <= 0:
            logger.debug("Descartada (sin tiempo): %s", title)
            return None

        return {
            "id":                  page["id"],
            "title":               title,
            "status":              status,
            "priority":            priority,
            "category":            category,
            "contexto":            contexto,
            "due_date":            due_date,
            "duracion_min":        duracion_min,
            "minutos_restantes":   minutos_restantes,
            "total_bloques":       total_bloques,
            "bloques_completados": bloques_completados,
            "session_duration":    session_duration,
            "tiempo_a_agendar":    tiempo_a_agendar,
            "score_urgencia":      score_urgencia,
        }

    # ---- escritura — solo campos permitidos (Pilar 1) ----

    def mark_in_progress(self, page_id: str) -> bool:
        r = self._req("PATCH", f"pages/{_normalize_notion_id(page_id)}",
                      json={"properties": {"Status": {"status": {"name": "In progress"}}}})
        if r.status_code >= 400:
            logger.error("Error Status→In progress %s: %s", page_id[:8], _notion_error(r))
            return False
        logger.info("Status→In progress: %s", page_id[:8])
        return True

    def update_bloques(self, page_id: str, bloques: int) -> bool:
        r = self._req("PATCH", f"pages/{_normalize_notion_id(page_id)}",
                      json={"properties": {"🧩 Bloques Completados": {"number": bloques}}})
        if r.status_code >= 400:
            logger.error("Error bloques %s: %s", page_id[:8], _notion_error(r))
            return False
        return True


# ---------------------------------------------------------------------------
# Huecos libres
# ---------------------------------------------------------------------------

CAMPUS_KW = {
    "clase", "class", "facultad", "universidad", "hospital",
    "rotacion", "rotation", "lab", "laboratorio", "seminario", "curso",
}


def _is_campus(ev: Dict) -> bool:
    return any(k in ev["summary"].lower() for k in CAMPUS_KW)


def _merge(intervals: List[Dict]) -> List[Dict]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda x: x["start"])
    merged  = [ordered[0].copy()]
    for iv in ordered[1:]:
        cur = merged[-1]
        if iv["start"] <= cur["end"]:
            cur["end"] = max(cur["end"], iv["end"])
        else:
            merged.append(iv.copy())
    return merged


def _infer_ctx(slot_start, slot_end, campus) -> str:
    prev = next((e for e in reversed(campus) if e["end_local"] <= slot_start), None)
    nxt  = next((e for e in campus if e["start_local"] >= slot_end), None)
    if prev and nxt:
        gap = int((nxt["start_local"] - prev["end_local"]).total_seconds() / 60)
        if gap <= 240:
            return "facultad"
    if prev and not nxt:
        return "casa"
    if nxt and not prev:
        return "casa"
    return "flexible"


def find_free_slots(events: List[Dict], tz_name: str) -> List[Dict]:
    tz    = get_tz(tz_name)
    now   = datetime.now(tz)
    today = now.date()

    day_s = now.replace(hour=Config.WORKDAY_START_HOUR, minute=0, second=0, microsecond=0)
    day_e = now.replace(hour=Config.WORKDAY_END_HOUR,   minute=0, second=0, microsecond=0)
    if now > day_s:
        day_s = now.replace(second=0, microsecond=0)

    today_ev = []
    for ev in events:
        s = ev["start"].astimezone(tz)
        e = ev["end"].astimezone(tz)
        if s.date() != today and e.date() != today:
            continue
        if e <= day_s or s >= day_e:
            continue
        today_ev.append({**ev, "start_local": max(s, day_s), "end_local": min(e, day_e)})

    today_ev.sort(key=lambda x: x["start_local"])
    campus = [e for e in today_ev if _is_campus(e)]

    occupied = [{"start": e["start_local"], "end": e["end_local"]} for e in today_ev]

    lunch_s = now.replace(hour=Config.LUNCH_START_HOUR, minute=0, second=0, microsecond=0)
    lunch_e = now.replace(hour=Config.LUNCH_END_HOUR,   minute=0, second=0, microsecond=0)
    occupied.append({"start": lunch_s, "end": lunch_e})

    if campus:
        first, last = campus[0], campus[-1]
        occupied.append({"start": max(day_s, first["start_local"] - timedelta(hours=1)),
                         "end":   first["start_local"]})
        occupied.append({"start": last["end_local"],
                         "end":   min(day_e, last["end_local"] + timedelta(hours=1))})

    merged = _merge([iv for iv in occupied if iv["end"] > day_s and iv["start"] < day_e])

    slots, cursor = [], day_s
    for iv in merged:
        if iv["start"] > cursor:
            dur = int((iv["start"] - cursor).total_seconds() / 60)
            if dur >= Config.MIN_SESSION_MINUTES:
                ctx = _infer_ctx(cursor, iv["start"], campus)
                slots.append({
                    "start": cursor, "end": iv["start"],
                    "duration_min": dur,
                    "label": f"{cursor.strftime('%H:%M')} - {iv['start'].strftime('%H:%M')}",
                    "context": ctx,
                })
        cursor = max(cursor, iv["end"])

    if cursor < day_e:
        dur = int((day_e - cursor).total_seconds() / 60)
        if dur >= Config.MIN_SESSION_MINUTES:
            ctx = _infer_ctx(cursor, day_e, campus)
            slots.append({
                "start": cursor, "end": day_e,
                "duration_min": dur,
                "label": f"{cursor.strftime('%H:%M')} - {day_e.strftime('%H:%M')}",
                "context": ctx,
            })

    return slots


def _ctx_ok(task_ctx: Optional[str], slot_ctx: str) -> bool:
    if not task_ctx:
        return True
    tc = task_ctx.lower()
    if "casa" in tc or "home" in tc:
        return slot_ctx in {"casa", "flexible"}
    if any(k in tc for k in ("facultad", "campus", "universidad")):
        return slot_ctx in {"facultad", "flexible"}
    return True


# ---------------------------------------------------------------------------
# Scheduler — Pilares 2, 3 y 5
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Reglas de asignación:
    - Toma tareas en orden de score_urgencia desc (ya vienen ordenadas).
    - Para cada tarea, session_duration = duracion_min / total_bloques.
    - Si session_duration < MIN_SESSION_MINUTES → omite la tarea (micro-bloque).
    - Si una sesión no cabe en un hueco, pasa al siguiente hueco.
    - Si no cabe en ningún hueco hoy, la tarea va a unscheduled.
    - Si una tarea tiene más tiempo que el día: agenda solo lo que cabe hoy.
    - Pilar 2: si la tarea de mayor score no cabe en un hueco, prueba las siguientes
      (el hueco no se desperdicia).
    """

    def assign(self, tasks: List[Dict], free_slots: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        # Slots como lista mutable con capacidad restante
        slots = [{"start": s["start"], "end": s["end"],
                  "duration_min": s["duration_min"],
                  "label": s["label"], "context": s["context"]}
                 for s in free_slots]

        scheduled:   List[Dict] = []
        unscheduled: List[Dict] = []

        for task in tasks:
            sessions = self._fit(task, slots)
            if sessions:
                scheduled.extend(sessions)
            else:
                unscheduled.append(task)

        return scheduled, unscheduled

    def _fit(self, task: Dict, slots: List[Dict]) -> List[Dict]:
        session_dur = task.get("session_duration")
        if not session_dur:
            logger.info("Sin session_duration: %s", task["title"])
            return []

        # Pilar 3: micro-bloque → omitir
        if session_dur < Config.MIN_SESSION_MINUTES:
            logger.info("Micro-bloque omitido (%d min < %d): %s",
                        session_dur, Config.MIN_SESSION_MINUTES, task["title"])
            return []

        tiempo_restante   = task["tiempo_a_agendar"]
        bloques_ya_hechos = task["bloques_completados"]
        sessions: List[Dict] = []
        bloque_num = bloques_ya_hechos  # se irá incrementando

        for slot in slots:
            if tiempo_restante <= 0:
                break
            if not _ctx_ok(task.get("contexto"), slot["context"]):
                continue
            if slot["duration_min"] < session_dur:
                # No cabe ni una sesión aquí; pero no bloqueamos el slot para otras tareas
                continue

            # Cuántas sesiones completas caben en este slot
            n_caben  = slot["duration_min"] // session_dur
            n_necesarias = -(-tiempo_restante // session_dur)  # ceil
            n_usar   = min(n_caben, n_necesarias)

            cursor = slot["start"]
            for _ in range(n_usar):
                if tiempo_restante <= 0:
                    break
                dur = min(session_dur, tiempo_restante)
                if dur < Config.MIN_SESSION_MINUTES:
                    break

                sess_end = cursor + timedelta(minutes=dur)
                bloque_num += 1
                sessions.append({
                    "task_id":    task["id"],
                    "task_title": task["title"],
                    "category":   task["category"],
                    "priority":   task["priority"],
                    "contexto":   task.get("contexto"),
                    "score":      task["score_urgencia"],
                    "start":      cursor,
                    "end":        sess_end,
                    "duration_min": dur,
                    "label": f"{cursor.strftime('%H:%M')} - {sess_end.strftime('%H:%M')}",
                    "bloque_numero": bloque_num,
                    "total_bloques": task["total_bloques"],
                    "bloques_completados_finales": bloque_num,
                })
                cursor = sess_end
                tiempo_restante -= dur

            # Reducir capacidad del slot
            usados = n_usar * session_dur
            slot["start"]        = slot["start"] + timedelta(minutes=usados)
            slot["duration_min"] -= usados

        return sessions


# ---------------------------------------------------------------------------
# Email Builder
# ---------------------------------------------------------------------------

class EmailBuilder:
    @staticmethod
    def build(
        scheduled:   List[Dict],
        unscheduled: List[Dict],
        free_slots:  List[Dict],
        all_events:  List[Dict],
        timestamp:   datetime,
        tz_name:     str,
        hub_url:     str,
    ) -> str:
        tz       = get_tz(tz_name)
        local_ts = timestamp.astimezone(tz).strftime("%d/%m/%Y %H:%M")
        today    = datetime.now(tz).date()
        tomorrow = today + timedelta(days=1)

        today_ev    = [e for e in all_events if e["start"].astimezone(tz).date() == today]
        tomorrow_ev = [e for e in all_events if e["start"].astimezone(tz).date() == tomorrow]

        # Agrupar sesiones por tarea
        grouped: Dict[str, List[Dict]] = {}
        for s in scheduled:
            grouped.setdefault(s["task_title"], []).append(s)

        # Cards de sesiones
        cards_html = ""
        for title, sessions in list(grouped.items())[:6]:
            f = sessions[0]
            pc = {"Alta": "#ef4444", "Media": "#f97316", "Baja": "#64748b"}.get(f["priority"], "#2563eb")
            nums = [str(s["bloque_numero"]) for s in sessions] if f["total_bloques"] else []
            bloque_str = f"Bloque(s) {', '.join(nums)}/{f['total_bloques']}" if nums else ""
            labels = " | ".join(s["label"] for s in sessions)
            total_min = sum(s["duration_min"] for s in sessions)
            cards_html += f"""
            <tr><td style="padding-bottom:14px;">
              <div style="background:#f8fbff;border:1px solid #dbeafe;border-radius:18px;padding:18px 22px;">
                <div style="font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:{pc};margin-bottom:8px;">{f['priority']} · {f['category']}</div>
                <div style="font-size:17px;font-weight:700;color:#0f172a;margin-bottom:6px;">{title}</div>
                <div style="font-size:14px;color:#334155;margin-bottom:4px;">{labels} ({total_min} min)</div>
                {f'<div style="font-size:13px;color:#64748b;">{bloque_str}</div>' if bloque_str else ""}
              </div>
            </td></tr>"""

        # Sin hueco
        unsch_html = ""
        for t in unscheduled[:5]:
            unsch_html += f"""
            <tr><td style="padding-bottom:10px;">
              <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;padding:12px 18px;">
                <div style="font-size:14px;font-weight:600;color:#7c2d12;">{t['title']}</div>
                <div style="font-size:12px;color:#9a3412;">Sin hueco · Score {t['score_urgencia']:.0f} · Sesion {t.get('session_duration','?')} min</div>
              </div>
            </td></tr>"""

        # Agenda hoy
        ag_hoy = ""
        for ev in today_ev[:8]:
            s = ev["start"].astimezone(tz).strftime("%H:%M")
            e = ev["end"].astimezone(tz).strftime("%H:%M")
            ag_hoy += f"""<tr>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;"><strong>{s}</strong> → <strong>{e}</strong></td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;">{ev['summary']}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;text-align:center;">{ev['duration_min']} min</td>
            </tr>"""

        # Agenda mañana
        ag_man = ""
        for ev in tomorrow_ev[:6]:
            s = ev["start"].astimezone(tz).strftime("%H:%M")
            e = ev["end"].astimezone(tz).strftime("%H:%M")
            ag_man += f"""<tr>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;"><strong>{s}</strong> → <strong>{e}</strong></td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;">{ev['summary']}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;text-align:center;">{ev['duration_min']} min</td>
            </tr>"""

        total_min_agendados = sum(s["duration_min"] for s in scheduled)

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.6;color:#0f172a;
     background:linear-gradient(180deg,#eaf4ff 0%,#f8fafc 48%,#f4f0ff 100%);margin:0;padding:0;}}
.wrap{{max-width:720px;margin:0 auto;padding:24px 16px 40px;}}
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
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div style="display:inline-block;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;
                color:#2563eb;background:#eff6ff;border:1px solid #bfdbfe;border-radius:999px;
                padding:5px 10px;margin-bottom:16px;">Asistente diario</div>
    <h1 style="font-size:32px;line-height:1.1;letter-spacing:-1px;margin:0 0 8px;color:#0f172a;">Plan del dia listo.</h1>
    <p style="margin:0;font-size:14px;color:#475569;">Sesiones ordenadas por Score de Urgencia · {local_ts}</p>
    <table role="presentation" style="margin-top:22px;">
      <tr>
        <td style="width:33%;padding-right:8px;vertical-align:top;">
          <div class="mc"><div class="mv">{len(grouped)}</div><div class="ml">Tareas agendadas</div></div></td>
        <td style="width:33%;padding:0 4px;vertical-align:top;">
          <div class="mc"><div class="mv">{total_min_agendados}</div><div class="ml">Min programados</div></div></td>
        <td style="width:33%;padding-left:8px;vertical-align:top;">
          <div class="mc"><div class="mv">{len(unscheduled)}</div><div class="ml">Sin hueco hoy</div></div></td>
      </tr>
    </table>
  </div>

  <div class="panel">
    <p class="sec">Sesiones del dia</p>
    <table role="presentation">
      {cards_html if cards_html else '<tr><td><div style="padding:20px;color:#64748b;text-align:center;">Sin sesiones programadas.</div></td></tr>'}
    </table>
  </div>

  {'''<div class="panel"><p class="sec">Tareas sin hueco hoy</p><table role="presentation">''' + unsch_html + '''</table></div>''' if unsch_html else ""}

  <div class="panel">
    <p class="sec">Agenda de hoy</p>
    <table>
      <thead><tr><th>Hora</th><th>Evento</th><th>Duracion</th></tr></thead>
      <tbody>{ag_hoy if ag_hoy else '<tr><td colspan="3" style="padding:14px;text-align:center;color:#334155;">Sin eventos.</td></tr>'}</tbody>
    </table>
  </div>

  <div class="panel">
    <p class="sec">Agenda de manana</p>
    <table>
      <thead><tr><th>Hora</th><th>Evento</th><th>Duracion</th></tr></thead>
      <tbody>{ag_man if ag_man else '<tr><td colspan="3" style="padding:14px;text-align:center;color:#334155;">Sin eventos.</td></tr>'}</tbody>
    </table>
  </div>

  <div style="text-align:center;margin-top:20px;">
    <a href="{hub_url}" style="display:inline-block;background:#0f172a;color:#fff;text-decoration:none;
                               font-weight:700;font-size:14px;padding:13px 22px;border-radius:14px;
                               box-shadow:0 10px 22px rgba(15,23,42,.15);">Abrir Task Hub</a>
  </div>
  <div style="text-align:center;color:#94a3b8;font-size:12px;padding-top:12px;">
    Asistente · {len(scheduled)} sesiones · {len(all_events)} eventos en calendario
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Email Sender
# ---------------------------------------------------------------------------

class EmailSender:
    @staticmethod
    def send(subject: str, html: str) -> bool:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = Config.EMAIL_FROM
            msg["To"]      = Config.EMAIL_TO
            msg.attach(MIMEText(html, "html", "utf-8"))
            with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as srv:
                srv.starttls()
                srv.login(Config.EMAIL_FROM, Config.EMAIL_PASSWORD)
                srv.send_message(msg)
            logger.info("Email enviado a %s", Config.EMAIL_TO)
            return True
        except Exception as exc:
            logger.error("Error enviando email: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------

class Asistente:
    def run(self) -> int:
        print("\n" + "=" * 70)
        print("ASISTENTE - Motor de Productividad")
        print(f"Ejecucion: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        print("=" * 70 + "\n")

        if not Config.validate():
            return 1

        # 1. Leer calendario
        logger.info("Leyendo Google Calendar...")
        cal    = CalendarClient()
        events = cal.get_events(Config.calendar_ids(), hours=48)

        # 2. Leer tareas pendientes (Pilar 5: todas, no solo In progress)
        logger.info("Leyendo tareas de Notion...")
        notion = NotionClient()
        tasks  = notion.get_pending_tasks(Config.NOTION_DATABASE_ID)

        # 3. Huecos libres hoy
        logger.info("Calculando huecos libres...")
        free_slots = find_free_slots(events, Config.TIMEZONE)
        logger.info("%d huecos disponibles:", len(free_slots))
        for sl in free_slots:
            logger.info("  %s (%d min) [%s]", sl["label"], sl["duration_min"], sl["context"])

        # 4. Asignar sesiones (Pilares 2, 3, 5)
        logger.info("Asignando sesiones...")
        scheduler  = Scheduler()
        scheduled, unscheduled = scheduler.assign(tasks, free_slots)
        logger.info("%d sesiones asignadas / %d tareas sin hueco", len(scheduled), len(unscheduled))

        # 5. Crear eventos en Google Calendar (Pilar 4)
        logger.info("Creando eventos en Google Calendar...")
        agendadas: Dict[str, int] = {}  # task_id → ultimo bloque_num agendado
        for sess in scheduled:
            bl = f" ({sess['bloque_numero']}/{sess['total_bloques']})" if sess["total_bloques"] else ""
            title = f"[Asistente] {sess['task_title']}{bl}"
            desc  = (
                f"Categoria: {sess['category']}\n"
                f"Prioridad: {sess['priority']}\n"
                f"Contexto: {sess.get('contexto') or '-'}\n"
                f"Score Urgencia: {sess['score']:.0f}\n"
                f"Duracion sesion: {sess['duration_min']} min"
            )
            color = CATEGORY_COLOR_MAP.get(sess["category"])
            ok = cal.create_event(title, sess["start"].isoformat(),
                                  sess["end"].isoformat(), desc, color_id=color)
            if ok:
                # Registrar el bloque más alto agendado para esta tarea
                prev = agendadas.get(sess["task_id"], 0)
                agendadas[sess["task_id"]] = max(prev, sess["bloques_completados_finales"])

        logger.info("%d eventos creados", len(agendadas))

        # 6. Sincronizar Status en Notion (Pilar 6)
        logger.info("Actualizando Notion...")
        for task_id, bloque_final in agendadas.items():
            notion.mark_in_progress(task_id)
            notion.update_bloques(task_id, bloque_final)

        # 7. Email (sin página de Notion — Pilar 4)
        logger.info("Enviando email...")
        now  = datetime.now(timezone.utc)
        html = EmailBuilder.build(
            scheduled, unscheduled, free_slots, events,
            now, Config.TIMEZONE, Config.TASK_HUB_URL,
        )
        subject = (
            f"Asistente · {now.astimezone(get_tz(Config.TIMEZONE)).strftime('%d/%m/%Y')} · "
            f"{len(agendadas)} tareas agendadas"
        )
        if not EmailSender.send(subject, html):
            return 1

        print("\n" + "=" * 70)
        print("ASISTENTE EJECUTADO EXITOSAMENTE")
        print(f"  Tareas agendadas:   {len(agendadas)}")
        print(f"  Sin hueco hoy:      {len(unscheduled)}")
        print(f"  Eventos calendario: {len(events)}")
        print("=" * 70 + "\n")
        return 0


if __name__ == "__main__":
    sys.exit(Asistente().run())
