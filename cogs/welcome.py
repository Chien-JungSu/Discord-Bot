import os
import json
import tempfile
import traceback

import discord
from discord import app_commands
from discord.ext import commands

WELCOME_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'welcome_settings.json')


def _load_welcome_settings() -> dict:
    if os.path.exists(WELCOME_SETTINGS_FILE):
        try:
            with open(WELCOME_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_welcome_settings(data: dict):
    dir_path = os.path.dirname(WELCOME_SETTINGS_FILE)
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=dir_path, delete=False, suffix='.tmp') as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, WELCOME_SETTINGS_FILE)
    except Exception as e:
        print(f"❌ 儲存歡迎訊息設定失敗: {e}")
        raise


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: dict = _load_welcome_settings()

    @app_commands.command(name="welcome_active", description="設定伺服器歡迎訊息的規則與頻道")
    @app_commands.describe(
        welcome_channel="新成員加入時發送歡迎訊息的頻道（必填）",
        rules_channel="規則頻道（選填，不填則不顯示）",
        role_channel="身份組領取的頻道（選填，不填則不顯示）"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_active(
        self,
        interaction: discord.Interaction,
        welcome_channel: discord.TextChannel,
        rules_channel: discord.TextChannel | None = None,
        role_channel: discord.TextChannel | None = None
    ):
        guild_id = str(interaction.guild.id)
        self.settings[guild_id] = {
            'welcome_channel_id': welcome_channel.id,
            'rules_channel_id': rules_channel.id if rules_channel else None,
            'role_channel_id': role_channel.id if role_channel else None,
        }
        try:
            _save_welcome_settings(self.settings)
        except Exception as e:
            await self.bot.notify_owner_error(e, interaction, extra_info="welcome_active: 儲存設定失敗")
            await interaction.response.send_message("❌ 儲存設定時發生錯誤，設定可能在重新啟動後遺失，已回報開發者。", ephemeral=True)
            return

        desc_lines = [f"✅ 歡迎訊息已啟用！", f"📢 歡迎頻道：{welcome_channel.mention}"]
        if rules_channel:
            desc_lines.append(f"📋 規則頻道：{rules_channel.mention}")
        if role_channel:
            desc_lines.append(f"🎭 身份組領取頻道：{role_channel.mention}")

        embed = discord.Embed(title="🎉 歡迎訊息設定完成", description="\n".join(desc_lines), color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="welcome_inactive", description="取消伺服器的歡迎訊息功能")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_inactive(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        if guild_id in self.settings:
            del self.settings[guild_id]
            try:
                _save_welcome_settings(self.settings)
            except Exception as e:
                await self.bot.notify_owner_error(e, interaction, extra_info="welcome_inactive: 儲存設定失敗")
                await interaction.response.send_message("❌ 儲存設定時發生錯誤，可能需要再執行一次，已回報開發者。", ephemeral=True)
                return
            embed = discord.Embed(title="🔕 歡迎訊息已關閉", description="此伺服器的歡迎訊息功能已停用。", color=discord.Color.red())
        else:
            embed = discord.Embed(title="ℹ️ 歡迎訊息未啟用", description="此伺服器目前沒有啟用歡迎訊息功能。", color=discord.Color.light_grey())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = str(member.guild.id)
        settings = self.settings.get(guild_id)
        if not settings:
            return

        channel = member.guild.get_channel(settings['welcome_channel_id'])
        if channel is None:
            return

        join_time = discord.utils.format_dt(member.joined_at or discord.utils.utcnow(), style='F')
        embed = discord.Embed(
            title=f"🎉 歡迎新成員加入 {member.guild.name}！",
            description=(
                f"嗨 {member.mention}，歡迎來到 **{member.guild.name}** 🎊\n\n"
                f"你是第 **{member.guild.member_count}** 位成員，希望你在這裡玩得開心！"
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        avatar_url = member.display_avatar.url
        embed.set_thumbnail(url=avatar_url)
        embed.set_author(name=str(member), icon_url=avatar_url)
        embed.add_field(name="📅 加入時間", value=join_time, inline=False)

        rules_channel_id = settings.get('rules_channel_id')
        if rules_channel_id:
            rules_ch = member.guild.get_channel(rules_channel_id)
            if rules_ch:
                embed.add_field(name="📋 伺服器規則", value=f"請先閱讀 {rules_ch.mention} 的規則！", inline=False)

        role_channel_id = settings.get('role_channel_id')
        if role_channel_id:
            role_ch = member.guild.get_channel(role_channel_id)
            if role_ch:
                embed.add_field(name="🎭 領取身份組", value=f"前往 {role_ch.mention} 領取你的身份組！", inline=False)

        embed.set_footer(text=f"成員 ID：{member.id}")

        try:
            await channel.send(content=f"{member.mention} 歡迎加入！", embed=embed)
        except discord.Forbidden:
            print(f"❌ 歡迎訊息：機器人沒有在 {channel} 發言的權限。")
            await self.bot.notify_owner_error(
                discord.Forbidden(discord.http.Route('POST', '/channels/{channel_id}/messages', channel_id=channel.id), ''),
                extra_info=f"on_member_join: 缺少 {channel} 的發言權限，伺服器={member.guild.name} ({member.guild.id})"
            )
        except Exception as e:
            print(f"❌ 發送歡迎訊息時發生未知錯誤: {e}")
            traceback.print_exc()
            await self.bot.notify_owner_error(e, extra_info=f"on_member_join: member={member} guild={member.guild.name} ({member.guild.id})")


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
