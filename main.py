import asyncio
import json
import os
import re
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
DATABASE_DIR = BASE_DIR / "database"
ASSETS_DIR = BASE_DIR / "assets"
SETTINGS_FILE = DATABASE_DIR / "settings.json"
PARTNERS_FILE = DATABASE_DIR / "partners.json"
TICKETS_FILE = DATABASE_DIR / "tickets.json"
PANELS_FILE = DATABASE_DIR / "panels.json"
TOKEN_FILE = BASE_DIR / "token.txt"
LOCK_FILE = BASE_DIR / ".bot_process.lock"

DEFAULT_SETTINGS = {
    "project_name": "HackStorm • SHOP",
    "owner_ids": [1389945313225080953],
    "verified_role_id": 1429401945671467028,
    "support_role_ids": [1416999397161566275, 1416999397161566276, 1416999397161566274],
    "embed_color": 725759,
    "ticket_channel_prefix": "support",
}


def ensure_files() -> None:
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    defaults = {
        SETTINGS_FILE: DEFAULT_SETTINGS,
        PARTNERS_FILE: {},
        TICKETS_FILE: {},
        PANELS_FILE: {},
    }
    for path, value in defaults.items():
        if not path.exists():
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            write_json(path, default)
            return default
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            write_json(path, default)
            return default
        return json.loads(raw)
    except Exception:
        backup = path.with_suffix(path.suffix + ".broken")
        try:
            path.replace(backup)
        except Exception:
            pass
        write_json(path, default)
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def settings() -> dict[str, Any]:
    data = read_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    changed = False
    for key, value in DEFAULT_SETTINGS.items():
        if key not in data:
            data[key] = value
            changed = True
    if changed:
        write_json(SETTINGS_FILE, data)
    return data


def project_name() -> str:
    return str(settings().get("project_name", "HackStorm • SHOP"))


def embed_color() -> int:
    return int(settings().get("embed_color", 725759))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_token() -> str:
    for key in ("DISCORD_TOKEN", "BOT_TOKEN", "TOKEN"):
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip().replace("Bot ", "", 1).strip().strip('"').strip("'")
    if TOKEN_FILE.exists():
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value.replace("Bot ", "", 1).strip().strip('"').strip("'")
    raise RuntimeError("Bot token not found. Add it in hosting panel or create token.txt")


