#!/usr/bin/env python3
"""
ASISTENTE: Motor de Productividad Automatizado
==============================================
Integracion completa:
- Google Calendar API (proximas 24h)
- Notion API (tareas hoy/vencidas)
- Analisis simple de huecos disponibles
- Sugerencias priorizadas
- Email HTML profesional
- Actualizacion de pagina en Notion con resultados

Ejecucion: python asistente.py
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
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# Buscamos el archivo .env en la misma carpeta donde esta este script.
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


def configure_console_encoding():
    """Intenta forzar UTF-8 para evitar errores con Unicode en Windows."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


configure_console_encoding()

if os.getenv("NOTION_API_KEY"):
    print("✅ Conexion con archivo .env establecida")
else:
    print("❌ ERROR: No se encuentran las variables en el .env")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def get_timezone(tz_name: str):
    """Devuelve una zona horaria valida con fallback para Windows."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        if tz_name == "America/Guayaquil":
            logger.warning(
                "No se encontro la zona %s en el sistema. Se usara UTC-05:00 como respaldo.",
                tz_name,
            )
            return timezone(timedelta(hours=-5), name="America/Guayaquil")

        logger.warning(
            "No se encontro la zona %s. Se usara UTC como respaldo.",
            tz_name,
        )
        return timezone.utc


class ConfigAsistente:
    """Centraliza las variables de entorno."""

    GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

    NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
    NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
    NOTION_OUTPUT_PAGE_ID = os.environ.get("NOTION_OUTPUT_PAGE_ID")
    NOTION_VERSION = os.environ.get("NOTION_VERSION", "2025-09-03")
    TASK_HUB_URL = os.environ.get("TASK_HUB_URL", "https://www.notion.so/")

    SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    EMAIL_FROM = os.environ.get("EMAIL_FROM")
    EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
    EMAIL_TO = os.environ.get("EMAIL_TO", EMAIL_FROM)

    TIMEZONE = os.environ.get("TIMEZONE", "America/Guayaquil")

    @staticmethod
    def validate():
        """Valida que todas las variables requeridas esten presentes."""
        required = [
            "GOOGLE_CREDENTIALS_JSON",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "NOTION_OUTPUT_PAGE_ID",
            "EMAIL_FROM",
            "EMAIL_PASSWORD",
            "EMAIL_TO",
        ]
        missing = [key for key in required if not getattr(ConfigAsistente, key)]

        if missing:
            logger.error("Variables de entorno faltantes: %s", ", ".join(missing))
            return False

        logger.info("Configuracion validada correctamente")
        return True


def normalize_notion_id(raw_id: Optional[str]) -> Optional[str]:
    """Normaliza IDs de Notion removiendo formato de URL y guiones."""
    if not raw_id:
        return raw_id

    cleaned = raw_id.strip()

    if "/" in cleaned:
        cleaned = cleaned.rstrip("/").split("/")[-1]
    if "?" in cleaned:
        cleaned = cleaned.split("?", 1)[0]
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0]

    if "-" in cleaned and len(cleaned) > 32:
        cleaned = cleaned.split("-")[-1]

    return cleaned.replace("-", "")


def parse_notion_error(response: requests.Response) -> str:
    """Devuelve un mensaje breve de error para respuestas de Notion."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"

    code = payload.get("code")
    message = payload.get("message")
    if code and message:
        return f"{code}: {message}"
    return message or code or f"HTTP {response.status_code}"


def split_events_by_day(events: List[Dict], tz) -> Tuple[List[Dict], List[Dict]]:
    """Separa eventos entre hoy y mañana segun la zona local."""
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)

    today_events = []
    tomorrow_events = []

    for event in events:
        event_day = event["start"].astimezone(tz).date()
        if event_day == today:
            today_events.append(event)
        elif event_day == tomorrow:
            tomorrow_events.append(event)

    return today_events, tomorrow_events


class GoogleCalendarIntegration:
    """Obtiene eventos de Google Calendar para las proximas 24 horas."""

    def __init__(self, credentials_json: str):
        try:
            credentials_dict = json.loads(credentials_json)
            self.credentials = Credentials.from_service_account_info(
                credentials_dict,
                scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            )
            self.service = build("calendar", "v3", credentials=self.credentials)
            logger.info("Google Calendar autenticado")
        except Exception as exc:
            logger.error("Error autenticando Google Calendar: %s", exc)
            raise

    def get_events_horizon(self, calendar_id: str = "primary", hours: int = 48) -> List[Dict]:
        """Obtiene todos los eventos del horizonte solicitado."""
        try:
            now = datetime.now(timezone.utc)
            end_time = now + timedelta(hours=hours)

            events_result = (
                self.service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=now.isoformat(),
                    timeMax=end_time.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    fields="items(id,summary,start,end)",
                )
                .execute()
            )

            events = []
            for event in events_result.get("items", []):
                start = event["start"].get("dateTime", event["start"].get("date"))
                end = event["end"].get("dateTime", event["end"].get("date"))

                if isinstance(start, str) and "T" in start:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    duration_min = int((end_dt - start_dt).total_seconds() / 60)

                    events.append(
                        {
                            "id": event["id"],
                            "summary": event.get("summary", "Evento sin titulo"),
                            "start": start_dt,
                            "end": end_dt,
                            "duration_minutes": duration_min,
                        }
                    )

            logger.info("%s eventos encontrados en proximas %sh", len(events), hours)
            return events
        except Exception as exc:
            logger.error("Error obteniendo eventos: %s", exc)
            return []


