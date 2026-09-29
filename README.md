Bot de reseñas para Discord

Instalación

Necesitas Python 3.10 o superior.

    pip install -r requirements.txt
    python bot.py

Variables de entorno (en un .env o en el panel del hosting):

    DISCORD_TOKEN=el token de tu bot
    DB_PATH=ruta de la base de datos (opcional, por defecto bot.db)

En el portal de desarrolladores de Discord activa Server Members Intent e invita
el bot con los ámbitos bot y applications.commands.

En Railway el disco se borra en cada despliegue. Crea un Volume montado en /data
y pon DB_PATH=/data/bot.db para no perder saldos ni reseñas. También puedes
descargar una copia con /backup cuando quieras.

Primeros pasos en el servidor

1. /config para elegir la categoría de tickets, el canal de verificación y el de logs.
2. /plantilla-reseña para crear una plantilla (nombre, recompensa e instrucciones).
3. /restock-panel y pegar los enlaces de las reseñas, uno por línea.
4. /panel en el canal donde quieras el botón para abrir tickets.

Comandos para todos

    /saldo /metodo-pago /movimientos /mis-reseñas /tiempo-restante
    /reportar /stock /top /ayuda

Comandos del staff (permiso Gestionar servidor)

    /config /panel /mensaje-ticket /pausar-tickets
    /plantilla-reseña /editar-plantilla /borrar-plantilla /plantillas
    /restock-panel /historial /reseña-info /eliminar-reseña
    /reset-cooldown /recordatorio
    /addsaldo /quitarsaldo /editar-saldo /pagar
    /blacklist agregar | quitar | lista
    /staff-stats /exportar /backup

Cómo funciona

El usuario abre un ticket, pulsa Reclamar reseña y el bot le da un enlace del stock.
Cuando termina, pulsa Enviar pruebas y pega su enlace. La prueba llega al canal de
verificación con los botones Aprobar y Rechazar. Si se aprueba, la reseña sale del
stock y se suma el saldo. Si se rechaza, vuelve al stock. En los dos casos el bot
avisa por mensaje directo.

Si el usuario no manda pruebas a tiempo o cierra el ticket, la reseña vuelve al stock
sola. Los tickets sin actividad durante 48 horas se cierran automáticamente.
