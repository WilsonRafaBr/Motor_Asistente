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

    def get_events_24h(self, calendar_id: str = "primary") -> List[Dict]:
        """Obtiene todos los eventos de las proximas 24 horas."""
        try:
            now = datetime.now(timezone.utc)
            end_time = now + timedelta(days=1)

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

            logger.info("%s eventos encontrados en proximas 24h", len(events))
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

        return {
            "id": result["id"],
            "title": title_text,
            "status": status_value,
            "due_date": due_value,
            "priority": priority_value,
            "category": category_value,
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


class MotorDeSugerencias:
    """Detecta huecos y propone tareas dentro de ellos."""

    PRIORITY_SCORES = {"Urgente": 4, "Alta": 3, "Normal": 2, "Baja": 1}
    DEFAULT_TASK_MINUTES = {"Urgente": 90, "Alta": 60, "Normal": 45, "Baja": 30}
    WORKDAY_START_HOUR = 6
    WORKDAY_END_HOUR = 22
    MIN_SLOT_MINUTES = 30

    @staticmethod
    def _slot_label(start: datetime, end: datetime) -> str:
        return f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"

    @classmethod
    def find_free_slots(
        cls,
        events: List[Dict],
        tz_name: str,
    ) -> List[Dict]:
        """Calcula huecos libres dentro de la jornada del dia actual."""
        tz = get_timezone(tz_name)
        now_local = datetime.now(tz)
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

        free_slots = []
        cursor = day_start
        for event in localized_events:
            if event["start_local"] > cursor:
                duration = int((event["start_local"] - cursor).total_seconds() / 60)
                if duration >= cls.MIN_SLOT_MINUTES:
                    free_slots.append(
                        {
                            "start": cursor,
                            "end": event["start_local"],
                            "duration_minutes": duration,
                            "label": cls._slot_label(cursor, event["start_local"]),
                        }
                    )
            cursor = max(cursor, event["end_local"])

        if cursor < day_end:
            duration = int((day_end - cursor).total_seconds() / 60)
            if duration >= cls.MIN_SLOT_MINUTES:
                free_slots.append(
                    {
                        "start": cursor,
                        "end": day_end,
                        "duration_minutes": duration,
                        "label": cls._slot_label(cursor, day_end),
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
            needed_minutes = cls.DEFAULT_TASK_MINUTES.get(task.get("priority", "Normal"), 45)
            slot = next(
                (candidate for candidate in available_slots if candidate["duration_minutes"] >= needed_minutes),
                None,
            )
            if not slot:
                continue

            reason = MetricasDeValor.get_metric(task["title"], task.get("category", ""))
            suggestions.append(
                {
                    "task_title": task["title"],
                    "priority": task.get("priority", "Normal"),
                    "slot_label": slot["label"],
                    "slot_duration": slot["duration_minutes"],
                    "reason": reason,
                }
            )
            available_slots.remove(slot)

        return suggestions


class EmailConstructor:
    """Construye email HTML con sugerencias."""

    @staticmethod
    def build_html(
        tasks: List[Dict],
        events: List[Dict],
        free_slots: List[Dict],
        suggestions: List[Dict],
        timestamp: datetime,
        tz_name: str,
    ) -> str:
        critical_tasks = [task for task in tasks if task.get("priority") in ["Alta", "Urgente"]]
        local_tz = get_timezone(tz_name)

        time_blocks_html = ""
        for event in events[:6]:
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

        free_slots_html = ""
        for slot in free_slots[:5]:
            free_slots_html += f"""
            <div style="background: #eef6ff; border-left: 4px solid #2b6cb0; padding: 12px; margin: 8px 0; border-radius: 4px;">
                <strong>{slot['label']}</strong>
                <br/>
                <small style="color: #4a5568;">Disponible: {slot['duration_minutes']} minutos</small>
            </div>
            """

        suggestions_html = ""
        for suggestion in suggestions[:5]:
            suggestions_html += f"""
            <div style="background: #f0fff4; border-left: 4px solid #2f855a; padding: 12px; margin: 8px 0; border-radius: 4px;">
                <strong>{suggestion['task_title']}</strong>
                <br/>
                <small style="color: #2d3748;">
                    Recomendado para {suggestion['slot_label']} ({suggestion['slot_duration']} min)
                </small>
                <br/>
                <small style="color: #4a5568;">{suggestion['reason']}</small>
            </div>
            """

        critical_tasks_html = ""
        for task in critical_tasks[:3]:
            metric = MetricasDeValor.get_metric(task["title"], task.get("category", ""))
            critical_tasks_html += f"""
            <div style="background: #fffaf0; border-left: 4px solid #dd6b20; padding: 12px; margin: 8px 0; border-radius: 4px;">
                <strong>{task['title']}</strong>
                <br/>
                <small style="color: #4a5568;">Prioridad: {task['priority']} | {metric}</small>
            </div>
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
                    color: #333;
                    background-color: #f8f9fa;
                    margin: 0;
                    padding: 0;
                }}
                .container {{
                    max-width: 720px;
                    margin: 0 auto;
                    background-color: white;
                    border-radius: 10px;
                    overflow: hidden;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                .header {{
                    background: linear-gradient(135deg, #0f172a 0%, #1d4ed8 100%);
                    color: white;
                    padding: 32px 28px;
                    text-align: center;
                }}
                .content {{
                    padding: 28px;
                }}
                .section {{
                    margin-bottom: 28px;
                }}
                .section h2 {{
                    font-size: 16px;
                    text-transform: uppercase;
                    letter-spacing: 0.4px;
                    border-bottom: 2px solid #e2e8f0;
                    padding-bottom: 8px;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 13px;
                }}
                th {{
                    background: #f1f5f9;
                    text-align: left;
                    padding: 12px;
                }}
                .footer {{
                    background: #f8fafc;
                    padding: 20px 28px;
                    text-align: center;
                    color: #64748b;
                    font-size: 12px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">Asistente</h1>
                    <p style="margin: 8px 0 0 0;">Plan de productividad sugerido</p>
                    <p style="margin: 8px 0 0 0;">{timestamp.astimezone(local_tz).strftime('%d/%m/%Y %H:%M')}</p>
                </div>
                <div class="content">
                    <div class="section">
                        <h2>Sugerencias</h2>
                        {suggestions_html if suggestions_html else '<p style="color:#64748b;">No se encontraron emparejamientos entre tareas y huecos disponibles.</p>'}
                    </div>

                    <div class="section">
                        <h2>Huecos detectados</h2>
                        {free_slots_html if free_slots_html else '<p style="color:#64748b;">No se detectaron huecos libres de al menos 30 minutos.</p>'}
                    </div>

                    <div class="section">
                        <h2>Tareas criticas</h2>
                        {critical_tasks_html if critical_tasks_html else '<p style="color:#64748b;">No hay tareas criticas pendientes.</p>'}
                    </div>

                    <div class="section">
                        <h2>Eventos del calendario</h2>
                        <table>
                            <thead>
                                <tr>
                                    <th>Hora</th>
                                    <th>Actividad</th>
                                    <th>Duracion</th>
                                    <th>Valor</th>
                                </tr>
                            </thead>
                            <tbody>
                                {time_blocks_html if time_blocks_html else '<tr><td colspan="4" style="padding: 12px; text-align:center; color:#64748b;">Sin eventos programados</td></tr>'}
                            </tbody>
                        </table>
                    </div>
                </div>
                <div class="footer">
                    Tareas pendientes: {len(tasks)} | Eventos: {len(events)} | Huecos: {len(free_slots)}
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
            self.events = calendar.get_events_24h(self.config.GOOGLE_CALENDAR_ID)

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