class NotionIntegration:
    """Consulta y actualiza Notion API."""

    NOTION_API_URL = "https://api.notion.com/v1"

    def __init__(self, api_key: str, notion_version: str):
        self.api_key = api_key
        self.notion_version = notion_version
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": notion_version,
            "Content-Type": "application/json",
        }
        logger.info("Notion API inicializado con version %s", notion_version)

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        response = requests.request(
            method,
            f"{self.NOTION_API_URL}/{endpoint}",
            headers=self.headers,
            timeout=20,
            **kwargs,
        )
        return response

    def _resolve_data_source_id(self, database_or_source_id: str) -> str:
        """
        Acepta un database_id o un data_source_id y devuelve el data_source_id real.
        """
        notion_id = normalize_notion_id(database_or_source_id)

        db_response = self._request("GET", f"databases/{notion_id}")
        if db_response.status_code == 200:
            database = db_response.json()
            data_sources = database.get("data_sources", [])
            if data_sources:
                resolved_id = data_sources[0]["id"]
                logger.info("Database resuelta a data source: %s", resolved_id)
                return resolved_id

            logger.info(
                "La database no devolvio data_sources; se reutiliza el ID original"
            )
            return notion_id

        ds_response = self._request("GET", f"data_sources/{notion_id}")
        if ds_response.status_code == 200:
            logger.info("El ID recibido ya corresponde a un data source")
            return notion_id

        raise RuntimeError(
            "No se pudo resolver el ID de Notion ni como database ni como data source. "
            f"Database -> {parse_notion_error(db_response)} | "
            f"Data source -> {parse_notion_error(ds_response)}"
        )

    def _get_data_source_schema(self, data_source_id: str) -> Dict:
        """Recupera el esquema del data source."""
        response = self._request("GET", f"data_sources/{data_source_id}")
        response.raise_for_status()
        return response.json().get("properties", {})

    @staticmethod
    def _extract_plain_text(rich_text_items: List[Dict]) -> str:
        return "".join(item.get("plain_text", "") for item in rich_text_items).strip()

    @staticmethod
    def _normalize_label(value: str) -> str:
        return (
            value.lower()
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
        )

    def _find_property(
        self,
        properties: Dict,
        expected_types: List[str],
        preferred_names: List[str],
    ) -> Tuple[Optional[str], Optional[Dict]]:
        """Busca una propiedad por nombre preferido y luego por tipo."""
        normalized_targets = [self._normalize_label(name) for name in preferred_names]

        for name, config in properties.items():
            if config.get("type") not in expected_types:
                continue
            if self._normalize_label(name) in normalized_targets:
                return name, config

        for name, config in properties.items():
            if config.get("type") in expected_types:
                return name, config

        return None, None

    def _build_query_payload(self, properties: Dict) -> Dict:
        """Construye un payload de query compatible con el esquema real."""
        today = datetime.now().date().isoformat()

        status_name, status_prop = self._find_property(
            properties,
            ["status", "select", "checkbox"],
            ["Status", "Estado", "Situacion"],
        )
        due_name, due_prop = self._find_property(
            properties,
            ["date"],
            ["Due Date", "Fecha", "Fecha limite", "Vencimiento", "Deadline"],
        )

        filters = []

        if status_name and status_prop:
            status_type = status_prop.get("type")
            if status_type == "checkbox":
                filters.append({"property": status_name, "checkbox": {"equals": False}})
            elif status_type in {"status", "select"}:
                options = status_prop.get(status_type, {}).get("options", [])
                completed_aliases = {
                    "completado",
                    "completed",
                    "done",
                    "hecho",
                    "listo",
                    "finalizado",
                }
                completed_value = next(
                    (
                        option["name"]
                        for option in options
                        if self._normalize_label(option["name"]) in completed_aliases
                    ),
                    None,
                )
                if completed_value:
                    filters.append(
                        {
                            "property": status_name,
                            status_type: {"does_not_equal": completed_value},
                        }
                    )

        if due_name and due_prop:
            filters.append(
                {
                    "or": [
                        {"property": due_name, "date": {"equals": today}},
                        {"property": due_name, "date": {"before": today}},
                    ]
                }
            )

        payload: Dict = {"page_size": 100}
        if len(filters) == 1:
            payload["filter"] = filters[0]
        elif len(filters) > 1:
            payload["filter"] = {"and": filters}

        if due_name:
            payload["sorts"] = [
                {"property": due_name, "direction": "ascending"},
                {"timestamp": "last_edited_time", "direction": "descending"},
            ]
        else:
            payload["sorts"] = [{"timestamp": "last_edited_time", "direction": "descending"}]

        return payload

    def _extract_task_from_result(self, result: Dict, properties: Dict) -> Dict:
        """Extrae una tarea adaptandose al esquema real del data source."""
        props = result.get("properties", {})

        title_name, _ = self._find_property(properties, ["title"], ["Name", "Nombre", "Tarea", "Task"])
        due_name, _ = self._find_property(
            properties,
            ["date"],
            ["Due Date", "Fecha", "Fecha limite", "Vencimiento", "Deadline"],
        )
        status_name, status_prop = self._find_property(
            properties,
            ["status", "select", "checkbox"],
            ["Status", "Estado", "Situacion"],
        )
        priority_name, priority_prop = self._find_property(
            properties,
            ["select", "status", "multi_select"],
            ["Priority", "Prioridad"],
        )
        category_name, category_prop = self._find_property(
            properties,
            ["select", "status", "multi_select"],
            ["Category", "Categoria", "Area"],
        )
        duration_name, duration_prop = self._find_property(
            properties,
            ["number"],
            [
                "Duracion estimada",
                "Duracion",
                "Estimated Duration",
                "Estimate",
                "Minutos",
                "Tiempo estimado",
            ],
        )
        context_name, context_prop = self._find_property(
            properties,
            ["select", "status", "multi_select", "rich_text"],
            ["Contexto", "Context", "Ubicacion", "Lugar"],
        )

        title_items = props.get(title_name or "", {}).get("title", [])
        title_text = self._extract_plain_text(title_items) or "Sin titulo"

        status_value = "Por hacer"
        if status_name and status_prop:
            prop_type = status_prop.get("type")
            status_data = props.get(status_name, {})
            if prop_type == "status":
                status_value = (status_data.get("status") or {}).get("name", "Por hacer")
            elif prop_type == "select":
                status_value = (status_data.get("select") or {}).get("name", "Por hacer")
            elif prop_type == "checkbox":
                status_value = "Completado" if status_data.get("checkbox") else "Por hacer"

        due_value = None
        if due_name:
            due_value = (props.get(due_name, {}).get("date") or {}).get("start")

        priority_value = "Normal"
        if priority_name and priority_prop:
            prop_type = priority_prop.get("type")
            priority_data = props.get(priority_name, {})
            if prop_type == "status":
                priority_value = (priority_data.get("status") or {}).get("name", "Normal")
            elif prop_type == "select":
                priority_value = (priority_data.get("select") or {}).get("name", "Normal")
            elif prop_type == "multi_select":
                items = priority_data.get("multi_select") or []
                priority_value = items[0]["name"] if items else "Normal"

        category_value = "General"
        if category_name and category_prop:
            prop_type = category_prop.get("type")
            category_data = props.get(category_name, {})
            if prop_type == "status":
                category_value = (category_data.get("status") or {}).get("name", "General")
            elif prop_type == "select":
                category_value = (category_data.get("select") or {}).get("name", "General")
            elif prop_type == "multi_select":
                items = category_data.get("multi_select") or []
                category_value = items[0]["name"] if items else "General"

        estimated_minutes = None
        if duration_name and duration_prop:
            estimated_minutes = props.get(duration_name, {}).get("number")
            if estimated_minutes is not None:
                estimated_minutes = int(estimated_minutes)

        context_value = None
        if context_name and context_prop:
            prop_type = context_prop.get("type")
            context_data = props.get(context_name, {})
            if prop_type == "status":
                context_value = (context_data.get("status") or {}).get("name")
            elif prop_type == "select":
                context_value = (context_data.get("select") or {}).get("name")
            elif prop_type == "multi_select":
                items = context_data.get("multi_select") or []
                context_value = items[0]["name"] if items else None
            elif prop_type == "rich_text":
                context_value = self._extract_plain_text(context_data.get("rich_text", [])) or None

        return {
            "id": result["id"],
            "title": title_text,
            "status": status_value,
            "due_date": due_value,
            "priority": priority_value,
            "category": category_value,
            "estimated_minutes": estimated_minutes,
            "context": context_value,
        }

    def query_database(self, database_id: str) -> List[Dict]:
        """Consulta tareas no completadas de hoy o vencidas."""
        try:
            data_source_id = self._resolve_data_source_id(database_id)
            properties = self._get_data_source_schema(data_source_id)
            url = f"data_sources/{data_source_id}/query"
            payload = self._build_query_payload(properties)

            response = self._request("POST", url, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Notion devolvio {response.status_code} al consultar tareas: "
                    f"{parse_notion_error(response)} | payload={payload}"
                )

            tasks = []
            for result in response.json().get("results", []):
                tasks.append(self._extract_task_from_result(result, properties))

            logger.info("%s tareas obtenidas de Notion", len(tasks))
            return tasks
        except Exception as exc:
            logger.error("Error consultando Notion: %s", exc)
            return []

    def _make_rich_text(self, text: str) -> List[Dict]:
        return [{"type": "text", "text": {"content": text[:2000]}}]

    def append_report_to_page(
        self,
        page_id: str,
        suggestions: List[Dict],
        free_slots: List[Dict],
        tasks: List[Dict],
        timestamp: datetime,
    ) -> bool:
        """Agrega un reporte resumido a la pagina de salida."""
        try:
            normalized_page_id = normalize_notion_id(page_id)

            page_response = self._request("GET", f"pages/{normalized_page_id}")
            page_response.raise_for_status()

            blocks = [
                {"object": "block", "type": "divider", "divider": {}},
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": self._make_rich_text(
                            f"Sugerencias del {timestamp.strftime('%d/%m/%Y %H:%M')}"
                        )
                    },
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": self._make_rich_text(
                            f"Tareas pendientes analizadas: {len(tasks)} | "
                            f"Huecos detectados: {len(free_slots)} | "
                            f"Sugerencias generadas: {len(suggestions)}"
                        )
                    },
                },
            ]

            if suggestions:
                blocks.append(
                    {
                        "object": "block",
                        "type": "heading_3",
                        "heading_3": {
                            "rich_text": self._make_rich_text("Sugerencias principales")
                        },
                    }
                )
                for suggestion in suggestions[:5]:
                    line = (
                        f"{suggestion['task_title']} -> "
                        f"{suggestion['slot_label']} | "
                        f"{suggestion['reason']}"
                    )
                    blocks.append(
                        {
                            "object": "block",
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": self._make_rich_text(line)
                            },
                        }
                    )
            else:
                blocks.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": self._make_rich_text(
                                "No se generaron sugerencias porque no hubo tareas priorizables o huecos suficientes."
                            )
                        },
                    }
                )

            if free_slots:
                blocks.append(
                    {
                        "object": "block",
                        "type": "heading_3",
                        "heading_3": {"rich_text": self._make_rich_text("Huecos detectados")},
                    }
                )
                for slot in free_slots[:5]:
                    blocks.append(
                        {
                            "object": "block",
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": self._make_rich_text(
                                    f"{slot['label']} ({slot['duration_minutes']} min)"
                                )
                            },
                        }
                    )

            response = self._request(
                "PATCH",
                f"blocks/{normalized_page_id}/children",
                json={"children": blocks},
            )
            response.raise_for_status()

            logger.info("Pagina de Notion actualizada: %s", normalized_page_id)
            return True
        except Exception as exc:
            logger.error("Error actualizando pagina Notion: %s", exc)
            return False