def single_process_guard() -> None:
    current_pid = os.getpid()
    if LOCK_FILE.exists():
        try:
            old_pid_text = LOCK_FILE.read_text(encoding="utf-8").strip()
            old_pid = int(old_pid_text)
            if old_pid and old_pid != current_pid:
                try:
                    os.kill(old_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    pass
                except Exception:
                    pass
        except Exception:
            pass
    try:
        LOCK_FILE.write_text(str(current_pid), encoding="utf-8")
    except Exception:
        pass


def is_owner(user_id: int) -> bool:
    return user_id in [int(x) for x in settings().get("owner_ids", [])]


def has_panel_access(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    perms = member.guild_permissions
    return bool(perms.administrator or perms.manage_guild)


def is_support_member(member: discord.Member) -> bool:
    if has_panel_access(member):
        return True
    support_ids = {int(x) for x in settings().get("support_role_ids", [])}
    return any(role.id in support_ids for role in member.roles)


def clean_channel_name(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9а-яё_-]+", "-", value, flags=re.IGNORECASE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:40] or "user"


def image_from_args(image: Optional[discord.Attachment], image_url: Optional[str]) -> Optional[str]:
    if image is not None:
        return image.url
    if image_url and image_url.strip():
        return image_url.strip()
    return None


def make_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=embed_color())
    embed.set_author(name=project_name())
    embed.set_footer(text=project_name())
    return embed


def local_logo_file() -> Optional[discord.File]:
    path = ASSETS_DIR / "logo.png"
    if path.exists():
        return discord.File(path, filename="logo.png")
    return None


def local_logo_url() -> Optional[str]:
    path = ASSETS_DIR / "logo.png"
    if path.exists():
        return "attachment://logo.png"
    return None


async def send_public_panel(
    interaction: discord.Interaction,
    embed: discord.Embed,
    view: discord.ui.View,
    image_url: Optional[str] = None,
) -> None:
    if image_url:
        embed.set_image(url=image_url)
        await interaction.followup.send(embed=embed, view=view)
        return
    file = local_logo_file()
    if file:
        embed.set_image(url=local_logo_url())
        await interaction.followup.send(embed=embed, view=view, file=file)
    else:
        await interaction.followup.send(embed=embed, view=view)


class VerifyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Пройти верификацию", style=discord.ButtonStyle.primary, custom_id="hackstorm_verify_button")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        role_id = int(settings().get("verified_role_id", 1429401945671467028))
        role = interaction.guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message("Роль верификации не найдена. Проверьте ID роли в database/settings.json.", ephemeral=True)
            return

        try:
            await interaction.user.add_roles(role, reason="Verification completed")
        except discord.Forbidden:
            await interaction.response.send_message("У бота нет прав выдать эту роль. Поднимите роль бота выше роли верификации.", ephemeral=True)
            return
        except Exception as exc:
            await interaction.response.send_message(f"Не удалось выдать роль: {exc}", ephemeral=True)
            return

        await interaction.response.send_message("Верификация завершена. Роль выдана.", ephemeral=True)


class PartnerPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Перейти к партнёрам", style=discord.ButtonStyle.secondary, custom_id="hackstorm_partners_open")
    async def open_partners(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        partners = read_json(PARTNERS_FILE, {})
        if not partners:
            await interaction.response.send_message("Список партнёров пока пуст.", ephemeral=True)
            return

        options = []
        for name, url in list(partners.items())[:25]:
            options.append(discord.SelectOption(label=str(name)[:100], value=str(name)[:100], description="Открыть ссылку партнёра"))

        view = PartnerSelectView(options)
        await interaction.response.send_message("Выберите партнёра из списка.", view=view, ephemeral=True)


class PartnerSelectView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(timeout=120)
        self.add_item(PartnerSelect(options))


class PartnerSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="Выберите партнёра", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        partners = read_json(PARTNERS_FILE, {})
        name = self.values[0]
        url = partners.get(name)
        if not url:
            await interaction.response.send_message("Партнёр не найден. Возможно, список был изменён.", ephemeral=True)
            return
        embed = make_embed("Партнёр", f"Название: {name}\nСсылка: {url}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать тикет", style=discord.ButtonStyle.primary, custom_id="hackstorm_ticket_create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Тикеты доступны только на сервере.", ephemeral=True)
            return

        guild = interaction.guild
        member = interaction.user
        tickets = read_json(TICKETS_FILE, {})

        for channel_id, data in list(tickets.items()):
            if int(data.get("user_id", 0)) == member.id:
                channel = guild.get_channel(int(channel_id))
                if isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message(f"У вас уже есть открытый тикет: {channel.mention}", ephemeral=True)
                    return
                tickets.pop(channel_id, None)
                write_json(TICKETS_FILE, tickets)

        cfg = settings()
        prefix = clean_channel_name(str(cfg.get("ticket_channel_prefix", "support")))
        user_part = clean_channel_name(member.display_name or member.name)
        channel_name = f"{prefix}-{user_part}"[:90]

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)

        support_mentions = []
        for role_id in cfg.get("support_role_ids", []):
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
                support_mentions.append(role.mention)

        category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None

        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                category=category,
                reason=f"Support ticket created by {member} ({member.id})",
            )
        except discord.Forbidden:
            await interaction.response.send_message("У бота нет прав создать приватный канал. Нужны права Manage Channels.", ephemeral=True)
            return
        except Exception as exc:
            await interaction.response.send_message(f"Не удалось создать тикет: {exc}", ephemeral=True)
            return

        tickets[str(channel.id)] = {
            "user_id": member.id,
            "guild_id": guild.id,
            "created_at": utc_now_iso(),
        }
        write_json(TICKETS_FILE, tickets)

        embed = make_embed(
            "Тикет поддержки создан",
            "Опишите вопрос или проблему одним сообщением. Команда поддержки ответит здесь.",
        )
        embed.add_field(name="Пользователь", value=member.mention, inline=False)
        embed.add_field(name="Статус", value="Открыт", inline=True)
        embed.add_field(name="Проект", value=project_name(), inline=True)
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        content = " ".join(support_mentions) if support_mentions else None
        await channel.send(
            content=content,
            embed=embed,
            view=TicketControlView(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )

        await interaction.response.send_message(f"Тикет создан: {channel.mention}", ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Удалить тикет", style=discord.ButtonStyle.danger, custom_id="hackstorm_ticket_delete")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Канал тикета не найден.", ephemeral=True)
            return

        tickets = read_json(TICKETS_FILE, {})
        data = tickets.get(str(interaction.channel.id), {})
        ticket_owner_id = int(data.get("user_id", 0))
        can_delete = interaction.user.id == ticket_owner_id or is_support_member(interaction.user)
        if not can_delete:
            await interaction.response.send_message("Удалить тикет может только создатель тикета или поддержка.", ephemeral=True)
            return

        await interaction.response.send_message("Тикет будет удалён через 3 секунды.", ephemeral=True)
        await asyncio.sleep(3)
        tickets.pop(str(interaction.channel.id), None)
        write_json(TICKETS_FILE, tickets)
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user} ({interaction.user.id})")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            try:
                await interaction.followup.send("У бота нет прав удалить этот канал. Нужны права Manage Channels.", ephemeral=True)
            except Exception:
                pass
        except Exception:
            pass


class HackStormBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.synced_once = False

    async def setup_hook(self) -> None:
        self.add_view(VerifyView())
        self.add_view(PartnerPanelView())
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        try:
            await self.tree.sync()
        except Exception as exc:
            print(f"Slash command sync failed: {exc}")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} | Project: {project_name()}")


ensure_files()
single_process_guard()
bot = HackStormBot()


async def require_panel_access(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return False
    if not has_panel_access(interaction.user):
        await interaction.response.send_message("Недостаточно прав для выполнения команды.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="verify", description="Опубликовать панель верификации HackStorm SHOP")
@app_commands.describe(image="Картинка для панели", image_url="Ссылка на картинку для панели")
async def slash_verify(
    interaction: discord.Interaction,
    image: Optional[discord.Attachment] = None,
    image_url: Optional[str] = None,
) -> None:
    if not await require_panel_access(interaction):
        return
    await interaction.response.defer(thinking=True)
    final_image = image_from_args(image, image_url)
    embed = make_embed(
        "Верификация",
        "Нажмите кнопку ниже, чтобы получить доступ к серверу. После нажатия бот выдаст роль верификации.",
    )
    embed.add_field(name="Проект", value=project_name(), inline=False)
    embed.add_field(name="Доступ", value="Выдаётся автоматически после подтверждения.", inline=False)
    await send_public_panel(interaction, embed, VerifyView(), final_image)


@bot.tree.command(name="partners", description="Опубликовать панель партнёров HackStorm SHOP")
@app_commands.describe(image="Картинка для панели", image_url="Ссылка на картинку для панели")
async def slash_partners(
    interaction: discord.Interaction,
    image: Optional[discord.Attachment] = None,
    image_url: Optional[str] = None,
) -> None:
    if not await require_panel_access(interaction):
        return
    await interaction.response.defer(thinking=True)
    final_image = image_from_args(image, image_url)
    embed = make_embed(
        "Партнёры",
        "Чтобы посмотреть список партнёров, нажмите кнопку ниже. Ссылка будет видна только вам.",
    )
    partners = read_json(PARTNERS_FILE, {})
    embed.add_field(name="Количество партнёров", value=str(len(partners)), inline=True)
    embed.add_field(name="Проект", value=project_name(), inline=True)
    await send_public_panel(interaction, embed, PartnerPanelView(), final_image)


@bot.tree.command(name="partnersadd", description="Добавить партнёра в список")
@app_commands.describe(name="Название партнёра", url="Ссылка на партнёра")
async def slash_partners_add(interaction: discord.Interaction, name: str, url: str) -> None:
    if not await require_panel_access(interaction):
        return
    name = name.strip()
    url = url.strip()
    if not name or not url:
        await interaction.response.send_message("Название и ссылка не могут быть пустыми.", ephemeral=True)
        return
    if len(name) > 100:
        await interaction.response.send_message("Название партнёра должно быть не длиннее 100 символов.", ephemeral=True)
        return
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("discord.gg/") or url.startswith("discord.com/")):
        await interaction.response.send_message("Ссылка должна начинаться с http, https, discord.gg или discord.com.", ephemeral=True)
        return

    partners = read_json(PARTNERS_FILE, {})
    partners[name] = url
    write_json(PARTNERS_FILE, partners)
    await interaction.response.send_message(f"Партнёр добавлен: {name}", ephemeral=True)


@bot.tree.command(name="nabor", description="Опубликовать панель поддержки с созданием тикетов")
@app_commands.describe(image="Картинка для панели", image_url="Ссылка на картинку для панели")
async def slash_nabor(
    interaction: discord.Interaction,
    image: Optional[discord.Attachment] = None,
    image_url: Optional[str] = None,
) -> None:
    if not await require_panel_access(interaction):
        return
    await interaction.response.defer(thinking=True)
    final_image = image_from_args(image, image_url)
    embed = make_embed(
        "Поддержка",
        "Создайте тикет, если нужна помощь, покупка, проверка заказа или вопрос по проекту. Канал будет виден только вам и команде поддержки.",
    )
    embed.add_field(name="Проект", value=project_name(), inline=False)
    embed.add_field(name="Как работает", value="Нажмите кнопку ниже. Бот создаст приватный канал и уведомит поддержку.", inline=False)
    await send_public_panel(interaction, embed, TicketPanelView(), final_image)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    text = "Произошла ошибка при выполнении команды."
    if isinstance(error, app_commands.CommandOnCooldown):
        text = f"Команда временно недоступна. Повторите через {round(error.retry_after)} секунд."
    elif isinstance(error, app_commands.MissingPermissions):
        text = "Недостаточно прав для выполнения команды."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass
    print(f"Command error: {error}")


if __name__ == "__main__":
    bot.run(get_token())
