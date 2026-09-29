import os
import re
import tempfile

import discord
from discord import app_commands
from discord.ext import commands

import database as db
from utils import es_staff, solo_staff, es_miembro_staff, enviar_log, fmt_monto
from config import COLOR_INFO, COLOR_PRINCIPAL, COLOR_AVISO, COLOR_ERROR

URL_REGEX = re.compile(r"https?://[^\s<>\"']+")


class RestockModal(discord.ui.Modal, title="Restock de reseñas"):
    instrucciones = discord.ui.TextInput(
        label="Instrucciones para el usuario",
        style=discord.TextStyle.paragraph,
        placeholder="Ej: entra al link, deja 5 estrellas y un comentario",
        max_length=1000,
    )
    valor = discord.ui.TextInput(
        label="Recompensa por reseña",
        placeholder="Ej: 2.5",
        max_length=12,
    )
    enlaces = discord.ui.TextInput(
        label="Enlaces (uno por línea)",
        style=discord.TextStyle.paragraph,
        placeholder="Pega aquí los enlaces, uno por línea",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            valor = float(self.valor.value.strip().replace(",", "."))
        except ValueError:
            await interaction.response.send_message("La recompensa debe ser un número.", ephemeral=True)
            return
        if valor < 0:
            await interaction.response.send_message("La recompensa no puede ser negativa.", ephemeral=True)
            return

        links = [l.rstrip(".,;)") for l in URL_REGEX.findall(self.enlaces.value)]
        if not links:
            await interaction.response.send_message(
                "No encontré ningún enlace en el mensaje.", ephemeral=True
            )
            return

        agregados = await db.agregar_stock(
            interaction.guild_id, self.instrucciones.value.strip(), valor, links
        )
        repetidos = len(set(links)) - agregados
        total = await db.total_disponibles(interaction.guild_id)

        texto = f"Se agregaron {agregados} reseñas al stock. Hay {total} disponibles en total."
        if repetidos > 0:
            texto += f" Omití {repetidos} que ya estaban en stock."
        await interaction.response.send_message(
            embed=discord.Embed(title="Restock realizado", description=texto, color=COLOR_INFO),
            ephemeral=True,
        )
        await enviar_log(
            interaction.guild, "Restock",
            campos=[("Staff", interaction.user.mention), ("Agregadas", agregados),
                    ("Recompensa", fmt_monto(valor))],
        )


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    blacklist = app_commands.Group(
        name="blacklist",
        description="Gestiona la lista negra",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @app_commands.command(name="config", description="Configura el sistema (sin opciones muestra lo actual)")
    @app_commands.describe(
        categoria_tickets="Categoría donde se crearán los tickets",
        canal_verificacion="Canal donde el staff verifica las pruebas",
        canal_logs="Canal de registros y avisos",
        horas_limite="Horas para enviar pruebas antes de liberar la reseña (0 = sin límite)",
        alerta_stock="Avisar en logs cuando queden esta cantidad de reseñas",
    )
    @es_staff()
    async def config(
        self,
        interaction: discord.Interaction,
        categoria_tickets: discord.CategoryChannel = None,
        canal_verificacion: discord.TextChannel = None,
        canal_logs: discord.TextChannel = None,
        horas_limite: int = None,
        alerta_stock: int = None,
    ):
        cambios = {}
        if categoria_tickets:
            cambios["ticket_category_id"] = categoria_tickets.id
        if canal_verificacion:
            cambios["verification_channel_id"] = canal_verificacion.id
        if canal_logs:
            cambios["log_channel_id"] = canal_logs.id
        if horas_limite is not None:
            if horas_limite < 0 or horas_limite > 720:
                await interaction.response.send_message("Las horas deben estar entre 0 y 720.", ephemeral=True)
                return
            cambios["cooldown_horas"] = horas_limite
        if alerta_stock is not None:
            if alerta_stock < 0:
                await interaction.response.send_message("La alerta no puede ser negativa.", ephemeral=True)
                return
            cambios["stock_alert_threshold"] = alerta_stock

        if cambios:
            await db.update_config(interaction.guild_id, **cambios)

        cfg = await db.get_config(interaction.guild_id)
        embed = discord.Embed(
            title="Configuración actualizada" if cambios else "Configuración actual",
            color=COLOR_INFO,
        )
        embed.add_field(name="Categoría de tickets",
                        value=f"<#{cfg['ticket_category_id']}>" if cfg["ticket_category_id"] else "No configurada")
        embed.add_field(name="Canal de verificación",
                        value=f"<#{cfg['verification_channel_id']}>" if cfg["verification_channel_id"] else "No configurado")
        embed.add_field(name="Canal de logs",
                        value=f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "No configurado")
        embed.add_field(name="Tiempo límite",
                        value=f"{cfg['cooldown_horas']} horas" if cfg["cooldown_horas"] else "Sin límite")
        embed.add_field(name="Alerta de stock", value=f"Al llegar a {cfg['stock_alert_threshold']}")
        embed.add_field(name="Tickets", value="Pausados" if cfg["tickets_paused"] else "Abiertos")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="restock-panel", description="Abre el panel para cargar reseñas al stock")
    @es_staff()
    async def restock_panel(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RestockModal())

    @app_commands.command(name="stock", description="Muestra cuántas reseñas hay disponibles")
    @app_commands.guild_only()
    async def stock(self, interaction: discord.Interaction):
        filas = await db.contar_stock(interaction.guild_id)
        estados = await db.contar_estados(interaction.guild_id)
        embed = discord.Embed(title="Stock de reseñas", color=COLOR_PRINCIPAL)
        embed.add_field(name="Disponibles", value=str(estados.get("disponible", 0)))
        embed.add_field(name="En curso", value=str(estados.get("reclamada", 0)))
        embed.add_field(name="En verificación", value=str(estados.get("pendiente_verificacion", 0)))
        if filas:
            embed.add_field(
                name="Detalle",
                value="\n".join(
                    f"{f['cantidad']}x - {fmt_monto(f['valor'])} - {f['instrucciones'][:60]}"
                    for f in filas
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    async def revisar_aviso_stock(self, guild: discord.Guild):
        cfg = await db.get_config(guild.id)
        total = await db.total_disponibles(guild.id)
        if total == 0:
            await enviar_log(guild, "Stock agotado", "No quedan reseñas disponibles.", COLOR_ERROR)
        elif total == cfg["stock_alert_threshold"]:
            await enviar_log(guild, "Stock bajo", f"Quedan {total} reseñas disponibles.", COLOR_AVISO)

    @blacklist.command(name="agregar", description="Bloquea a un usuario para reclamar reseñas")
    @app_commands.describe(usuario="Usuario", razon="Motivo del bloqueo")
    @solo_staff()
    async def blacklist_agregar(self, interaction: discord.Interaction, usuario: discord.Member, razon: str):
        await db.agregar_blacklist(interaction.guild_id, usuario.id, razon, interaction.user.id)
        await interaction.response.send_message(
            f"{usuario.mention} fue agregado a la lista negra.", ephemeral=True
        )
        await enviar_log(
            interaction.guild, "Usuario en lista negra", color=COLOR_ERROR,
            campos=[("Usuario", usuario.mention), ("Staff", interaction.user.mention), ("Motivo", razon)],
        )

    @blacklist.command(name="quitar", description="Quita a un usuario de la lista negra")
    @app_commands.describe(usuario="Usuario")
    @solo_staff()
    async def blacklist_quitar(self, interaction: discord.Interaction, usuario: discord.Member):
        if await db.quitar_blacklist(interaction.guild_id, usuario.id):
            await interaction.response.send_message(
                f"{usuario.mention} fue quitado de la lista negra.", ephemeral=True
            )
        else:
            await interaction.response.send_message("Ese usuario no está en la lista negra.", ephemeral=True)

    @blacklist.command(name="lista", description="Muestra los usuarios bloqueados")
    @solo_staff()
    async def blacklist_lista(self, interaction: discord.Interaction):
        filas = await db.listar_blacklist(interaction.guild_id)
        if not filas:
            await interaction.response.send_message("La lista negra está vacía.", ephemeral=True)
            return
        texto = "\n".join(f"<@{f['user_id']}> - {f['reason']}" for f in filas)
        await interaction.response.send_message(
            embed=discord.Embed(title="Lista negra", description=texto[:4000], color=COLOR_ERROR),
            ephemeral=True,
        )

    @app_commands.command(name="staff-stats", description="Reseñas gestionadas por cada miembro del staff")
    @es_staff()
    async def staff_stats(self, interaction: discord.Interaction):
        filas = await db.staff_stats(interaction.guild_id)
        if not filas:
            await interaction.response.send_message("Todavía no hay datos.", ephemeral=True)
            return
        texto = "\n".join(
            f"<@{f['staff_id']}> - Aprobadas: {f['aprobadas'] or 0} | Rechazadas: {f['rechazadas'] or 0}"
            for f in filas
        )
        await interaction.response.send_message(
            embed=discord.Embed(title="Estadísticas del staff", description=texto, color=COLOR_INFO),
            ephemeral=True,
        )

    @app_commands.command(name="backup", description="Descarga una copia de seguridad de la base de datos")
    @es_staff()
    async def backup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = os.path.join(carpeta, "respaldo.db")
            await db.hacer_backup(ruta)
            await interaction.followup.send(
                "Copia de seguridad generada. Guárdala en un lugar seguro.",
                file=discord.File(ruta, filename="respaldo.db"),
                ephemeral=True,
            )

    @app_commands.command(name="ayuda", description="Lista de comandos disponibles")
    @app_commands.guild_only()
    async def ayuda(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Comandos", color=COLOR_PRINCIPAL)
        embed.add_field(
            name="Usuarios",
            value=(
                "/saldo - ver tu saldo\n"
                "/metodo-pago - cómo quieres que te paguen\n"
                "/movimientos - tus movimientos de saldo\n"
                "/mis-reseñas - tu historial de reseñas\n"
                "/tiempo-restante - tiempo para enviar pruebas\n"
                "/reportar - reportar un problema con una reseña\n"
                "/stock - reseñas disponibles\n"
                "/top - ranking de reseñas aprobadas"
            ),
            inline=False,
        )
        if es_miembro_staff(interaction.user):
            embed.add_field(
                name="Staff",
                value=(
                    "/config - canales, categoría, tiempo límite y alerta de stock\n"
                    "/panel - publicar el panel de tickets\n"
                    "/mensaje-ticket - editar el texto de bienvenida\n"
                    "/pausar-tickets - pausar o reanudar tickets\n"
                    "/restock-panel - cargar reseñas al stock\n"
                    "/historial, /reseña-info, /eliminar-reseña\n"
                    "/reset-cooldown, /recordatorio\n"
                    "/addsaldo, /quitarsaldo, /editar-saldo, /pagar\n"
                    "/blacklist agregar | quitar | lista\n"
                    "/staff-stats, /exportar, /backup"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