class MetricasDeValor:
    """Genera justificaciones dinamicas basadas en palabras clave."""

    KEYWORDS_MAPPING = {
        "estudio": {
            "keywords": [
                "estudio",
                "medicina",
                "fisiologia",
                "inmunologia",
                "imagenologia",
                "metodologia",
                "psicologia",
                "aprender",
                "leer",
                "investigacion",
                "investigar",
            ],
            "metric": "Pico de alerta cognitiva para retencion profunda",
        },
        "gym": {
            "keywords": [
                "gym",
                "entrenamiento",
                "ejercicio",
                "push",
                "pull",
                "legs",
                "ppl",
                "fuerza",
                "hipertrofia",
                "cardio",
                "entrenar",
            ],
            "metric": "Ventana de fuerza maxima segun ritmos circadianos",
        },
        "general": {
            "keywords": [],
            "metric": "Optimizacion de flujo para evitar fatiga mental",
        },
    }

    @staticmethod
    def get_metric(task_title: str, category: str = "") -> str:
        text_to_check = f"{task_title} {category}".lower()
        for category_key, config in MetricasDeValor.KEYWORDS_MAPPING.items():
            if category_key == "general":
                continue
            for keyword in config["keywords"]:
                if keyword in text_to_check:
                    return config["metric"]
        return MetricasDeValor.KEYWORDS_MAPPING["general"]["metric"]


