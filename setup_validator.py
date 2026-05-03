#!/usr/bin/env python3
"""
ASISTENTE SETUP VALIDATOR
=========================
Script de validacion interactivo para verificar que todos los secretos
y configuraciones esten correctamente instalados.

Uso: python setup_validator.py
"""

import json
import os
import sys
from pathlib import Path


NOTION_VERSION = "2025-09-03"


def configure_console_encoding():
    """Intenta forzar UTF-8 para evitar errores con Unicode en Windows."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


class Colors:
    """ANSI color codes para terminal."""

    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_header(text):
    """Imprime un encabezado formateado."""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.END}\n")


def print_success(text):
    """Imprime un mensaje de exito."""
    print(f"{Colors.GREEN}✅ {text}{Colors.END}")


def print_error(text):
    """Imprime un mensaje de error."""
    print(f"{Colors.RED}❌ {text}{Colors.END}")


def print_warning(text):
    """Imprime un mensaje de advertencia."""
    print(f"{Colors.YELLOW}⚠️  {text}{Colors.END}")


def print_info(text):
    """Imprime un mensaje informativo."""
    print(f"{Colors.BLUE}ℹ️  {text}{Colors.END}")


def validate_json(json_str):
    """Valida si un string es JSON valido."""
    try:
        json.loads(json_str)
        return True
    except json.JSONDecodeError:
        return False


def normalize_notion_id(raw_id):
    """Normaliza IDs de Notion removiendo guiones y sufijos de URL."""
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


def parse_notion_error(response):
    """Extrae un mensaje util de error desde la respuesta de Notion."""
    try:
        data = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"

    message = data.get("message")
    code = data.get("code")
    if message and code:
        return f"{code}: {message}"
    return message or code or f"HTTP {response.status_code}"


def check_env_variable(key, description, is_json=False):
    """
    Verifica si una variable de entorno esta configurada.

    Returns:
        Tuple (is_valid, value)
    """
    value = os.environ.get(key)

    if not value:
        print_error(f"{description} no configurada")
        return False, None

    if is_json and not validate_json(value):
        print_error(f"{description} - JSON invalido")
        return False, None

    if len(value) > 50:
        display = f"{value[:20]}...{value[-10:]}"
    else:
        display = value

    print_success(f"{description}: {display}")
    return True, value


def validate_google_credentials(json_str):
    """Valida que las credenciales de Google sean correctas."""
    try:
        creds = json.loads(json_str)
        required_fields = [
            "type",
            "project_id",
            "private_key_id",
            "private_key",
            "client_email",
            "client_id",
            "auth_uri",
            "token_uri",
        ]

        missing = [field for field in required_fields if field not in creds]
        if missing:
            print_error(f"Campos faltantes en credenciales: {', '.join(missing)}")
            return False

        if creds.get("type") != "service_account":
            print_error("Las credenciales deben ser de tipo 'service_account'")
            return False

        print_success(
            f"Credenciales de Google validas (Service Account: {creds['client_email']})"
        )
        return True
    except Exception as exc:
        print_error(f"Error validando credenciales: {exc}")
        return False


def validate_gmail_credentials():
    """Valida conexion SMTP a Gmail."""
    import smtplib

    email_from = os.environ.get("EMAIL_FROM")
    password = os.environ.get("EMAIL_PASSWORD")
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))

    if not email_from or not password:
        print_warning("EMAIL_FROM o EMAIL_PASSWORD no configurados")
        return False

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(email_from, password)
        server.quit()

        print_success(f"Conexion SMTP a Gmail verificada ({email_from})")
        return True
    except smtplib.SMTPAuthenticationError:
        print_error("Credenciales de Gmail invalidas (usuario/contrasena)")
        return False
    except Exception as exc:
        print_error(f"Error conectando a Gmail: {exc}")
        return False


def notion_get(requests_module, headers, endpoint, object_name):
    """Hace una peticion GET a Notion y devuelve la respuesta."""
    response = requests_module.get(
        f"https://api.notion.com/v1/{endpoint}",
        headers=headers,
        timeout=10,
    )
    if response.status_code == 200:
        print_success(f"{object_name} accesible por API")
    return response


def validate_notion_api(api_key, database_id, output_page_id=None):
    """Valida acceso a Notion aceptando database_id o data_source_id."""
    import requests

    normalized_database_id = normalize_notion_id(database_id)
    normalized_output_page_id = normalize_notion_id(output_page_id)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    try:
        db_res = notion_get(
            requests,
            headers,
            f"databases/{normalized_database_id}",
            "Database",
        )

        is_data_source = False
        if db_res.status_code == 404:
            print_warning(
                "Ese ID no respondio como database. Intentando validarlo como data source..."
            )
            ds_res = notion_get(
                requests,
                headers,
                f"data_sources/{normalized_database_id}",
                "Data source",
            )
            if ds_res.status_code != 200:
                print_error(
                    "No se pudo acceder ni como database ni como data source: "
                    f"{parse_notion_error(ds_res)}"
                )
                return False
            is_data_source = True
        elif db_res.status_code != 200:
            print_error(f"Error validando Database: {parse_notion_error(db_res)}")
            return False

        if normalized_output_page_id:
            page_res = notion_get(
                requests,
                headers,
                f"pages/{normalized_output_page_id}",
                "Pagina de salida",
            )
            if page_res.status_code != 200:
                print_error(
                    "La pagina de salida no es accesible: "
                    f"{parse_notion_error(page_res)}"
                )
                return False

        if is_data_source:
            print_success(
                "Notion configurado correctamente (el ID de entrada corresponde a un data source)"
            )
        else:
            print_success(
                "Notion configurado correctamente (el ID de entrada corresponde a una database)"
            )
        return True
    except Exception as exc:
        print_error(f"Fallo de conexion con Notion: {exc}")
        return False


def load_env_file(filepath=".env"):
    """Carga variables desde un archivo .env."""
    try:
        if Path(filepath).exists():
            from dotenv import load_dotenv

            load_dotenv(filepath)
            print_success(f"Archivo {filepath} cargado")
            return True
        return False
    except ImportError:
        print_warning("python-dotenv no instalado")
        return False


def main():
    """Funcion principal de validacion."""
    configure_console_encoding()
    print_header("🤖 ASISTENTE - Setup Validator")

    print_info("Intentando cargar archivo .env...")
    load_env_file()

    validations = [
        ("GOOGLE_CREDENTIALS_JSON", "Google Calendar Credentials", True),
        ("GOOGLE_CALENDAR_IDS", "Google Calendar IDs", False),
        ("NOTION_API_KEY", "Notion API Key", False),
        ("NOTION_DATABASE_ID", "Notion Database ID", False),
        ("NOTION_OUTPUT_PAGE_ID", "Notion Output Page ID", False),
        ("SMTP_SERVER", "SMTP Server", False),
        ("SMTP_PORT", "SMTP Port", False),
        ("EMAIL_FROM", "Email From", False),
        ("EMAIL_PASSWORD", "Email Password", False),
        ("EMAIL_TO", "Email To", False),
    ]

    print_header("📋 VERIFICANDO VARIABLES DE ENTORNO")

    results = {}
    google_creds = None

    for var_name, description, is_json in validations:
        is_valid, value = check_env_variable(var_name, description, is_json)
        results[var_name] = is_valid
        if var_name == "GOOGLE_CREDENTIALS_JSON" and value:
            google_creds = value

    print_header("🔐 VALIDACIONES DE CONECTIVIDAD")

    print_info("Validando credenciales de Google...")
    if google_creds:
        results["google_creds"] = validate_google_credentials(google_creds)
    else:
        print_warning("No se puede validar Google (credenciales no configuradas)")
        results["google_creds"] = False

    print_info("Validando conexion SMTP a Gmail...")
    results["gmail_smtp"] = validate_gmail_credentials()

    print_info("Validando acceso a Notion API...")
    notion_api = os.environ.get("NOTION_API_KEY")
    notion_db = os.environ.get("NOTION_DATABASE_ID")
    notion_output_page = os.environ.get("NOTION_OUTPUT_PAGE_ID")
    results["notion_api"] = validate_notion_api(
        notion_api,
        notion_db,
        notion_output_page,
    )

    print_header("📊 RESUMEN DE VALIDACION")

    all_valid = all(results.values())
    if all_valid:
        print_success("TODAS LAS CONFIGURACIONES SON VALIDAS")
        print_success("El sistema Asistente esta listo para ejecutarse")
        print_info("\nProximos pasos:")
        print_info("1. Ejecutar el programa principal")
        print_info("2. Revisar el email generado")
        print_info("3. Confirmar la escritura en la pagina de Notion")
        return 0

    print_error("ALGUNAS CONFIGURACIONES NO SON VALIDAS")
    print_error("\nVariables faltantes o invalidas:")
    for key, valid in results.items():
        if not valid:
            print_error(f"  - {key}")

    print_warning("\nRevisa la documentacion en ASISTENTE_README.md")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n❌ Validacion cancelada por el usuario")
        sys.exit(1)
