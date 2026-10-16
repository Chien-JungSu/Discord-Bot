import os
import json
import tempfile
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.database import (
    load_welcome_settings as _load_welcome_settings,
    save_welcome_settings as _save_welcome_settings,
)


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


# 設定面板閒置逾時（秒）：與其他 cog 相同慣例，每次操作都會重新計時
PANEL_IDLE_TIMEOUT = 120.0
# 頻道選單只列出可發文字訊息的頻道（文字頻道＋公告頻道）
_TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]


def _auto_role_problems(guild: discord.Guild, role: discord.Role) -> list[str]:
    """檢查機器人能否把此身份組自動發給新成員；回傳問題描述（空清單＝可以發放）。"""
    me = guild.me
    problems: list[str] = []
    if role.is_default():
        problems.append("不能選擇 @everyone")
    elif role.managed:
        problems.append("此身份組由整合服務（機器人／訂閱等）管理，無法手動發放")
    elif me is not None and me.top_role <= role:
        problems.append(f"機器人的最高身份組（{me.top_role.name}）沒有高於它，請到伺服器設定把機器人的身份組往上拉")
    if me is not None and not me.guild_permissions.manage_roles:
        problems.append("機器人缺少「管理身份組」權限")
    return problems


class WelcomeSetupView(discord.ui.View):
    """/welcome_active 互動設定面板：用下拉選單挑選頻道與自動身份組，按「儲存」才寫入。"""

    def __init__(self, cog: 'Welcome', guild: discord.Guild, initiator_id: int, current: Optional[dict]):
        super().__init__(timeout=PANEL_IDLE_TIMEOUT)
        self.cog = cog
        self.guild = guild
        self.initiator_id = initiator_id
        self.message: Optional[discord.Message] = None
        current = current or {}
        # 草稿：只存 ID，預先帶入目前設定（且確認對象仍存在）
        self.draft: dict[str, Optional[int]] = {
            key: (current.get(key) if self._exists(key, current.get(key)) else None)
            for key in ('welcome_channel_id', 'rules_channel_id', 'role_channel_id', 'auto_role_id')
        }
        self.status: Optional[str] = None
        self._refresh_components()

    def _exists(self, key: str, obj_id: Optional[int]) -> bool:
        if not obj_id:
            return False
        if key == 'auto_role_id':
            return self.guild.get_role(int(obj_id)) is not None
        return isinstance(self.guild.get_channel(int(obj_id)), discord.TextChannel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.initiator_id:
            await interaction.response.send_message('❌ 只有執行指令的人才能操作這個設定面板。', ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # ---------- 畫面 ----------
    def _channel_text(self, key: str) -> str:
        cid = self.draft.get(key)
        ch = self.guild.get_channel(int(cid)) if cid else None
        return ch.mention if ch else '（未設定）'

    def build_embed(self) -> discord.Embed:
        role_id = self.draft.get('auto_role_id')
        role = self.guild.get_role(int(role_id)) if role_id else None
        lines = [
            f"📢 **歡迎頻道**（必填）：{self._channel_text('welcome_channel_id')}",
            f"📋 **規則頻道**（選填）：{self._channel_text('rules_channel_id')}",
            f"🎭 **身份組領取頻道**（選填）：{self._channel_text('role_channel_id')}",
            f"🏷️ **新成員自動身份組**（選填）：{role.mention if role else '（未設定）'}",
            '',
            '從下方選單挑選後按「💾 儲存並啟用」才會生效。選填項目在選單中取消勾選即可清除。',
        ]
        embed = discord.Embed(
            title='🎉 歡迎訊息設定',
            description='\n'.join(lines),
            color=discord.Color.blurple(),
        )
        if self.status:
            embed.add_field(name='ℹ️ 狀態', value=self.status[:1024], inline=False)
        embed.set_footer(text='連續 120 秒無操作，面板會自動失效。')
        return embed

    def _channel_select(self, key: str, placeholder: str, row: int, required: bool) -> discord.ui.ChannelSelect:
        cid = self.draft.get(key)
        ch = self.guild.get_channel(int(cid)) if cid else None
        select = discord.ui.ChannelSelect(
            placeholder=placeholder,
            channel_types=_TEXT_CHANNEL_TYPES,
            min_values=1 if required else 0,
            max_values=1,
            row=row,
            default_values=[ch] if ch else [],
        )

        async def _callback(interaction: discord.Interaction):
            self.draft[key] = select.values[0].id if select.values else None
            self.status = None
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        select.callback = _callback
        return select

    def _refresh_components(self):
        """重建所有元件，讓選單的預設值與草稿同步（重繪後選單不會變回空白）。"""
        self.clear_items()
        self.add_item(self._channel_select('welcome_channel_id', '📢 選擇歡迎頻道（必填）', 0, required=True))
        self.add_item(self._channel_select('rules_channel_id', '📋 選擇規則頻道（選填）', 1, required=False))
        self.add_item(self._channel_select('role_channel_id', '🎭 選擇身份組領取頻道（選填）', 2, required=False))

        role_id = self.draft.get('auto_role_id')
        role = self.guild.get_role(int(role_id)) if role_id else None
        role_select = discord.ui.RoleSelect(
            placeholder='🏷️ 選擇新成員自動獲得的身份組（選填）',
            min_values=0,
            max_values=1,
            row=3,
            default_values=[role] if role else [],
        )

        async def _role_callback(interaction: discord.Interaction):
            if not role_select.values:
                self.draft['auto_role_id'] = None
                self.status = None
            else:
                picked = role_select.values[0]
                problems = _auto_role_problems(self.guild, picked)
                if problems:
                    self.status = f"❌ 無法使用 {picked.mention}：" + '；'.join(problems)
                else:
                    self.draft['auto_role_id'] = picked.id
                    self.status = None
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        role_select.callback = _role_callback
        self.add_item(role_select)

        save_btn = discord.ui.Button(
            label='💾 儲存並啟用',
            style=discord.ButtonStyle.success,
            row=4,
            disabled=not self.draft.get('welcome_channel_id'),
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

        cancel_btn = discord.ui.Button(label='取消', style=discord.ButtonStyle.secondary, row=4)
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    # ---------- 按鈕 ----------
    async def _on_save(self, interaction: discord.Interaction):
        errors: list[str] = []
        welcome_id = self.draft.get('welcome_channel_id')
        if not welcome_id or not isinstance(self.guild.get_channel(int(welcome_id)), discord.TextChannel):
            errors.append('• 請先選擇**歡迎頻道**（可能已被刪除）')
        role_id = self.draft.get('auto_role_id')
        if role_id:
            role = self.guild.get_role(int(role_id))
            if role is None:
                errors.append('• 所選的自動身份組已不存在，請重新選擇')
            else:
                # 儲存前再驗證一次：選取後到儲存之間，階層或權限可能已被調整
                problems = _auto_role_problems(self.guild, role)
                if problems:
                    errors.append(f"• 自動身份組 {role.mention}：" + '；'.join(problems))
        if errors:
            self.status = '❌ 無法儲存：\n' + '\n'.join(errors)
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        self.cog.settings[str(self.guild.id)] = dict(self.draft)
        try:
            _save_welcome_settings(self.cog.settings)
        except Exception as e:
            await self.cog.bot.notify_owner_error(e, interaction, extra_info="welcome_active: 儲存設定失敗")
            self.status = '❌ 儲存設定時發生錯誤，設定可能在重新啟動後遺失，已回報開發者。'
            self._refresh_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        role = self.guild.get_role(int(role_id)) if role_id else None
        desc_lines = ['✅ 歡迎訊息已啟用！', f"📢 歡迎頻道：{self._channel_text('welcome_channel_id')}"]
        if self.draft.get('rules_channel_id'):
            desc_lines.append(f"📋 規則頻道：{self._channel_text('rules_channel_id')}")
        if self.draft.get('role_channel_id'):
            desc_lines.append(f"🎭 身份組領取頻道：{self._channel_text('role_channel_id')}")
        if role:
            desc_lines.append(f"🏷️ 新成員自動身份組：{role.mention}")
        embed = discord.Embed(title='🎉 歡迎訊息設定完成', description='\n'.join(desc_lines), color=discord.Color.green())
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

    async def _on_cancel(self, interaction: discord.Interaction):
        self.stop()
        embed = discord.Embed(title='已取消', description='歡迎訊息設定未變更。', color=discord.Color.light_grey())
        await interaction.response.edit_message(embed=embed, view=None)


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: dict = _load_welcome_settings()

    @app_commands.command(name="welcome_active", description="開啟歡迎訊息設定面板（歡迎／規則／身份組頻道與新成員自動身份組）")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def welcome_active(self, interaction: discord.Interaction):
        # 頻道選單已限制只能選文字／公告頻道，因此不再需要事後驗證頻道型別
        current = self.settings.get(str(interaction.guild.id))
        view = WelcomeSetupView(self, interaction.guild, interaction.user.id, current)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @app_commands.command(name="welcome_inactive", description="取消伺服器的歡迎訊息功能")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)  # H3: 執行期檢查，伺服器端覆寫權限也擋得住
    async def welcome_inactive(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        if guild_id in self.settings:
            del self.settings[guild_id]
            try:
                _save_welcome_settings(self.settings)
            except Exception as e:
                await self.bot.notify_owner_error(e, interaction, extra_info="welcome_inactive: 儲存設定失敗")
                await interaction.followup.send("❌ 儲存設定時發生錯誤，可能需要再執行一次，已回報開發者。", ephemeral=True)
                return
            embed = discord.Embed(title="🔕 歡迎訊息已關閉", description="此伺服器的歡迎訊息功能已停用。", color=discord.Color.red())
        else:
            embed = discord.Embed(title="ℹ️ 歡迎訊息未啟用", description="此伺服器目前沒有啟用歡迎訊息功能。", color=discord.Color.light_grey())
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _give_auto_role(self, member: discord.Member, settings: dict):
        """把 /welcome_active 設定的自動身份組發給新成員（機器人帳號不發放）。"""
        role_id = settings.get('auto_role_id')
        if not role_id or member.bot:
            return
        guild = member.guild
        role = guild.get_role(int(role_id))
        if role is None:
            print(f"⚠️ 自動身份組：伺服器 {guild.name} ({guild.id}) 設定的身份組 {role_id} 已不存在，略過。")
            return
        # 設定後身份組階層或機器人權限可能被調整，發放前再檢查一次，避免每次都撞 403
        problems = _auto_role_problems(guild, role)
        if problems:
            print(f"⚠️ 自動身份組：無法在 {guild.name} 發放 {role.name}：{'；'.join(problems)}")
            await self.bot.notify_owner_error(
                RuntimeError('；'.join(problems)),
                extra_info=f"on_member_join: 無法發放自動身份組 {role.name} ({role.id})，伺服器={guild.name} ({guild.id})",
            )
            return
        try:
            await member.add_roles(role, reason="新成員自動身份組（/welcome_active 設定）")
        except discord.Forbidden as e:
            await self.bot.notify_owner_error(
                e, extra_info=f"on_member_join: 發放自動身份組被拒（權限不足），伺服器={guild.name} ({guild.id})"
            )
        except discord.HTTPException as e:
            print(f"❌ 發放自動身份組失敗: {e}")
            await self.bot.notify_owner_error(
                e, extra_info=f"on_member_join: 發放自動身份組失敗 member={member} guild={guild.name} ({guild.id})"
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = str(member.guild.id)
        settings = self.settings.get(guild_id)
        if not settings:
            return

        # 先發放自動身份組（與歡迎訊息互相獨立：歡迎頻道失效時仍會發放）
        await self._give_auto_role(member, settings)

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
