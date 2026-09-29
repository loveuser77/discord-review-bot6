import discord
from discord import app_commands
from discord.ext import commands

import database as db
from utils import es_staff, es_miembro_staff, enviar_log, mandar_dm, fmt_monto
from config import COLOR_INFO, COLOR_EXITO, COLOR_AVISO

LIMITE_MONTO = 1_000_000_000


def _fecha(valor: str) -> str:
    return discord.utils.format_dt(db.parse_fecha(valor), "d")


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="saldo", description="Muestra tu saldo (el staff puede consultar a otro usuario)")
    @app_commands.describe(usuario="Solo para el staff: usuario a consultar")
    @app_commands.guild_only()
    async def saldo(self, interaction: discord.Interaction, usuario: discord.Member = None):
        objetivo = interaction.user
        if usuario is not None and usuario.id != interaction.user.id:
            if not es_miembro_staff(interaction.user):
                await interaction.response.send_message(
                    "Solo el staff puede consultar el saldo de otros usuarios.", ephemeral=True
                )
                return
            objetivo = usuario

        balance = await db.get_balance(interaction.guild_id, objetivo.id)
        metodo = await db.get_metodo_pago(interaction.guild_id, objetivo.id)
        embed = discord.Embed(title=f"Saldo de {objetivo.display_name}", color=COLOR_INFO)
        embed.add_field(name="Saldo disponible", value=fmt_monto(balance))
        embed.add_field(name="Método de pago", value=metodo or "No configurado")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="metodo-pago", description="Configura cómo quieres recibir tus pagos")
    @app_commands.describe(metodo="Ejemplo: PayPal correo@ejemplo.com, transferencia, USDT...")
    @app_commands.guild_only()
    async def metodo_pago(self, interaction: discord.Interaction, metodo: str):
        metodo = metodo.strip()
        if not metodo or len(metodo) > 200:
            await interaction.response.send_message(
                "El método de pago debe tener entre 1 y 200 caracteres.", ephemeral=True
            )
            return
        await db.set_metodo_pago(interaction.guild_id, interaction.user.id, metodo)
        await interaction.response.send_message(
            f"Tu método de pago quedó guardado: {metodo}", ephemeral=True
        )

    @app_commands.command(name="movimientos", description="Últimos movimientos de saldo")
    @app_commands.describe(usuario="Solo para el staff: usuario a consultar")
    @app_commands.guild_only()
    async def movimientos(self, interaction: discord.Interaction, usuario: discord.Member = None):
        objetivo = interaction.user
        if usuario is not None and usuario.id != interaction.user.id:
            if not es_miembro_staff(interaction.user):
                await interaction.response.send_message(
                    "Solo el staff puede consultar los movimientos de otros usuarios.", ephemeral=True
                )
                return
            objetivo = usuario

        filas = await db.movimientos_de_usuario(interaction.guild_id, objetivo.id, 10)
        if not filas:
            await interaction.response.send_message("No hay movimientos registrados.", ephemeral=True)
            return
        lineas = [
            f"{_fecha(m['fecha'])} - {m['tipo']}: {fmt_monto(m['monto'])} (saldo: {fmt_monto(m['saldo_resultante'])})"
            for m in filas
        ]
        embed = discord.Embed(
            title=f"Movimientos de {objetivo.display_name}",
            description="\n".join(lineas),
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="addsaldo", description="Agrega saldo a un usuario")
    @app_commands.describe(usuario="Usuario", cantidad="Cantidad a agregar", motivo="Motivo (opcional)")
    @es_staff()
    async def addsaldo(self, interaction: discord.Interaction, usuario: discord.Member,
                       cantidad: float, motivo: str = None):
        if cantidad <= 0 or cantidad > LIMITE_MONTO:
            await interaction.response.send_message("La cantidad debe ser mayor a 0.", ephemeral=True)
            return
        anterior, nuevo = await db.ajustar_saldo(
            interaction.guild_id, usuario.id, cantidad, "ajuste (+)", interaction.user.id, motivo
        )
        await interaction.response.send_message(
            f"Se agregaron {fmt_monto(cantidad)} a {usuario.mention}. "
            f"Saldo: {fmt_monto(anterior)} -> {fmt_monto(nuevo)}",
            ephemeral=True,
        )
        await enviar_log(
            interaction.guild, "Saldo agregado", color=COLOR_EXITO,
            campos=[("Usuario", usuario.mention), ("Cantidad", fmt_monto(cantidad)),
                    ("Saldo nuevo", fmt_monto(nuevo)), ("Staff", interaction.user.mention),
                    ("Motivo", motivo or "-")],
        )

    @app_commands.command(name="quitarsaldo", description="Quita saldo a un usuario")
    @app_commands.describe(usuario="Usuario", cantidad="Cantidad a quitar", motivo="Motivo (opcional)")
    @es_staff()
    async def quitarsaldo(self, interaction: discord.Interaction, usuario: discord.Member,
                          cantidad: float, motivo: str = None):
        if cantidad <= 0 or cantidad > LIMITE_MONTO:
            await interaction.response.send_message("La cantidad debe ser mayor a 0.", ephemeral=True)
            return
        anterior, nuevo = await db.ajustar_saldo(
            interaction.guild_id, usuario.id, -cantidad, "ajuste (-)", interaction.user.id, motivo
        )
        await interaction.response.send_message(
            f"Se quitaron {fmt_monto(anterior - nuevo)} a {usuario.mention}. "
            f"Saldo: {fmt_monto(anterior)} -> {fmt_monto(nuevo)}",
            ephemeral=True,
        )
        await enviar_log(
            interaction.guild, "Saldo descontado", color=COLOR_AVISO,
            campos=[("Usuario", usuario.mention), ("Cantidad", fmt_monto(anterior - nuevo)),
                    ("Saldo nuevo", fmt_monto(nuevo)), ("Staff", interaction.user.mention),
                    ("Motivo", motivo or "-")],
        )

    @app_commands.command(name="editar-saldo", description="Establece el saldo exacto de un usuario")
    @app_commands.describe(usuario="Usuario", nuevo_saldo="Saldo que debe quedar", motivo="Motivo (opcional)")
    @es_staff()
    async def editar_saldo(self, interaction: discord.Interaction, usuario: discord.Member,
                           nuevo_saldo: float, motivo: str = None):
        if nuevo_saldo < 0 or nuevo_saldo > LIMITE_MONTO:
            await interaction.response.send_message(
                "El saldo debe ser un número mayor o igual a 0.", ephemeral=True
            )
            return
        anterior, nuevo = await db.fijar_saldo(
            interaction.guild_id, usuario.id, nuevo_saldo, "edición", interaction.user.id, motivo
        )
        embed = discord.Embed(title="Saldo editado", color=COLOR_INFO)
        embed.add_field(name="Usuario", value=usuario.mention)
        embed.add_field(name="Saldo anterior", value=fmt_monto(anterior))
        embed.add_field(name="Saldo nuevo", value=fmt_monto(nuevo))
        if motivo:
            embed.add_field(name="Motivo", value=motivo, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await enviar_log(
            interaction.guild, "Saldo editado",
            campos=[("Usuario", usuario.mention), ("Anterior", fmt_monto(anterior)),
                    ("Nuevo", fmt_monto(nuevo)), ("Staff", interaction.user.mention),
                    ("Motivo", motivo or "-")],
        )

    @app_commands.command(name="pagar", description="Registra un pago a un usuario y descuenta su saldo")
    @app_commands.describe(
        usuario="Usuario a pagar",
        monto="Monto pagado (déjalo vacío para pagar todo el saldo)",
        nota="Nota del pago (opcional)",
    )
    @es_staff()
    async def pagar(self, interaction: discord.Interaction, usuario: discord.Member,
                    monto: float = None, nota: str = None):
        saldo = await db.get_balance(interaction.guild_id, usuario.id)
        if saldo <= 0:
            await interaction.response.send_message(
                f"{usuario.mention} no tiene saldo pendiente.", ephemeral=True
            )
            return
        if monto is None:
            monto = saldo
        if monto <= 0:
            await interaction.response.send_message("El monto debe ser mayor a 0.", ephemeral=True)
            return
        if monto > saldo:
            await interaction.response.send_message(
                f"El monto supera su saldo ({fmt_monto(saldo)}).", ephemeral=True
            )
            return

        _, nuevo = await db.ajustar_saldo(
            interaction.guild_id, usuario.id, -monto, "pago", interaction.user.id, nota
        )
        metodo = await db.get_metodo_pago(interaction.guild_id, usuario.id)
        embed = discord.Embed(title="Pago registrado", color=COLOR_EXITO)
        embed.add_field(name="Usuario", value=usuario.mention)
        embed.add_field(name="Monto pagado", value=fmt_monto(monto))
        embed.add_field(name="Saldo restante", value=fmt_monto(nuevo))
        embed.add_field(name="Método de pago", value=metodo or "No configurado")
        if nota:
            embed.add_field(name="Nota", value=nota, inline=False)
        await interaction.response.send_message(embed=embed)

        await mandar_dm(
            self.bot, usuario.id,
            discord.Embed(
                title="Pago realizado",
                description=f"Se registró un pago de {fmt_monto(monto)}.\nSaldo restante: {fmt_monto(nuevo)}",
                color=COLOR_EXITO,
            ),
        )
        await enviar_log(
            interaction.guild, "Pago registrado", color=COLOR_EXITO,
            campos=[("Usuario", usuario.mention), ("Monto", fmt_monto(monto)),
                    ("Staff", interaction.user.mention)],
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
