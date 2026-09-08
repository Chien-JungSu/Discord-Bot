import discord
from discord import app_commands
from discord.ext import commands


def generate_server_info_embed(guild: discord.Guild) -> discord.Embed:
    created_time_full = discord.utils.format_dt(guild.created_at, style='F')
    created_time_relative = discord.utils.format_dt(guild.created_at, style='R')

    embed = discord.Embed(
        title=f"📊 {guild.name} 的伺服器資訊",
        color=discord.Color.teal(),
        timestamp=discord.utils.utcnow()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="🏰 伺服器名稱", value=guild.name, inline=True)
    embed.add_field(name="👥 總成員人數", value=f"{guild.member_count} 人", inline=True)
    embed.add_field(name="📅 建立時間", value=f"{created_time_full}\n({created_time_relative})", inline=False)
    embed.set_footer(text=f"伺服器 ID: {guild.id}")

    return embed


class ServerInfo(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="server_info", description="顯示此伺服器的詳細資訊")
    @app_commands.guild_only()
    async def server_info(self, interaction: discord.Interaction):
        info_embed = generate_server_info_embed(interaction.guild)
        await interaction.response.send_message(embed=info_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerInfo(bot))
