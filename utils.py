import discord
from discord import app_commands

import database as db
from config import COLOR_INFO


def es_miembro_staff(usuario) -> bool:
    permisos = getattr(usuario, "guild_permissions", None)
    return bool(permisos and permisos.manage_guild)


async def _predicado_staff(interaction: discord.Interaction) -> bool:
    if es_miembro_staff(interaction.user):
        return True
    raise app_commands.CheckFailure("No tienes permisos para usar este comando.")


def solo_staff():
    return app_commands.check(_predicado_staff)


def es_staff():
    def decorador(func):
        func = app_commands.check(_predicado_staff)(func)
        func = app_commands.default_permissions(manage_guild=True)(func)
        func = app_commands.guild_only()(func)
        return func
    return decorador


def fmt_monto(valor) -> str:
    return f"{float(valor):.2f}".rstrip("0").rstrip(".")


async def enviar_log(guild: discord.Guild, titulo: str, descripcion: str = "",
                     color: int = COLOR_INFO, campos=None):
    cfg = await db.get_config(guild.id)
    if not cfg["log_channel_id"]:
        return
    canal = guild.get_channel(cfg["log_channel_id"])
    if canal is None:
        return
    embed = discord.Embed(
        title=titulo,
        description=descripcion or None,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    for nombre, valor in campos or []:
        embed.add_field(name=nombre, value=str(valor)[:1024], inline=True)
    try:
        await canal.send(embed=embed)
    except discord.HTTPException:
        pass


async def mandar_dm(bot, user_id: int, embed: discord.Embed) -> bool:
    usuario = bot.get_user(user_id)
    if usuario is None:
        try:
            usuario = await bot.fetch_user(user_id)
        except discord.HTTPException:
            return False
    try:
        await usuario.send(embed=embed)
        return True
    except discord.HTTPException:
        return False