class ExplicadorDeSugerencias:
    """Construye razones mas concretas y menos genericas para cada recomendacion."""

    @staticmethod
    def build_reason(task: Dict, slot: Dict) -> str:
        priority = task.get("priority", "Normal")
        due_date = task.get("due_date")
        estimated = task.get("estimated_minutes")

        reasons = []

        if priority in {"Urgente", "Alta"}:
            reasons.append(f"prioridad {priority.lower()}")

        if due_date:
            reasons.append(f"vence el {due_date[:10]}")

        if estimated:
            reasons.append(f"requiere {estimated} min estimados")

        if slot["duration_minutes"] >= 180:
            reasons.append("bloque largo con pocas interrupciones")
        elif slot["duration_minutes"] >= 90:
            reasons.append("bloque suficiente para avanzar sin cambiar de contexto")
        else:
            reasons.append("bloque corto util para destrabar una parte concreta")

        base = ", ".join(reasons[:3])
        return f"Se recomienda este espacio porque combina {base}."


class MotorDeSugerencias:
    """Detecta huecos y propone tareas dentro de ellos."""

    PRIORITY_SCORES = {"Urgente": 4, "Alta": 3, "Normal": 2, "Baja": 1}
    DEFAULT_TASK_MINUTES = {"Urgente": 90, "Alta": 60, "Normal": 45, "Baja": 30}
    WORKDAY_START_HOUR = 6
    WORKDAY_END_HOUR = 22
    MIN_SLOT_MINUTES = 30
    LUNCH_START_HOUR = 13
    LUNCH_END_HOUR = 14
    CAMPUS_KEYWORDS = {
        "clase",
        "class",
        "facultad",
        "universidad",
        "hospital",
        "rotacion",
        "rotation",
        "lab",
        "laboratorio",
        "seminario",
        "curso",
    }

    @staticmethod
    def _slot_label(start: datetime, end: datetime) -> str:
        return f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"

    @staticmethod
    def _normalize_text(value: Optional[str]) -> str:
        if not value:
            return ""
        return (
            value.lower()
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
        )

    @classmethod
    def _is_campus_event(cls, event: Dict) -> bool:
        text = cls._normalize_text(event.get("summary", ""))
        return any(keyword in text for keyword in cls.CAMPUS_KEYWORDS)

    @staticmethod
    def _merge_intervals(intervals: List[Dict]) -> List[Dict]:
        if not intervals:
            return []

        ordered = sorted(intervals, key=lambda item: item["start"])
        merged = [ordered[0].copy()]

        for interval in ordered[1:]:
            current = merged[-1]
            if interval["start"] <= current["end"]:
                current["end"] = max(current["end"], interval["end"])
                current["label"] = f"{current['label']} + {interval['label']}"
            else:
                merged.append(interval.copy())

        return merged

    @classmethod
    def _infer_slot_context(
        cls,
        slot_start: datetime,
        slot_end: datetime,
        campus_events: List[Dict],
    ) -> str:
        previous_event = None
        for event in campus_events:
            if event["end_local"] <= slot_start:
                previous_event = event
            else:
                break

        next_event = next(
            (event for event in campus_events if event["start_local"] >= slot_end),
            None,
        )

        if previous_event and next_event:
            gap_minutes = int(
                (next_event["start_local"] - previous_event["end_local"]).total_seconds() / 60
            )
            if gap_minutes <= 240:
                return "facultad"

        if previous_event and not next_event:
            return "casa"

        if next_event and not previous_event:
            return "casa"

        return "flexible"

    @classmethod
    def _context_matches(cls, task_context: Optional[str], slot_context: str) -> bool:
        normalized = cls._normalize_text(task_context)
        if not normalized:
            return True
        if "casa" in normalized or "home" in normalized:
            return slot_context in {"casa", "flexible"}
        if "facultad" in normalized or "campus" in normalized or "universidad" in normalized:
            return slot_context in {"facultad", "flexible"}
        return True

    @classmethod
    def find_free_slots(
        cls,
        events: List[Dict],
        tz_name: str,
    ) -> List[Dict]:
        """Calcula huecos libres dentro de la jornada del dia actual."""
        tz = get_timezone(tz_name)
        now_local = datetime.now(tz)
        today = now_local.date()
        day_start = now_local.replace(
            hour=cls.WORKDAY_START_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        day_end = now_local.replace(
            hour=cls.WORKDAY_END_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        if now_local > day_start:
            day_start = now_local.replace(second=0, microsecond=0)

        localized_events = []
        for event in events:
            start_local = event["start"].astimezone(tz)
            end_local = event["end"].astimezone(tz)

            if start_local.date() != today and end_local.date() != today:
                continue

            if end_local <= day_start or start_local >= day_end:
                continue

            localized_events.append(
                {
                    **event,
                    "start_local": max(start_local, day_start),
                    "end_local": min(end_local, day_end),
                }
            )

        localized_events.sort(key=lambda item: item["start_local"])
        campus_events = [event for event in localized_events if cls._is_campus_event(event)]

        occupied_intervals = [
            {
                "start": event["start_local"],
                "end": event["end_local"],
                "label": event["summary"],
            }
            for event in localized_events
        ]

        lunch_start = now_local.replace(
            hour=cls.LUNCH_START_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        lunch_end = now_local.replace(
            hour=cls.LUNCH_END_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        occupied_intervals.append(
            {"start": lunch_start, "end": lunch_end, "label": "Bloque protegido de almuerzo"}
        )

        if campus_events:
            first_class = campus_events[0]
            last_class = campus_events[-1]
            occupied_intervals.append(
                {
                    "start": max(day_start, first_class["start_local"] - timedelta(hours=1)),
                    "end": first_class["start_local"],
                    "label": "Buffer antes de primera clase",
                }
            )
            occupied_intervals.append(
                {
                    "start": last_class["end_local"],
                    "end": min(day_end, last_class["end_local"] + timedelta(hours=1)),
                    "label": "Buffer despues de ultima clase",
                }
            )

        merged_intervals = cls._merge_intervals(
            [
                interval
                for interval in occupied_intervals
                if interval["end"] > day_start and interval["start"] < day_end
            ]
        )

        free_slots = []
        cursor = day_start
        for interval in merged_intervals:
            if interval["start"] > cursor:
                duration = int((interval["start"] - cursor).total_seconds() / 60)
                if duration >= cls.MIN_SLOT_MINUTES:
                    slot_end = interval["start"]
                    free_slots.append(
                        {
                            "start": cursor,
                            "end": slot_end,
                            "duration_minutes": duration,
                            "label": cls._slot_label(cursor, slot_end),
                            "context": cls._infer_slot_context(cursor, slot_end, campus_events),
                        }
                    )
            cursor = max(cursor, interval["end"])

        if cursor < day_end:
            duration = int((day_end - cursor).total_seconds() / 60)
            if duration >= cls.MIN_SLOT_MINUTES:
                free_slots.append(
                    {
                        "start": cursor,
                        "end": day_end,
                        "duration_minutes": duration,
                        "label": cls._slot_label(cursor, day_end),
                        "context": cls._infer_slot_context(cursor, day_end, campus_events),
                    }
                )

        return free_slots

    @classmethod
    def generate(cls, tasks: List[Dict], free_slots: List[Dict]) -> List[Dict]:
        """Empareja tareas con huecos disponibles."""
        ordered_tasks = sorted(
            tasks,
            key=lambda task: (
                -cls.PRIORITY_SCORES.get(task.get("priority", "Normal"), 2),
                task.get("due_date") or "9999-12-31",
            ),
        )

        available_slots = free_slots.copy()
        suggestions = []

        for task in ordered_tasks:
            needed_minutes = task.get("estimated_minutes") or cls.DEFAULT_TASK_MINUTES.get(
                task.get("priority", "Normal"),
                45,
            )
            slot = next(
                (
                    candidate
                    for candidate in available_slots
                    if candidate["duration_minutes"] >= needed_minutes
                    and cls._context_matches(task.get("context"), candidate.get("context", "flexible"))
                ),
                None,
            )
            if not slot:
                continue

            reason = ExplicadorDeSugerencias.build_reason(task, slot)
            suggestions.append(
                {
                    "task_title": task["title"],
                    "priority": task.get("priority", "Normal"),
                    "slot_label": slot["label"],
                    "slot_duration": slot["duration_minutes"],
                    "required_minutes": needed_minutes,
                    "reason": reason,
                }
            )
            available_slots.remove(slot)

        return suggestions


class EmailConstructor:
    """Construye email HTML con sugerencias."""

    @staticmethod
    def _build_daily_insight(
        tasks: List[Dict],
        free_slots: List[Dict],
        suggestions: List[Dict],
    ) -> str:
        """Genera una frase breve con valor agregado."""
        if suggestions and free_slots:
            best = suggestions[0]
            longest_slot = max(free_slots, key=lambda slot: slot["duration_minutes"])
            return (
                f"Hoy tienes una ventana clara de {longest_slot['duration_minutes']} minutos. "
                f"El mejor movimiento es enfocar primero '{best['task_title']}' en el bloque {best['slot_label']}, "
                f"alineado con su necesidad real de {best.get('required_minutes', best['slot_duration'])} minutos."
            )

        if free_slots:
            longest_slot = max(free_slots, key=lambda slot: slot["duration_minutes"])
            return (
                f"Tu mayor activo hoy es un bloque libre de {longest_slot['duration_minutes']} minutos "
                f"entre {longest_slot['label']}. Conviene reservarlo para trabajo de alta concentracion."
            )

        if tasks:
            return (
                "Hoy el sistema detecta poco margen libre. La prioridad no es agregar mas trabajo, "
                "sino proteger energia y ejecutar solo lo esencial."
            )

        return (
            "Tu dia luce liviano. Este es un buen momento para adelantar una tarea importante antes "
            "de que se acumule carga el resto de la semana."
        )

    @staticmethod
    def _build_tomorrow_note(events: List[Dict], tz) -> str:
        """Genera una nota corta de anticipacion para el dia siguiente."""
        tomorrow = datetime.now(tz).date() + timedelta(days=1)
        tomorrow_events = [
            event for event in events
            if event["start"].astimezone(tz).date() == tomorrow
        ]

        if len(tomorrow_events) >= 5:
            return f"Mañana se perfila como un dia cargado: ya hay {len(tomorrow_events)} eventos en agenda. Conviene cerrar hoy con descanso y dejar claro el primer bloque de accion."
        if len(tomorrow_events) >= 2:
            return f"Mañana ya tiene {len(tomorrow_events)} compromisos visibles. Vale la pena dejar preparada desde hoy tu tarea de apertura."
        if len(tomorrow_events) == 1:
            return "Mañana empieza con baja friccion. Si dejas una prioridad bien definida hoy, puedes entrar en ritmo rapido."
        return "Mañana todavia luce flexible. Eso te da margen para proteger un bloque profundo desde temprano."

    @staticmethod
    def build_html(
        tasks: List[Dict],
        events: List[Dict],
        free_slots: List[Dict],
        suggestions: List[Dict],
        timestamp: datetime,
        tz_name: str,
        task_hub_url: str,
    ) -> str:
        critical_tasks = [task for task in tasks if task.get("priority") in ["Alta", "Urgente"]]
        local_tz = get_timezone(tz_name)
        date_str = timestamp.astimezone(local_tz).strftime("%d/%m/%Y %H:%M")
        today_events, tomorrow_events = split_events_by_day(events, local_tz)

        total_minutes = sum(event["duration_minutes"] for event in events)
        top_suggestion = suggestions[0] if suggestions else None
        daily_insight = EmailConstructor._build_daily_insight(tasks, free_slots, suggestions)
        tomorrow_note = EmailConstructor._build_tomorrow_note(events, local_tz)
        big_three = (critical_tasks or tasks)[:3]

        suggestion_cards_html = ""
        for suggestion in suggestions[:4]:
            priority = suggestion.get("priority", "Normal")
            priority_color = {
                "Urgente": "#ef4444",
                "Alta": "#f97316",
                "Normal": "#2563eb",
                "Baja": "#64748b",
            }.get(priority, "#2563eb")
            suggestion_cards_html += f"""
            <tr>
                <td style="padding-bottom: 14px;">
                    <div style="background:#f8fbff; border:1px solid #dbeafe; border-radius:18px; padding:20px 22px;">
                        <div style="font-size:11px; font-weight:700; letter-spacing:1.2px; text-transform:uppercase; color:{priority_color}; margin-bottom:10px;">
                            {priority}
                        </div>
                        <div style="font-size:18px; line-height:1.35; font-weight:700; color:#0f172a; margin-bottom:8px;">
                            {suggestion['task_title']}
                        </div>
                        <div style="font-size:14px; color:#334155; margin-bottom:8px;">
                            Recomendado para {suggestion['slot_label']} ({suggestion['slot_duration']} min disponibles)
                        </div>
                        <div style="font-size:13px; color:#0f172a; margin-bottom:8px; font-weight:600;">
                            Duracion estimada de la tarea: {suggestion.get('required_minutes', 'N/D')} min
                        </div>
                        <div style="font-size:13px; color:#64748b; line-height:1.6;">
                            {suggestion['reason']}
                        </div>
                    </div>
                </td>
            </tr>
            """

        gaps_cards_html = ""
        for slot in free_slots[:4]:
            slot_kind = "Bloque profundo" if slot["duration_minutes"] >= 90 else "Bloque rapido"
            gaps_cards_html += f"""
            <td style="padding:0 8px 12px 8px; vertical-align:top;">
                <div style="background:#fff7ed; border:1px solid #fdba74; border-radius:18px; padding:18px;">
                    <div style="font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:1px; color:#3b82f6; margin-bottom:8px;">
                        {slot_kind}
                    </div>
                    <div style="font-size:20px; font-weight:700; color:#0f172a; margin-bottom:6px;">
                        {slot['label']}
                    </div>
                    <div style="font-size:13px; color:#64748b;">
                        {slot['duration_minutes']} minutos disponibles
                    </div>
                </div>
            </td>
            """

        tasks_html = ""
        for task in big_three:
            priority = task.get("priority", "Normal")
            priority_color = {
                "Urgente": "#ef4444",
                "Alta": "#f97316",
                "Normal": "#2563eb",
                "Baja": "#64748b",
            }.get(priority, "#2563eb")
            metric = MetricasDeValor.get_metric(task["title"], task.get("category", ""))
            tasks_html += f"""
            <tr>
                <td style="padding: 0 0 12px 0;">
                    <div style="background:#f5f3ff; border:1px solid #c4b5fd; border-radius:16px; padding:16px 18px;">
                        <div style="font-size:16px; font-weight:700; color:#0f172a; margin-bottom:6px;">
                            {task['title']}
                        </div>
                        <div style="font-size:13px; color:{priority_color}; font-weight:700; margin-bottom:4px;">
                            Prioridad: {priority}
                        </div>
                        <div style="font-size:13px; color:#64748b; line-height:1.5;">
                            {metric}
                        </div>
                    </div>
                </td>
            </tr>
            """

        time_blocks_html = ""
        for event in today_events[:6]:
            start_time = event["start"].astimezone(local_tz).strftime("%H:%M")
            end_time = event["end"].astimezone(local_tz).strftime("%H:%M")
            metric = MetricasDeValor.get_metric(event["summary"], "Calendario")

            time_blocks_html += f"""
            <tr>
                <td style="padding: 12px; border-bottom: 1px solid #e0e0e0;">
                    <strong>{start_time}</strong> → <strong>{end_time}</strong>
                </td>
                <td style="padding: 12px; border-bottom: 1px solid #e0e0e0;">
                    {event['summary']}
                </td>
                <td style="padding: 12px; border-bottom: 1px solid #e0e0e0; text-align: center;">
                    {event['duration_minutes']} min
                </td>
                <td style="padding: 12px; border-bottom: 1px solid #e0e0e0; font-size: 12px; color: #666;">
                    {metric}
                </td>
            </tr>
            """

        tomorrow_blocks_html = ""
        for event in tomorrow_events[:6]:
            start_time = event["start"].astimezone(local_tz).strftime("%H:%M")
            end_time = event["end"].astimezone(local_tz).strftime("%H:%M")
            tomorrow_blocks_html += f"""
            <tr>
                <td style="padding: 12px; border-bottom: 1px solid #cbd5e1;">
                    <strong>{start_time}</strong> → <strong>{end_time}</strong>
                </td>
                <td style="padding: 12px; border-bottom: 1px solid #cbd5e1; color:#0f172a;">
                    {event['summary']}
                </td>
                <td style="padding: 12px; border-bottom: 1px solid #cbd5e1; text-align: center; color:#334155;">
                    {event['duration_minutes']} min
                </td>
            </tr>
            """

        html = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
                    line-height: 1.6;
                    color: #0f172a;
                    background: linear-gradient(180deg, #eaf4ff 0%, #f8fafc 48%, #f4f0ff 100%);
                    margin: 0;
                    padding: 0;
                }}
                .container {{
                    max-width: 720px;
                    margin: 0 auto;
                    background-color: transparent;
                    border-radius: 0;
                    overflow: hidden;
                }}
                .shell {{
                    padding: 28px 18px 40px 18px;
                }}
                .hero {{
                    background: radial-gradient(circle at top left, #cfe7ff 0%, #ffffff 38%, #f8fafc 100%);
                    border: 1px solid #bfdbfe;
                    border-radius: 28px;
                    padding: 34px 30px 28px 30px;
                    box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
                    margin-bottom: 18px;
                }}
                .eyebrow {{
                    display: inline-block;
                    font-size: 11px;
                    font-weight: 700;
                    letter-spacing: 1.2px;
                    text-transform: uppercase;
                    color: #2563eb;
                    background: #eff6ff;
                    border: 1px solid #bfdbfe;
                    border-radius: 999px;
                    padding: 6px 10px;
                    margin-bottom: 18px;
                }}
                .hero h1 {{
                    font-size: 34px;
                    line-height: 1.1;
                    letter-spacing: -1px;
                    margin: 0 0 10px 0;
                    color: #0f172a;
                }}
                .hero p {{
                    margin: 0;
                    font-size: 15px;
                    color: #475569;
                }}
                .section-title {{
                    font-size: 12px;
                    font-weight: 700;
                    text-transform: uppercase;
                    letter-spacing: 1.2px;
                    color: #64748b;
                    margin: 0 0 12px 0;
                }}
                .panel {{
                    background: rgba(255,255,255,0.96);
                    border: 1px solid #cbd5e1;
                    border-radius: 24px;
                    padding: 24px;
                    box-shadow: 0 12px 30px rgba(15, 23, 42, 0.05);
                    margin-bottom: 18px;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 13px;
                }}
                th {{
                    background: #f8fafc;
                    text-align: left;
                    padding: 12px;
                    color: #334155;
                    border-bottom: 1px solid #e2e8f0;
                }}
                .footer {{
                    padding: 10px 18px 0 18px;
                    text-align: center;
                    color: #64748b;
                    font-size: 12px;
                }}
                .metric-card {{
                    background: #ffffff;
                    border: 1px solid #cbd5e1;
                    border-radius: 18px;
                    padding: 18px;
                }}
                .metric-value {{
                    font-size: 26px;
                    font-weight: 700;
                    line-height: 1.1;
                    color: #0f172a;
                    margin-bottom: 6px;
                }}
                .metric-label {{
                    font-size: 12px;
                    color: #64748b;
                    text-transform: uppercase;
                    letter-spacing: 1px;
                }}
                @media only screen and (max-width: 640px) {{
                    .shell {{
                        padding: 14px 10px 28px 10px !important;
                    }}
                    .hero {{
                        padding: 24px 18px 20px 18px !important;
                        border-radius: 22px !important;
                    }}
                    .hero h1 {{
                        font-size: 28px !important;
                    }}
                    .panel {{
                        padding: 18px !important;
                        border-radius: 20px !important;
                    }}
                    .metric-card {{
                        padding: 14px !important;
                    }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="shell">
                    <div class="hero">
                        <div class="eyebrow">Asistente diario</div>
                        <h1>Tu agenda ya tiene una mejor version.</h1>
                        <p>Resumen premium de tareas, huecos disponibles y recomendaciones accionables para hoy.</p>
                        <p style="margin-top:10px; font-size:13px; color:#64748b;">{date_str}</p>

                        <table role="presentation" style="margin-top:24px;">
                            <tr>
                                <td style="width:33.33%; padding-right:8px; vertical-align:top;">
                                    <div class="metric-card">
                                        <div class="metric-value">{len(tasks)}</div>
                                        <div class="metric-label">Tareas pendientes</div>
                                    </div>
                                </td>
                                <td style="width:33.33%; padding:0 4px; vertical-align:top;">
                                    <div class="metric-card">
                                        <div class="metric-value">{len(free_slots)}</div>
                                        <div class="metric-label">Huecos detectados</div>
                                    </div>
                                </td>
                                <td style="width:33.33%; padding-left:8px; vertical-align:top;">
                                    <div class="metric-card">
                                        <div class="metric-value">{total_minutes}</div>
                                        <div class="metric-label">Minutos agendados</div>
                                    </div>
                                </td>
                            </tr>
                        </table>
                    </div>

                    <div class="panel">
                        <div class="section-title">Insight del dia</div>
                        <div style="background:#dcfce7; border:1px solid #4ade80; border-radius:20px; padding:20px 22px;">
                            <div style="font-size:20px; line-height:1.4; font-weight:700; color:#0f172a; margin-bottom:8px;">
                                {daily_insight}
                            </div>
                            <div style="font-size:13px; color:#64748b;">
                                El valor de Asistente no es recordarte tareas: es señalar el mejor momento para ejecutarlas.
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="section-title">Sugerencia principal</div>
                        <div style="background:#0f172a; border-radius:22px; padding:24px; color:#ffffff; border:1px solid #1d4ed8;">
                            <div style="font-size:12px; text-transform:uppercase; letter-spacing:1.2px; color:#bfdbfe; margin-bottom:10px; font-weight:700;">
                                Hoja de ruta sugerida
                            </div>
                            <div style="font-size:24px; line-height:1.2; font-weight:700; margin-bottom:10px; color:#ffffff;">
                                {top_suggestion['task_title'] if top_suggestion else 'Analizando tu flujo optimo...'}
                            </div>
                            <div style="font-size:14px; line-height:1.6; color:#e2e8f0;">
                                {f"Bloque recomendado: {top_suggestion['slot_label']} ({top_suggestion['slot_duration']} min disponibles) para una tarea estimada en {top_suggestion.get('required_minutes', top_suggestion['slot_duration'])} min. {top_suggestion['reason']}" if top_suggestion else 'Hoy no se encontraron cruces fuertes entre tareas y disponibilidad, pero el sistema sigue monitoreando tus huecos.'}
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="section-title">Sugerencias priorizadas</div>
                        <table role="presentation">
                            {suggestion_cards_html if suggestion_cards_html else '<tr><td><div style="background:#ffffff; border:1px dashed #cbd5e1; border-radius:18px; padding:20px; color:#64748b;">No se encontraron emparejamientos entre tareas y huecos disponibles.</div></td></tr>'}
                        </table>
                    </div>

                    <div class="panel">
                        <div class="section-title">Disponibilidad del dia</div>
                        <table role="presentation">
                            <tr>
                            {gaps_cards_html if gaps_cards_html else '<td><div style="background:#fff7ed; border:1px dashed #fdba74; border-radius:18px; padding:20px; color:#7c2d12;">Sin bloques libres detectados hoy.</div></td>'}
                        </tr>
                    </table>
                </div>

                    <div class="panel">
                        <div class="section-title">The Big Three</div>
                        <table role="presentation">
                            {tasks_html if tasks_html else '<tr><td><div style="background:#f5f3ff; border:1px dashed #a78bfa; border-radius:18px; padding:20px; color:#5b21b6;">No hay objetivos prioritarios pendientes para hoy.</div></td></tr>'}
                        </table>
                    </div>

                    <div class="panel">
                        <div class="section-title">Agenda de hoy</div>
                        <table>
                            <thead>
                                <tr>
                                    <th>Hora</th>
                                    <th>Actividad</th>
                                    <th>Duracion</th>
                                    <th>Valor estrategico</th>
                                </tr>
                            </thead>
                            <tbody>
                                {time_blocks_html if time_blocks_html else '<tr><td colspan="4" style="padding: 16px; text-align:center; color:#334155; background:#ffffff;">Sin eventos programados para hoy.</td></tr>'}
                            </tbody>
                        </table>
                    </div>

                    <div class="panel">
                        <div class="section-title">Agenda de mañana</div>
                        <table>
                            <thead>
                                <tr>
                                    <th>Hora</th>
                                    <th>Actividad</th>
                                    <th>Duracion</th>
                                </tr>
                            </thead>
                            <tbody>
                                {tomorrow_blocks_html if tomorrow_blocks_html else '<tr><td colspan="3" style="padding: 16px; text-align:center; color:#334155; background:#ffffff;">No hay clases o eventos agendados para mañana dentro del horizonte actual.</td></tr>'}
                            </tbody>
                        </table>
                    </div>

                    <div class="panel">
                        <div class="section-title">Anticipacion para mañana</div>
                        <div style="background:#f5f3ff; border:1px solid #c4b5fd; border-radius:20px; padding:18px 20px; font-size:14px; color:#334155;">
                            {tomorrow_note}
                        </div>
                    </div>

                    <div style="text-align:center; margin-top:22px;">
                        <a href="{task_hub_url}" style="display:inline-block; background:#0f172a; color:#ffffff; text-decoration:none; font-weight:700; font-size:15px; padding:14px 22px; border-radius:14px; box-shadow:0 10px 22px rgba(15,23,42,0.15);">
                            Abrir Task Hub
                        </a>
                    </div>
                </div>
                <div class="footer">
                    Enviado por <strong>Asistente</strong> | Tareas: {len(tasks)} | Eventos: {len(events)} | Huecos: {len(free_slots)}
                </div>
            </div>
        </body>
        </html>
        """
        return html


class EmailSender:
    """Envia emails via Gmail SMTP."""

    @staticmethod
    def send(
        smtp_server: str,
        smtp_port: int,
        email_from: str,
        password: str,
        email_to: str,
        subject: str,
        html_content: str,
    ) -> bool:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = email_from
            msg["To"] = email_to
            msg.attach(MIMEText(html_content, "html", "utf-8"))

            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(email_from, password)
                server.send_message(msg)

            logger.info("Email enviado a: %s", email_to)
            return True
        except Exception as exc:
            logger.error("Error enviando email: %s", exc)
            return False


class Asistente:
    """Orquesta todo el flujo del sistema."""

    def __init__(self, config: ConfigAsistente):
        self.config = config
        self.events: List[Dict] = []
        self.tasks: List[Dict] = []
        self.free_slots: List[Dict] = []
        self.suggestions: List[Dict] = []

    def run(self) -> int:
        try:
            print("\n" + "=" * 70)
            print("🤖 ASISTENTE - Motor de Productividad")
            print(f"Ejecucion: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
            print("=" * 70 + "\n")

            if not self.config.validate():
                return 1

            logger.info("Obtener eventos de Google Calendar...")
            calendar = GoogleCalendarIntegration(self.config.GOOGLE_CREDENTIALS_JSON)
            self.events = calendar.get_events_horizon(self.config.GOOGLE_CALENDAR_ID, hours=48)

            logger.info("Obtener tareas de Notion...")
            notion = NotionIntegration(
                self.config.NOTION_API_KEY,
                self.config.NOTION_VERSION,
            )
            self.tasks = notion.query_database(self.config.NOTION_DATABASE_ID)

            logger.info("Calcular huecos y sugerencias...")
            self.free_slots = MotorDeSugerencias.find_free_slots(
                self.events,
                self.config.TIMEZONE,
            )
            self.suggestions = MotorDeSugerencias.generate(self.tasks, self.free_slots)

            now = datetime.now(timezone.utc)

            logger.info("Construir email HTML...")
            html_content = EmailConstructor.build_html(
                self.tasks,
                self.events,
                self.free_slots,
                self.suggestions,
                now,
                self.config.TIMEZONE,
                self.config.TASK_HUB_URL,
            )

            logger.info("Enviar email...")
            email_ok = EmailSender.send(
                smtp_server=self.config.SMTP_SERVER,
                smtp_port=self.config.SMTP_PORT,
                email_from=self.config.EMAIL_FROM,
                password=self.config.EMAIL_PASSWORD,
                email_to=self.config.EMAIL_TO,
                subject=f"Asistente - Plan del dia {now.astimezone(get_timezone(self.config.TIMEZONE)).strftime('%d/%m/%Y')}",
                html_content=html_content,
            )
            if not email_ok:
                return 1

            logger.info("Actualizar pagina de Notion...")
            notion_ok = notion.append_report_to_page(
                self.config.NOTION_OUTPUT_PAGE_ID,
                self.suggestions,
                self.free_slots,
                self.tasks,
                now.astimezone(get_timezone(self.config.TIMEZONE)),
            )
            if not notion_ok:
                return 1

            print("\n" + "=" * 70)
            print("✅ ASISTENTE EJECUTADO EXITOSAMENTE")
            print("=" * 70 + "\n")
            return 0
        except Exception as exc:
            logger.error("ERROR: %s", exc)
            print("\n" + "=" * 70)
            print("❌ ERROR EN LA EJECUCION")
            print("=" * 70 + "\n")
            return 1


if __name__ == "__main__":
    config = ConfigAsistente()
    asistente = Asistente(config)
    sys.exit(asistente.run())
