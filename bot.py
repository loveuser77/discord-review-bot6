import logging

import discord
from discord.ext import commands

import config
import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

COGS = [
    "cogs.tickets",
    "cogs.reviews",
    "cogs.economy",
    "cogs.admin",
]


class BotResenas(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        await database.init_db()

        for cog in COGS:
            await self.load_extension(cog)
            log.info(f"Cog cargado: {cog}")

        from cogs.tickets import PanelView, TicketView
        from cogs.reviews import VerificationView

        self.add_view(PanelView())
        self.add_view(TicketView())
        self.add_view(VerificationView())

        self.tree.on_error = self.on_app_command_error

        try:
            sincronizados = await self.tree.sync()
            log.info(f"{len(sincronizados)} comandos sincronizados")
        except discord.HTTPException as e:
            log.error(f"Error sincronizando comandos: {e}")

    async def on_ready(self):
        log.info(f"Sesión iniciada como {self.user} ({self.user.id})")

    async def on_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, discord.app_commands.CheckFailure):
            mensaje = str(error) or "No tienes permiso para usar este comando."
        else:
            original = getattr(error, "original", error)
            log.error("Error en un comando", exc_info=original)
            mensaje = "Ocurrió un error al ejecutar el comando. Inténtalo de nuevo."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(mensaje, ephemeral=True)
            else:
                await interaction.response.send_message(mensaje, ephemeral=True)
        except discord.HTTPException:
            pass


bot = BotResenas()

if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit(
            "No se encontró DISCORD_TOKEN. Configúralo en las variables de entorno o en un archivo .env."
        )
    bot.run(config.TOKEN, log_handler=None)
