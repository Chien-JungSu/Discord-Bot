import os
import json
import tempfile
import traceback
from typing import Optional

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


# 頻道型別的中文顯示名稱，用於錯誤提示。
_CHANNEL_TYPE_LABELS: list[tuple[type, str]] = [
    (discord.VoiceChannel, "語音頻道"),
    (discord.StageChannel, "舞台頻道"),
    (discord.ForumChannel, "論壇頻道"),
    (discord.CategoryChannel, "分類頻道"),
]


def _channel_type_label(ch: discord.abc.GuildChannel) -> str:
    """取得頻道型別的中文標籤（無法辨識時退回英文類名）。"""
    for cls, label in _CHANNEL_TYPE_LABELS:
        if isinstance(ch, cls):
            return label
    return type(ch).__name__


def _extract_text_channel(ch: Optional[discord.abc.GuildChannel]) -> Optional[discord.TextChannel]:
    """只接受文字頻道（含公告頻道，公告頻道在 discord.py 中也是 TextChannel）。"""
    if isinstance(ch, discord.TextChannel):
        return ch
    return None


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: dict = _load_welcome_settings()

    @app_commands.command(name="welcome_active", description="設定伺服器歡迎訊息的頻道（歡迎／規則／身份組）")
    @app_commands.describe(
        welcome_channel="新成員加入時發送歡迎訊息的頻道（必填，需為文字頻道）",
        rules_channel="規則頻道（選填，不填則不顯示，需為文字頻道）",
        role_channel="身份組領取的頻道（選填，不填則不顯示，需為文字頻道）"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def welcome_active(
        self,
        interaction: discord.Interaction,
        welcome_channel: discord.abc.GuildChannel,
        rules_channel: Optional[discord.abc.GuildChannel] = None,
        role_channel: Optional[discord.abc.GuildChannel] = None
    ):
        # 改為接受任何伺服器頻道型別（文字、語音、論壇、公告、舞台…），再自行驗證。
        # 若只標註 discord.TextChannel，使用者在 Discord 選單選到語音或論壇頻道時，
        # discord.py 會在參數轉換階段丟 TransformerError，使用者只會看到冷冰冰的
        # 「應用程式錯誤」；改成在這裡驗證，才能給出清楚的中文提示。
        # （Optional[...] 而非 X | None：discord.py 的註解解析在 Python 3.12
        #   不支援 PEP 604 union，Optional 在 3.12 / 3.14 都能正確註冊。）
        welcome_tc = _extract_text_channel(welcome_channel)
        rules_tc = _extract_text_channel(rules_channel)
        role_tc = _extract_text_channel(role_channel)

        # 一次列出所有選錯型別的參數，使用者不用改一次錯一次。
        invalid = [
            f"• **{label}** 是{_channel_type_label(ch)} {ch.mention}，請改選**文字頻道**"
            for label, ch, tc in (
                ("歡迎頻道", welcome_channel, welcome_tc),
                ("規則頻道", rules_channel, rules_tc),
                ("身份組領取頻道", role_channel, role_tc),
            )
            if ch is not None and tc is None
        ]
        if invalid:
            embed = discord.Embed(
                title="❌ 頻道型別錯誤",
                description="歡迎訊息只能發送到**文字頻道**（含公告頻道）。以下參數請改選文字頻道：\n\n" + "\n".join(invalid),
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        guild_id = str(interaction.guild.id)
        self.settings[guild_id] = {
            'welcome_channel_id': welcome_tc.id,
            'rules_channel_id': rules_tc.id if rules_tc else None,
            'role_channel_id': role_tc.id if role_tc else None,
        }
        try:
            _save_welcome_settings(self.settings)
        except Exception as e:
            await self.bot.notify_owner_error(e, interaction, extra_info="welcome_active: 儲存設定失敗")
            await interaction.response.send_message("❌ 儲存設定時發生錯誤，設定可能在重新啟動後遺失，已回報開發者。", ephemeral=True)
            return

        desc_lines = [f"✅ 歡迎訊息已啟用！", f"📢 歡迎頻道：{welcome_tc.mention}"]
        if rules_tc:
            desc_lines.append(f"📋 規則頻道：{rules_tc.mention}")
        if role_tc:
            desc_lines.append(f"🎭 身份組領取頻道：{role_tc.mention}")

        embed = discord.Embed(title="🎉 歡迎訊息設定完成", description="\n".join(desc_lines), color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="welcome_inactive", description="取消伺服器的歡迎訊息功能")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
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

        join_time = discord.utils.format_dt(member.joined_at or discord.utils.utcnow(), style='R')

        embed = discord.Embed(
            color=discord.Color.gold(),
            description=(
                f"{member.mention} 剛剛加入了我們！\n"
                f"你是第 **{member.guild.member_count}** 位成員，歡迎來到 **{member.guild.name}** 🎊"
            ),
            timestamp=discord.utils.utcnow()
        )

        # 大頭貼作為主視覺（embed 大圖），左上 author 顯示帳號名稱方便辨識。
        avatar_url = member.display_avatar.url
        embed.set_author(name=str(member), icon_url=avatar_url)
        embed.set_image(url=avatar_url)
        embed.set_thumbnail(url=member.guild.icon.url if member.guild.icon else None)

        # 指南區塊：把所有「新成員應該知道的事」集中在一起，而不是散落各處。
        guide_lines = [f"📅 加入時間：{join_time}"]
        rules_channel_id = settings.get('rules_channel_id')
        if rules_channel_id:
            rules_ch = member.guild.get_channel(rules_channel_id)
            if rules_ch:
                guide_lines.append(f"📋 出發前先看看 {rules_ch.mention} 的規則！")
        role_channel_id = settings.get('role_channel_id')
        if role_channel_id:
            role_ch = member.guild.get_channel(role_channel_id)
            if role_ch:
                guide_lines.append(f"🎭 到 {role_ch.mention} 領取你的身份組！")
        embed.add_field(name="📖 新成員指南", value="\n".join(guide_lines), inline=False)

        embed.set_footer(text=f"成員 ID：{member.id}")

        try:
            await channel.send(content=f"{member.mention} 歡迎加入！🎉", embed=embed)
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
