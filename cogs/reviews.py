import csv
import datetime
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database as db
from utils import es_staff, es_miembro_staff, enviar_log, mandar_dm, fmt_monto
from config import COLOR_EXITO, COLOR_ERROR, COLOR_INFO, COLOR_AVISO, COLOR_PRINCIPAL

log = logging.getLogger("bot")

ESTADOS_LEGIBLES = {
    "disponible": "Disponible",
    "reclamada": "Reclamada, pendiente de pruebas",
    "pendiente_verificacion": "En verificación",
    "aprobada": "Aprobada",
}

ACCIONES_LEGIBLES = {
    "aprobada": "Aprobada",
    "rechazada": "Rechazada",
    "liberada": "Liberada por el staff",
    "vencida": "Vencida por tiempo",
    "ticket_cerrado": "Ticket cerrado sin enviar pruebas",
}


def _extraer_review_id(embed) -> int:
    texto = embed.footer.text if embed.footer and embed.footer.text else embed.title
    return int(texto.split("#")[-1].strip())


def _fecha(valor: str) -> str:
    return discord.utils.format_dt(db.parse_fecha(valor), "d")


async def iniciar_reclamo_resena(interaction: discord.Interaction):
    guild = interaction.guild
    ticket = await db.get_ticket_por_canal(interaction.channel_id)
    if ticket is None or ticket["status"] != "abierto":
        await interaction.response.send_message("Este canal no es un ticket activo.", ephemeral=True)
        return
    if ticket["user_id"] != interaction.user.id:
        await interaction.response.send_message(
            "Solo quien abrió el ticket puede reclamar una reseña.", ephemeral=True
        )
        return
    if await db.esta_en_blacklist(guild.id, interaction.user.id):
        await interaction.response.send_message(
            "No puedes reclamar reseñas: estás en la lista negra.", ephemeral=True
        )
        return

    estado, review = await db.reclamar_review(guild.id, interaction.user.id, interaction.channel_id)
    if estado == "activa":
        await interaction.response.send_message(
            "Ya tienes una reseña reclamada. Termínala antes de pedir otra.", ephemeral=True
        )
        return
    if estado == "sin_stock":
        await interaction.response.send_message(
            "No hay reseñas disponibles en este momento. Inténtalo más tarde.", ephemeral=True
        )
        return

    plantilla = await db.get_plantilla(review["plantilla_id"]) if review["plantilla_id"] else None
    instrucciones = plantilla["instrucciones"] if plantilla else "Sigue las indicaciones del staff."
    valor = plantilla["valor"] if plantilla else 0
    cfg = await db.get_config(guild.id)

    embed = discord.Embed(
        title=f"Reseña asignada #{review['id']}", description=instrucciones, color=COLOR_INFO
    )
    embed.add_field(name="Enlace de la reseña", value=review["link"] or "No disponible", inline=False)
    embed.add_field(name="Recompensa", value=fmt_monto(valor))
    if cfg["cooldown_horas"] > 0:
        embed.add_field(name="Tiempo límite", value=f"{cfg['cooldown_horas']} horas")
    embed.set_footer(text="Cuando termines, pulsa el botón Enviar pruebas.")
    await interaction.response.send_message(embed=embed)

    await enviar_log(
        guild,
        "Reseña reclamada",
        campos=[("Reseña", f"#{review['id']}"), ("Usuario", interaction.user.mention)],
    )
    admin = interaction.client.get_cog("Admin")
    if admin is not None:
        await admin.revisar_aviso_stock(guild)


