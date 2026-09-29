import asyncio
import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database as db
from utils import es_staff, es_miembro_staff, enviar_log
from config import COLOR_PRINCIPAL, COLOR_AVISO

log = logging.getLogger("bot")

INACTIVIDAD_HORAS = 48
_abriendo = set()


async def cerrar_canal_ticket(canal: discord.TextChannel, motivo: str, espera: int = 5):
    ticket = await db.get_ticket_por_canal(canal.id)
    if ticket is None or ticket["status"] != "abierto":
        return
    await db.cerrar_ticket(canal.id)

    liberada = False
    review = await db.review_reclamada_de_ticket(canal.id)
    if review is not None:
        previa = await db.liberar_review(review["id"], None, motivo, "ticket_cerrado", ("reclamada",))
        liberada = previa is not None

    descripcion = f"{motivo}.\nEl canal se eliminará en {espera} segundos."
    if liberada:
        descripcion += "\nLa reseña que tenías reclamada volvió al stock."
    try:
        await canal.send(
            embed=discord.Embed(title="Ticket cerrado", description=descripcion, color=COLOR_AVISO)
        )
    except discord.HTTPException:
        pass

    await enviar_log(
        canal.guild,
        "Ticket cerrado",
        campos=[("Ticket", canal.name), ("Usuario", f"<@{ticket['user_id']}>"), ("Motivo", motivo)],
    )
    await asyncio.sleep(espera)
    try:
        await canal.delete(reason=motivo)
    except discord.HTTPException:
        pass


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Abrir ticket", style=discord.ButtonStyle.primary, custom_id="panel:abrir_ticket"
    )
    async def abrir_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        clave = (interaction.guild_id, interaction.user.id)
        if clave in _abriendo:
            await interaction.response.send_message(
                "Tu ticket se está creando, espera un momento.", ephemeral=True
            )
            return
        _abriendo.add(clave)
        try:
            await interaction.response.defer(ephemeral=True)
            await self._crear_ticket(interaction)
        finally:
            _abriendo.discard(clave)

    async def _crear_ticket(self, interaction: discord.Interaction):
        guild = interaction.guild
        usuario = interaction.user
        cfg = await db.get_config(guild.id)

        if cfg["tickets_paused"]:
            await interaction.followup.send(
                "La apertura de tickets está pausada temporalmente.", ephemeral=True
            )
            return

        categoria = guild.get_channel(cfg["ticket_category_id"]) if cfg["ticket_category_id"] else None
        if not isinstance(categoria, discord.CategoryChannel):
            await interaction.followup.send(
                "El sistema de tickets no está configurado todavía. Avisa al staff.", ephemeral=True
            )
            return

        existente = await db.ticket_abierto_de_usuario(guild.id, usuario.id)
        if existente is not None:
            canal_existente = guild.get_channel(existente["channel_id"])
            if canal_existente is not None:
                await interaction.followup.send(
                    f"Ya tienes un ticket abierto: {canal_existente.mention}", ephemeral=True
                )
                return
            await db.cerrar_ticket(existente["channel_id"])

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            usuario: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, embed_links=True
            ),
        }
        for rol in guild.roles:
            if rol.is_default() or rol.managed:
                continue
            if rol.permissions.manage_guild or rol.permissions.administrator:
                overwrites[rol] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        numero = await db.next_ticket_number(guild.id)
        try:
            canal = await guild.create_text_channel(
                f"ticket-{numero}",
                category=categoria,
                overwrites=overwrites,
                topic=f"Ticket de {usuario} ({usuario.id})",
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "No pude crear el ticket. Avisa al staff (revisa los permisos del bot).",
                ephemeral=True,
            )
            return

        await db.crear_ticket(guild.id, canal.id, usuario.id)

        embed = discord.Embed(
            title=f"Ticket #{numero}", description=cfg["ticket_message"], color=COLOR_PRINCIPAL
        )
        embed.set_footer(text=f"Abierto por {usuario.display_name}")
        await canal.send(content=usuario.mention, embed=embed, view=TicketView())
        await interaction.followup.send(f"Tu ticket fue creado: {canal.mention}", ephemeral=True)

        await enviar_log(
            guild, "Ticket abierto", campos=[("Ticket", canal.mention), ("Usuario", usuario.mention)]
        )


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Reclamar reseña", style=discord.ButtonStyle.success, custom_id="ticket:reclamar_resena"
    )
    async def reclamar_resena(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.reviews import iniciar_reclamo_resena
        await iniciar_reclamo_resena(interaction)

    @discord.ui.button(
        label="Enviar pruebas", style=discord.ButtonStyle.primary, custom_id="ticket:enviar_pruebas"
    )
    async def enviar_pruebas(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.reviews import abrir_modal_pruebas
        await abrir_modal_pruebas(interaction)

    @discord.ui.button(
        label="Reclamar ticket", style=discord.ButtonStyle.secondary, custom_id="ticket:reclamar_ticket"
    )
    async def reclamar_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not es_miembro_staff(interaction.user):
            await interaction.response.send_message(
                "Solo el staff puede reclamar tickets.", ephemeral=True
            )
            return
        ticket = await db.get_ticket_por_canal(interaction.channel_id)
        if ticket is None or ticket["status"] != "abierto":
            await interaction.response.send_message("Este ticket ya no está activo.", ephemeral=True)
            return
        if ticket["claimed_staff_id"] and ticket["claimed_staff_id"] != interaction.user.id:
            await interaction.response.send_message(
                f"Este ticket ya fue reclamado por <@{ticket['claimed_staff_id']}>.", ephemeral=True
            )
            return
        await db.reclamar_ticket(interaction.channel_id, interaction.user.id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Ticket reclamado",
                description=f"{interaction.user.mention} se encargará de este ticket.",
                color=COLOR_PRINCIPAL,
            )
        )

    @discord.ui.button(
        label="Cerrar ticket", style=discord.ButtonStyle.danger, custom_id="ticket:cerrar_ticket"
    )
    async def cerrar_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await db.get_ticket_por_canal(interaction.channel_id)
        if ticket is None or ticket["status"] != "abierto":
            await interaction.response.send_message(
                "Este canal no es un ticket activo.", ephemeral=True
            )
            return
        if not (ticket["user_id"] == interaction.user.id or es_miembro_staff(interaction.user)):
            await interaction.response.send_message(
                "No tienes permiso para cerrar este ticket.", ephemeral=True
            )
            return
        await interaction.response.send_message("Cerrando el ticket...", ephemeral=True)
        await cerrar_canal_ticket(interaction.channel, f"Cerrado por {interaction.user.display_name}")


class MensajeTicketModal(discord.ui.Modal):
    def __init__(self, texto_actual: str):
        super().__init__(title="Mensaje de bienvenida")
        self.texto = discord.ui.TextInput(
            label="Texto que aparece al abrir un ticket",
            style=discord.TextStyle.paragraph,
            default=texto_actual[:1500],
            max_length=1500,
        )
        self.add_item(self.texto)

    async def on_submit(self, interaction: discord.Interaction):
        await db.update_config(interaction.guild_id, ticket_message=self.texto.value)
        await interaction.response.send_message(
            "El mensaje de bienvenida de los tickets fue actualizado.", ephemeral=True
        )


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.autolimpieza.start()

    async def cog_unload(self):
        self.autolimpieza.cancel()

    @app_commands.command(name="panel", description="Publica el panel de tickets en este canal")
    @es_staff()
    async def panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Sistema de reseñas",
            description="Pulsa el botón para abrir un ticket, reclamar reseñas y enviar tus pruebas.",
            color=COLOR_PRINCIPAL,
        )
        try:
            await interaction.channel.send(embed=embed, view=PanelView())
        except discord.HTTPException:
            await interaction.response.send_message(
                "No tengo permiso para escribir en este canal.", ephemeral=True
            )
            return
        await db.update_config(interaction.guild_id, panel_channel_id=interaction.channel_id)
        await interaction.response.send_message("Panel publicado.", ephemeral=True)

    @app_commands.command(
        name="mensaje-ticket", description="Edita el texto que aparece al abrir un ticket"
    )
    @es_staff()
    async def mensaje_ticket(self, interaction: discord.Interaction):
        cfg = await db.get_config(interaction.guild_id)
        await interaction.response.send_modal(MensajeTicketModal(cfg["ticket_message"]))

    @app_commands.command(
        name="pausar-tickets", description="Pausa o reanuda la apertura de tickets"
    )
    @es_staff()
    async def pausar_tickets(self, interaction: discord.Interaction):
        cfg = await db.get_config(interaction.guild_id)
        nuevo_estado = 0 if cfg["tickets_paused"] else 1
        await db.update_config(interaction.guild_id, tickets_paused=nuevo_estado)
        estado = "pausada" if nuevo_estado else "reanudada"
        await interaction.response.send_message(
            f"La apertura de tickets fue {estado}.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        ticket = await db.get_ticket_por_canal(channel.id)
        if ticket is None or ticket["status"] != "abierto":
            return
        await db.cerrar_ticket(channel.id)
        review = await db.review_reclamada_de_ticket(channel.id)
        if review is not None:
            await db.liberar_review(
                review["id"], None, "Ticket eliminado", "ticket_cerrado", ("reclamada",)
            )

    @tasks.loop(minutes=30)
    async def autolimpieza(self):
        try:
            ahora = discord.utils.utcnow()
            for guild in self.bot.guilds:
                for ticket in await db.tickets_abiertos(guild.id):
                    canal = guild.get_channel(ticket["channel_id"])
                    if canal is None:
                        await db.cerrar_ticket(ticket["channel_id"])
                        review = await db.review_reclamada_de_ticket(ticket["channel_id"])
                        if review is not None:
                            await db.liberar_review(
                                review["id"], None, "Ticket eliminado", "ticket_cerrado", ("reclamada",)
                            )
                        continue
                    if await db.review_pendiente_de_ticket(canal.id):
                        continue
                    if canal.last_message_id:
                        ultima = discord.utils.snowflake_time(canal.last_message_id)
                    else:
                        ultima = canal.created_at
                    if ahora - ultima > datetime.timedelta(hours=INACTIVIDAD_HORAS):
                        await cerrar_canal_ticket(canal, "Cerrado por inactividad")
        except Exception:
            log.exception("Error en la limpieza automática de tickets")

    @autolimpieza.before_loop
    async def antes_autolimpieza(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
