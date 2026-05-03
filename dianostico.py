#!/usr/bin/env python3
"""
DIAGNÓSTICO DE ASISTENTE
Prueba cada componente por separado y reporta exactamente qué funciona y qué no.
"""
import json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

def sep(title): print(f"\n{'='*60}\n{title}\n{'='*60}")
def ok(msg):   print(f"  ✅ {msg}")
def err(msg):  print(f"  ❌ {msg}")
def info(msg): print(f"  ℹ️  {msg}")

# ─── 1. Variables de entorno ──────────────────────────────────────────────────
sep("1. VARIABLES DE ENTORNO")
vars_check = {
    "GOOGLE_CREDENTIALS_JSON": os.environ.get("GOOGLE_CREDENTIALS_JSON",""),
    "GOOGLE_CALENDAR_ID":      os.environ.get("GOOGLE_CALENDAR_ID",""),
    "NOTION_API_KEY":          os.environ.get("NOTION_API_KEY",""),
    "NOTION_DATABASE_ID":      os.environ.get("NOTION_DATABASE_ID",""),
    "EMAIL_FROM":              os.environ.get("EMAIL_FROM",""),
    "TIMEZONE":                os.environ.get("TIMEZONE",""),
}
for k,v in vars_check.items():
    if v:
        preview = v[:40]+"..." if len(v)>40 else v
        ok(f"{k} = {preview}")
    else:
        err(f"{k} — VACÍA O FALTANTE")

# ─── 2. Parseo del JSON de service account ────────────────────────────────────
sep("2. SERVICE ACCOUNT JSON")
sa_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON","")
sa_data = None
if not sa_json_str:
    err("GOOGLE_CREDENTIALS_JSON está vacía")
else:
    try:
        sa_data = json.loads(sa_json_str)
        ok(f"JSON válido")
        ok(f"type         = {sa_data.get('type')}")
        ok(f"project_id   = {sa_data.get('project_id')}")
        ok(f"client_email = {sa_data.get('client_email')}")
        if sa_data.get("type") != "service_account":
            err("El campo 'type' no es 'service_account'")
    except json.JSONDecodeError as e:
        err(f"JSON inválido: {e}")

# ─── 3. Autenticación Google Calendar ─────────────────────────────────────────
sep("3. AUTENTICACIÓN GOOGLE CALENDAR")
svc = None
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    if sa_data:
        creds = Credentials.from_service_account_info(sa_data, scopes=SCOPES)
        svc = build("calendar", "v3", credentials=creds)
        ok("Service account autenticada correctamente")
    else:
        err("No hay JSON válido para autenticar")
except Exception as e:
    err(f"Error: {e}")

# ─── 4. Calendarios visibles para la service account ─────────────────────────
sep("4. CALENDARIOS VISIBLES PARA LA SERVICE ACCOUNT")
visible_calendars = []
if svc:
    try:
        result = svc.calendarList().list().execute()
        items = result.get("items", [])
        if items:
            for c in items:
                ok(f"{c['id']}  ({c.get('summary','sin nombre')})")
                visible_calendars.append(c["id"])
        else:
            err("La service account NO tiene ningún calendario visible")
            info("Solución: comparte tu calendario con la service account")
            if sa_data:
                info(f"Email: {sa_data.get('client_email')}")
    except Exception as e:
        err(f"Error listando calendarios: {e}")

# ─── 5. Verificar GOOGLE_CALENDAR_ID ─────────────────────────────────────────
sep("5. CONFIGURACIÓN GOOGLE_CALENDAR_ID")
cal_ids_raw = os.environ.get("GOOGLE_CALENDAR_ID","") or os.environ.get("GOOGLE_CALENDAR_IDS","")
if cal_ids_raw:
    cal_ids = [x.strip() for x in cal_ids_raw.split(",") if x.strip()]
    info(f"IDs configurados: {cal_ids}")
    for cid in cal_ids:
        if cid in visible_calendars:
            ok(f"{cid} — VISIBLE para la service account")
        elif cid == "ALL":
            info("Modo ALL — usará todos los calendarios visibles")
        else:
            err(f"{cid} — NO VISIBLE para la service account")
            info("Solución: comparte ese calendario con la service account")
