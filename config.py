import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

DB_PATH = os.getenv("DB_PATH", "bot.db")

COLOR_PRINCIPAL = 0x2B2D31
COLOR_EXITO = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_AVISO = 0xF1C40F
COLOR_INFO = 0x5865F2

TEXTO_TICKET_DEFAULT = (
    "Bienvenido a tu ticket.\n\n"
    "Cuando quieras reclamar una reseña, usa el botón correspondiente. "
    "Se te asignará una reseña disponible junto con las instrucciones a seguir.\n\n"
    "Una vez completada, usa el botón de enviar pruebas para adjuntar el enlace "
    "que demuestre que realizaste la reseña correctamente."
)