class EnviarPruebasModal(discord.ui.Modal):
    def __init__(self, review_id: int):
        super().__init__(title="Enviar pruebas")
        self.review_id = review_id
        self.link = discord.ui.TextInput(
            label="Enlace de la prueba", placeholder="https://...", max_length=500
        )
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction):
        link = self.link.value.strip()
        if not link.lower().startswith(("http://", "https://")):
            await interaction.response.send_message(
                "El enlace no es válido. Debe empezar con http:// o https://", ephemeral=True
            )
            return

        cfg = await db.get_config(interaction.guild_id)
        canal_verificacion = (
            interaction.guild.get_channel(cfg["verification_channel_id"])
            if cfg["verification_channel_id"] else None
        )
        if canal_verificacion is None:
            await interaction.response.send_message(
                "El canal de verificación no está configurado. Avisa al staff.", ephemeral=True
            )
            return

        resultado = await db.enviar_prueba(self.review_id, interaction.user.id, link)
        if resultado == "duplicada":
            await interaction.response.send_message(
                "Ese enlace ya fue enviado como prueba en otra reseña.", ephemeral=True
            )
            return
        if resultado == "invalida":
            await interaction.response.send_message(
                "Esta reseña ya no está esperando pruebas.", ephemeral=True
            )
            return

        review = await db.get_review(self.review_id)
        plantilla = await db.get_plantilla(review["plantilla_id"]) if review["plantilla_id"] else None

        embed = discord.Embed(title=f"Verificación de reseña #{review['id']}", color=COLOR_AVISO)
        embed.add_field(name="Usuario", value=interaction.user.mention)
        embed.add_field(name="Plantilla", value=plantilla["nombre"] if plantilla else "N/A")
        embed.add_field(name="Ticket", value=interaction.channel.mention)
        embed.add_field(name="Reseña asignada", value=review["link"] or "N/A", inline=False)
        embed.add_field(name="Prueba enviada", value=link, inline=False)

        try:
            await canal_verificacion.send(embed=embed, view=VerificationView())
        except discord.HTTPException:
            await db.deshacer_prueba(self.review_id)
            await interaction.response.send_message(
                "No pude enviar tus pruebas al canal de verificación. Avisa al staff.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Pruebas enviadas",
                description="El staff revisará tu reseña. Te avisaremos por mensaje directo.",
                color=COLOR_EXITO,
            )
        )


async def abrir_modal_pruebas(interaction: discord.Interaction):
    review = await db.resena_activa_de_usuario(interaction.guild_id, interaction.user.id)
    if review is None or review["ticket_channel_id"] != interaction.channel_id:
        await interaction.response.send_message(
            "No tienes ninguna reseña reclamada en este ticket.", ephemeral=True
        )
        return
    if review["estado"] == "pendiente_verificacion":
        await interaction.response.send_message(
            "Tus pruebas ya fueron enviadas y están en verificación.", ephemeral=True
        )
        return
    await interaction.response.send_modal(EnviarPruebasModal(review["id"]))


class MotivoRechazoModal(discord.ui.Modal):
    def __init__(self, mensaje: discord.Message, review_id: int):
        super().__init__(title="Motivo del rechazo")
        self.mensaje = mensaje
        self.review_id = review_id
        self.motivo = discord.ui.TextInput(
            label="¿Por qué se rechaza?",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
        )
        self.add_item(self.motivo)

    async def on_submit(self, interaction: discord.Interaction):
        motivo = self.motivo.value.strip() or "No especificado"
        previa = await db.liberar_review(
            self.review_id, interaction.user.id, motivo, "rechazada", ("pendiente_verificacion",)
        )
        if previa is None:
            await interaction.response.send_message("Esta reseña ya fue procesada.", ephemeral=True)
            return

        embed = self.mensaje.embeds[0]
        embed.color = COLOR_ERROR
        embed.add_field(
            name="Resultado",
            value=f"Rechazada por {interaction.user.mention}. Motivo: {motivo}",
            inline=False,
        )
        await self.mensaje.edit(embed=embed, view=None)
        await interaction.response.send_message("Reseña rechazada y devuelta al stock.", ephemeral=True)

        aviso = discord.Embed(
            title="Reseña no aprobada",
            description=(
                f"Tu reseña #{self.review_id} no cumplió con los requisitos.\n"
                "La reseña volvió al stock. Puedes reclamar otra desde tu ticket."
            ),
            color=COLOR_ERROR,
        )
        aviso.add_field(name="Motivo", value=motivo, inline=False)
        await mandar_dm(interaction.client, previa["claimed_by"], aviso)
        canal = interaction.guild.get_channel(previa["ticket_channel_id"]) if previa["ticket_channel_id"] else None
        if canal is not None:
            try:
                await canal.send(f"<@{previa['claimed_by']}>", embed=aviso)
            except discord.HTTPException:
                pass

        await enviar_log(
            interaction.guild,
            "Reseña rechazada",
            color=COLOR_ERROR,
            campos=[("Reseña", f"#{self.review_id}"), ("Staff", interaction.user.mention), ("Motivo", motivo)],
        )