else:
    err("GOOGLE_CALENDAR_ID vacío — el código usará ALL (todos los calendarios visibles)")
    if not visible_calendars:
        err("Pero no hay calendarios visibles, así que leerá 0 eventos")

# ─── 6. Leer eventos reales ───────────────────────────────────────────────────
sep("6. EVENTOS EN LAS PRÓXIMAS 48H")
if svc and visible_calendars:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=48)
    total = 0
    for cid in visible_calendars[:5]:  # máximo 5
        try:
            res = svc.events().list(
                calendarId=cid,
                timeMin=now.isoformat(),
                timeMax=horizon.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=10,
            ).execute()
            events = res.get("items", [])
            info(f"Calendario {cid}: {len(events)} eventos")
            for ev in events[:3]:
                s = ev["start"].get("dateTime", ev["start"].get("date","?"))
                info(f"   · {ev.get('summary','sin nombre')} @ {s}")
            total += len(events)
        except Exception as e:
            err(f"Error leyendo {cid}: {e}")
    ok(f"Total eventos encontrados: {total}") if total else err("0 eventos — calendario vacío o sin acceso")
elif svc:
    err("Sin calendarios visibles — no se puede leer eventos")
else:
    err("Sin autenticación — no se puede leer eventos")

# ─── 7. Crear evento de prueba ────────────────────────────────────────────────
sep("7. CREAR EVENTO DE PRUEBA EN GOOGLE CALENDAR")
if svc and visible_calendars:
    from zoneinfo import ZoneInfo
    tz_name = os.environ.get("TIMEZONE","America/Guayaquil")
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start = now_local.replace(second=0, microsecond=0) + timedelta(minutes=5)
    end   = start + timedelta(minutes=30)

    # Usar el primer calendario visible (o el configurado en GOOGLE_CALENDAR_ID)
    target_cal = cal_ids[0] if cal_ids_raw and cal_ids else visible_calendars[0]
    info(f"Intentando crear evento en: {target_cal}")
    info(f"Horario: {start.strftime('%H:%M')} - {end.strftime('%H:%M')}")

    body = {
        "summary": "[DIAGNÓSTICO] Evento de prueba - borrar",
        "description": "Evento creado por el script de diagnóstico del Asistente.",
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end":   {"dateTime": end.isoformat(),   "timeZone": tz_name},
    }
    try:
        created = svc.events().insert(calendarId=target_cal, body=body).execute()
        ok(f"Evento creado exitosamente: {created.get('id')}")
        ok(f"Revisa tu Google Calendar — debería aparecer en los próximos minutos")
        info(f"Link: {created.get('htmlLink','N/A')}")
    except Exception as e:
        err(f"Error creando evento: {e}")
        info("Posibles causas:")
        info("  · La service account solo tiene acceso de LECTURA (necesita 'Realizar cambios en eventos')")
        info("  · El calendario ID no existe o no está compartido correctamente")
else:
    err("Sin autenticación o sin calendarios — no se puede crear evento de prueba")

# ─── 8. Resumen ──────────────────────────────────────────────────────────────
sep("8. RESUMEN Y PRÓXIMOS PASOS")
if svc and visible_calendars and cal_ids_raw:
    ok("Todo parece configurado correctamente")
    ok("Si el evento de prueba no aparece, verifica el permiso de escritura")
elif svc and not visible_calendars:
    err("PROBLEMA PRINCIPAL: La service account no tiene calendarios visibles")
    print(f"""
  Para arreglarlo:
  1. Ve a calendar.google.com
  2. Click en ⋮ junto a tu calendario → Configuración y uso compartido
  3. Compartir con personas: {sa_data.get('client_email') if sa_data else 'tu service account email'}
  4. Permiso: "Realizar cambios en eventos"
  5. Agrega el secret GOOGLE_CALENDAR_ID = quizhpybravowilsonrafael@gmail.com
""")
elif not svc:
    err("PROBLEMA PRINCIPAL: No se pudo autenticar con Google")
    err("Verifica que GOOGLE_CREDENTIALS_JSON tenga el JSON completo y válido")