class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Aprobar", style=discord.ButtonStyle.success, custom_id="verif:aprobar")
    async def aprobar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not es_miembro_staff(interaction.user):
            await interaction.response.send_message("No tienes permiso.", ephemeral=True)
            return

        review_id = _extraer_review_id(interaction.message.embeds[0])
        review = await db.aprobar_review(review_id, interaction.user.id)
        if review is None:
            await interaction.response.send_message("Esta reseña ya fue procesada.", ephemeral=True)
            return

        plantilla = await db.get_plantilla(review["plantilla_id"]) if review["plantilla_id"] else None
        valor = plantilla["valor"] if plantilla else 0
        saldo_nuevo = await db.get_balance(interaction.guild_id, review["claimed_by"])
        if valor > 0:
            _, saldo_nuevo = await db.ajustar_saldo(
                interaction.guild_id, review["claimed_by"], valor,
                "reseña aprobada", interaction.user.id, f"Reseña #{review_id}",
            )

        embed = interaction.message.embeds[0]
        embed.color = COLOR_EXITO
        embed.add_field(name="Resultado", value=f"Aprobada por {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)

        aviso = discord.Embed(
            title="Reseña aprobada",
            description=(
                f"Tu reseña #{review_id} fue aprobada.\n"
                f"Recompensa: {fmt_monto(valor)}\nSaldo actual: {fmt_monto(saldo_nuevo)}"
            ),
            color=COLOR_EXITO,
        )
        await mandar_dm(interaction.client, review["claimed_by"], aviso)
        canal = interaction.guild.get_channel(review["ticket_channel_id"]) if review["ticket_channel_id"] else None
        if canal is not None:
            try:
                await canal.send(f"<@{review['claimed_by']}>", embed=aviso)
            except discord.HTTPException:
                pass

        await enviar_log(
            interaction.guild,
            "Reseña aprobada",
            color=COLOR_EXITO,
            campos=[("Reseña", f"#{review_id}"), ("Staff", interaction.user.mention),
                    ("Usuario", f"<@{review['claimed_by']}>"), ("Recompensa", fmt_monto(valor))],
        )

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, custom_id="verif:rechazar")
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not es_miembro_staff(interaction.user):
            await interaction.response.send_message("No tienes permiso.", ephemeral=True)
            return
        review_id = _extraer_review_id(interaction.message.embeds[0])
        await interaction.response.send_modal(MotivoRechazoModal(interaction.message, review_id))


class ConfirmarEliminarView(discord.ui.View):
    def __init__(self, review, autor_id: int, valor: float):
        super().__init__(timeout=60)
        self.review = review
        self.autor_id = autor_id
        self.valor = valor

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message("Este menú no es tuyo.", ephemeral=True)
            return False
        return True

    async def _eliminar(self, interaction: discord.Interaction, descontar: bool):
        review_id = self.review["id"]
        await db.eliminar_review(review_id)
        texto = f"La reseña #{review_id} fue eliminada del historial."
        if descontar and self.valor > 0:
            _, nuevo = await db.ajustar_saldo(
                interaction.guild_id, self.review["claimed_by"], -self.valor,
                "reseña eliminada", interaction.user.id, f"Reseña #{review_id}",
            )
            texto += f"\nSe descontaron {fmt_monto(self.valor)} de su saldo (ahora: {fmt_monto(nuevo)})."
        await interaction.response.edit_message(content=texto, embed=None, view=None)
        await enviar_log(
            interaction.guild,
            "Reseña eliminada",
            color=COLOR_AVISO,
            campos=[("Reseña", f"#{review_id}"), ("Staff", interaction.user.mention),
                    ("Usuario", f"<@{self.review['claimed_by']}>"),
                    ("Saldo descontado", "Sí" if descontar else "No")],
        )
        self.stop()

    @discord.ui.button(label="Eliminar y descontar saldo", style=discord.ButtonStyle.danger)
    async def con_saldo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._eliminar(interaction, True)

    @discord.ui.button(label="Eliminar solo del historial", style=discord.ButtonStyle.secondary)
    async def sin_saldo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._eliminar(interaction, False)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Eliminación cancelada.", embed=None, view=None)
        self.stop()


class Reviews(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.vencimientos.start()

    async def cog_unload(self):
        self.vencimientos.cancel()

    @tasks.loop(minutes=10)
    async def vencimientos(self):
        try:
            for guild in self.bot.guilds:
                cfg = await db.get_config(guild.id)
                for review in await db.reviews_vencidas(guild.id, cfg["cooldown_horas"]):
                    previa = await db.liberar_review(
                        review["id"], None, "Tiempo agotado", "vencida", ("reclamada",)
                    )
                    if previa is None:
                        continue
                    aviso = discord.Embed(
                        title="Reseña vencida",
                        description=(
                            f"Se agotó el tiempo para la reseña #{review['id']} y volvió al stock. "
                            "Puedes reclamar otra desde tu ticket."
                        ),
                        color=COLOR_AVISO,
                    )
                    await mandar_dm(self.bot, previa["claimed_by"], aviso)
                    canal = guild.get_channel(previa["ticket_channel_id"]) if previa["ticket_channel_id"] else None
                    if canal is not None:
                        try:
                            await canal.send(f"<@{previa['claimed_by']}>", embed=aviso)
                        except discord.HTTPException:
                            pass
                    await enviar_log(
                        guild, "Reseña vencida", color=COLOR_AVISO,
                        campos=[("Reseña", f"#{review['id']}"), ("Usuario", f"<@{previa['claimed_by']}>")],
                    )
        except Exception:
            log.exception("Error revisando reseñas vencidas")

    @vencimientos.before_loop
    async def antes_vencimientos(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="mis-reseñas", description="Ver tu historial de reseñas")
    @app_commands.guild_only()
    async def mis_resenas(self, interaction: discord.Interaction):
        conteos, recientes = await db.historial_usuario(interaction.guild_id, interaction.user.id)
        activa = await db.resena_activa_de_usuario(interaction.guild_id, interaction.user.id)

        embed = discord.Embed(title="Tus reseñas", color=COLOR_INFO)
        embed.add_field(name="Aprobadas", value=str(conteos.get("aprobada", 0)))
        embed.add_field(name="Rechazadas", value=str(conteos.get("rechazada", 0)))
        embed.add_field(name="Vencidas", value=str(conteos.get("vencida", 0)))

        if activa is not None:
            embed.add_field(
                name=f"En curso: reseña #{activa['id']}",
                value=f"{ESTADOS_LEGIBLES.get(activa['estado'], activa['estado'])}\n{activa['link'] or ''}",
                inline=False,
            )
        if recientes:
            lineas = [
                f"#{r['review_id']} - {ACCIONES_LEGIBLES.get(r['accion'], r['accion'])} - {_fecha(r['fecha'])}"
                for r in recientes
            ]
            embed.add_field(name="Últimos movimientos", value="\n".join(lineas), inline=False)
        if not recientes and activa is None:
            embed.description = "Todavía no tienes reseñas registradas."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="top", description="Ranking de usuarios con más reseñas aprobadas")
    @app_commands.guild_only()
    async def top(self, interaction: discord.Interaction):
        filas = await db.top_usuarios(interaction.guild_id)
        if not filas:
            await interaction.response.send_message("Todavía no hay datos.", ephemeral=True)
            return
        texto = "\n".join(f"{i + 1}. <@{f['user_id']}> - {f['aprobadas']}" for i, f in enumerate(filas))
        embed = discord.Embed(title="Top de reseñas aprobadas", description=texto, color=COLOR_PRINCIPAL)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reportar", description="Reporta un problema con una de tus reseñas")
    @app_commands.describe(id="Número de la reseña", motivo="Describe el problema")
    @app_commands.guild_only()
    async def reportar(self, interaction: discord.Interaction, id: int, motivo: str):
        review = await db.get_review(id)
        if review is None or review["claimed_by"] != interaction.user.id:
            await interaction.response.send_message(
                "No tienes ninguna reseña con ese número.", ephemeral=True
            )
            return
        await db.crear_reporte(interaction.guild_id, id, interaction.user.id, motivo)
        await enviar_log(
            interaction.guild,
            "Nuevo reporte",
            color=COLOR_AVISO,
            campos=[("Reseña", f"#{id}"), ("Usuario", interaction.user.mention), ("Motivo", motivo)],
        )
        await interaction.response.send_message("Tu reporte fue enviado al staff.", ephemeral=True)

    @app_commands.command(
        name="tiempo-restante", description="Cuánto tiempo te queda para enviar tus pruebas"
    )
    @app_commands.guild_only()
    async def tiempo_restante(self, interaction: discord.Interaction):
        review = await db.resena_activa_de_usuario(interaction.guild_id, interaction.user.id)
        if review is None:
            await interaction.response.send_message(
                "No tienes ninguna reseña reclamada actualmente.", ephemeral=True
            )
            return
        if review["estado"] == "pendiente_verificacion":
            await interaction.response.send_message(
                "Tus pruebas ya fueron enviadas y están en verificación.", ephemeral=True
            )
            return
        cfg = await db.get_config(interaction.guild_id)
        if cfg["cooldown_horas"] <= 0:
            await interaction.response.send_message(
                "Tu reseña no tiene tiempo límite.", ephemeral=True
            )
            return

        limite = db.parse_fecha(review["claimed_at"]) + datetime.timedelta(hours=cfg["cooldown_horas"])
        restante = limite - datetime.datetime.now(datetime.timezone.utc)
        if restante.total_seconds() <= 0:
            await interaction.response.send_message(
                "Tu tiempo se agotó. La reseña se liberará en unos minutos.", ephemeral=True
            )
            return
        horas = int(restante.total_seconds() // 3600)
        minutos = int((restante.total_seconds() % 3600) // 60)
        await interaction.response.send_message(
            f"Te quedan {horas} h {minutos} min para enviar las pruebas.", ephemeral=True
        )

    @app_commands.command(name="historial", description="Historial completo de un usuario")
    @app_commands.describe(usuario="Usuario a consultar")
    @es_staff()
    async def historial(self, interaction: discord.Interaction, usuario: discord.Member):
        guild_id = interaction.guild_id
        conteos, recientes = await db.historial_usuario(guild_id, usuario.id)
        saldo = await db.get_balance(guild_id, usuario.id)
        metodo = await db.get_metodo_pago(guild_id, usuario.id)
        bloqueado = await db.esta_en_blacklist(guild_id, usuario.id)
        movimientos = await db.movimientos_de_usuario(guild_id, usuario.id, 5)

        embed = discord.Embed(title=f"Historial de {usuario.display_name}", color=COLOR_INFO)
        embed.add_field(name="Aprobadas", value=str(conteos.get("aprobada", 0)))
        embed.add_field(name="Rechazadas", value=str(conteos.get("rechazada", 0)))
        embed.add_field(name="Vencidas", value=str(conteos.get("vencida", 0)))
        embed.add_field(name="Saldo", value=fmt_monto(saldo))
        embed.add_field(name="Método de pago", value=metodo or "No configurado")
        embed.add_field(name="Lista negra", value="Sí" if bloqueado else "No")
        if recientes:
            embed.add_field(
                name="Últimas reseñas",
                value="\n".join(
                    f"#{r['review_id']} - {ACCIONES_LEGIBLES.get(r['accion'], r['accion'])} - {_fecha(r['fecha'])}"
                    for r in recientes[:8]
                ),
                inline=False,
            )
        if movimientos:
            embed.add_field(
                name="Últimos movimientos de saldo",
                value="\n".join(
                    f"{m['tipo']}: {fmt_monto(m['monto'])} - {_fecha(m['fecha'])}" for m in movimientos
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="reseña-info", description="Detalle completo de una reseña")
    @app_commands.describe(id="Número de la reseña")
    @es_staff()
    async def resena_info(self, interaction: discord.Interaction, id: int):
        review = await db.get_review(id)
        if review is None or review["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("No existe esa reseña.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Reseña #{id}", color=COLOR_INFO)
        embed.add_field(name="Estado", value=ESTADOS_LEGIBLES.get(review["estado"], review["estado"]))
        embed.add_field(
            name="Reclamada por",
            value=f"<@{review['claimed_by']}>" if review["claimed_by"] else "Nadie",
        )
        embed.add_field(name="Reseña asignada", value=review["link"] or "Ninguno", inline=False)
        embed.add_field(name="Prueba enviada", value=review["proof_link"] or "Ninguna", inline=False)
        if review["decided_by"]:
            embed.add_field(name="Aprobada por", value=f"<@{review['decided_by']}>")
        decisiones = await db.decisiones_de_review(id)
        if decisiones:
            embed.add_field(
                name="Historial de decisiones",
                value="\n".join(
                    f"{ACCIONES_LEGIBLES.get(d['accion'], d['accion'])} - <@{d['user_id']}> - {_fecha(d['fecha'])}"
                    + (f" ({d['motivo']})" if d["motivo"] else "")
                    for d in decisiones
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="eliminar-reseña", description="Elimina del historial una reseña aprobada"
    )
    @app_commands.describe(id="Número de la reseña aprobada")
    @es_staff()
    async def eliminar_resena(self, interaction: discord.Interaction, id: int):
        review = await db.get_review(id)
        if review is None or review["guild_id"] != interaction.guild_id or review["estado"] != "aprobada":
            await interaction.response.send_message(
                "Esa reseña no existe o no está aprobada.", ephemeral=True
            )
            return
        plantilla = await db.get_plantilla(review["plantilla_id"]) if review["plantilla_id"] else None
        valor = plantilla["valor"] if plantilla else 0

        embed = discord.Embed(title=f"Eliminar la reseña #{id}", color=COLOR_AVISO)
        embed.add_field(name="Usuario", value=f"<@{review['claimed_by']}>", inline=False)
        embed.add_field(name="Reseña asignada", value=review["link"] or "N/A", inline=False)
        embed.add_field(name="Prueba enviada", value=review["proof_link"] or "N/A", inline=False)
        embed.add_field(name="Recompensa que se otorgó", value=fmt_monto(valor), inline=False)
        await interaction.response.send_message(
            embed=embed, view=ConfirmarEliminarView(review, interaction.user.id, valor), ephemeral=True
        )

    @app_commands.command(
        name="reset-cooldown", description="Libera la reseña reclamada de un usuario"
    )
    @app_commands.describe(usuario="Usuario a liberar")
    @es_staff()
    async def reset_cooldown(self, interaction: discord.Interaction, usuario: discord.Member):
        review = await db.resena_activa_de_usuario(interaction.guild_id, usuario.id)
        if review is None:
            await interaction.response.send_message(
                "Ese usuario no tiene ninguna reseña reclamada.", ephemeral=True
            )
            return
        previa = await db.liberar_review(
            review["id"], interaction.user.id, "Liberada manualmente por el staff", "liberada",
            ("reclamada", "pendiente_verificacion"),
        )
        if previa is None:
            await interaction.response.send_message("La reseña ya cambió de estado.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Se liberó la reseña #{review['id']} de {usuario.mention}. Volvió al stock.", ephemeral=True
        )
        await mandar_dm(
            interaction.client, usuario.id,
            discord.Embed(
                title="Reseña liberada",
                description=f"El staff liberó tu reseña #{review['id']}. Puedes reclamar otra desde tu ticket.",
                color=COLOR_AVISO,
            ),
        )
        await enviar_log(
            interaction.guild, "Reseña liberada", color=COLOR_AVISO,
            campos=[("Reseña", f"#{review['id']}"), ("Usuario", usuario.mention), ("Staff", interaction.user.mention)],
        )

    @app_commands.command(
        name="recordatorio", description="Recuerda a un usuario que debe enviar sus pruebas"
    )
    @app_commands.describe(usuario="Usuario a recordar")
    @es_staff()
    async def recordatorio(self, interaction: discord.Interaction, usuario: discord.Member):
        review = await db.resena_activa_de_usuario(interaction.guild_id, usuario.id)
        if review is None or review["estado"] != "reclamada":
            await interaction.response.send_message(
                "Ese usuario no tiene una reseña pendiente de pruebas.", ephemeral=True
            )
            return
        aviso = discord.Embed(
            title="Recordatorio",
            description=f"Recuerda enviar las pruebas de tu reseña #{review['id']} antes de que se agote el tiempo.",
            color=COLOR_AVISO,
        )
        canal = interaction.guild.get_channel(review["ticket_channel_id"]) if review["ticket_channel_id"] else None
        if canal is not None:
            try:
                await canal.send(usuario.mention, embed=aviso)
            except discord.HTTPException:
                pass
        await mandar_dm(interaction.client, usuario.id, aviso)
        await interaction.response.send_message("Recordatorio enviado.", ephemeral=True)

    @app_commands.command(name="exportar", description="Exporta el historial de reseñas en CSV")
    @es_staff()
    async def exportar(self, interaction: discord.Interaction):
        filas = await db.todas_las_resenas(interaction.guild_id)
        buffer = io.StringIO()
        escritor = csv.writer(buffer)
        escritor.writerow(
            ["id", "link", "estado", "usuario", "reclamada", "prueba", "aprobada_por", "fecha_decision"]
        )
        for r in filas:
            escritor.writerow(
                [r["id"], r["link"], r["estado"], r["claimed_by"], r["claimed_at"],
                 r["proof_link"], r["decided_by"], r["decided_at"]]
            )
        archivo = discord.File(io.BytesIO(buffer.getvalue().encode("utf-8-sig")), filename="reseñas.csv")
        await interaction.response.send_message(
            "Aquí tienes el historial completo.", file=archivo, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Reviews(bot))
