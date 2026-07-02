"""
Bot Telegram — AnimeUnity + StreamingCommunity
Usa python-telegram-bot v20+ (async)

Variabili d'ambiente richieste:
    TELEGRAM_TOKEN   — token del bot (@BotFather)
    ALLOWED_USER_ID  — (opzionale) tuo user_id Telegram per uso privato

Comandi:
    /start           — benvenuto
    /anime <query>   — cerca su AnimeUnity
    /film  <query>   — cerca su StreamingCommunity
    /help            — lista comandi
"""

import asyncio
import logging
import os
import re
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from animeunity import AnimeUnity
from streamingcommunity import StreamingCommunity

# ---------------------------------------------------------------------------
# Setup logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti conversazione
# ---------------------------------------------------------------------------

# AnimeUnity states
AU_RESULTS, AU_EPISODES, AU_WAITING_EP = range(3)

# StreamingCommunity states
SC_RESULTS, SC_EPISODES, SC_WAITING_EP = range(10, 13)

# ---------------------------------------------------------------------------
# Controllo accesso opzionale
# ---------------------------------------------------------------------------

ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == ALLOWED_USER_ID

async def deny(update: Update) -> None:
    await update.effective_message.reply_text("⛔ Non autorizzato.")

# ---------------------------------------------------------------------------
# Helpers UI
# ---------------------------------------------------------------------------

def _chunk(lst: list, n: int) -> list[list]:
    """Divide una lista in righe da n elementi."""
    return [lst[i:i+n] for i in range(0, len(lst), n)]

def _results_keyboard(results: list[dict], prefix: str) -> InlineKeyboardMarkup:
    """
    Crea una tastiera inline con un bottone per ogni risultato.
    callback_data = "prefix:index"
    """
    buttons = [
        InlineKeyboardButton(
            text=f"{'🎬' if r.get('type')=='movie' else '📺'} {r['name'][:40]}",
            callback_data=f"{prefix}:{i}",
        )
        for i, r in enumerate(results)
    ]
    # 1 bottone per riga (titoli lunghi)
    keyboard = [[b] for b in buttons]
    keyboard.append([InlineKeyboardButton("❌ Annulla", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(keyboard)

def _episodes_keyboard(episodes: list[dict], prefix: str, page: int = 0) -> InlineKeyboardMarkup:
    """
    Tastiera episodi con paginazione da 20 per volta.
    callback_data = "prefix:ep:ep_index"
    callback_data = "prefix:page:N"
    """
    page_size = 20
    start = page * page_size
    page_eps = episodes[start:start + page_size]

    buttons = [
        InlineKeyboardButton(
            text=_ep_label(ep),
            callback_data=f"{prefix}:ep:{episodes.index(ep)}",
        )
        for ep in page_eps
    ]
    rows = _chunk(buttons, 4)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prec", callback_data=f"{prefix}:page:{page-1}"))
    if start + page_size < len(episodes):
        nav.append(InlineKeyboardButton("➡️ Succ", callback_data=f"{prefix}:page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("▶️ Tutti", callback_data=f"{prefix}:all"),
        InlineKeyboardButton("❌ Annulla", callback_data=f"{prefix}:cancel"),
    ])
    return InlineKeyboardMarkup(rows)

def _ep_label(ep: dict) -> str:
    """Etichetta breve per un episodio."""
    if "season_number" in ep:
        return f"S{ep['season_number']:02d}E{ep['number']:02d}"
    return f"Ep {ep['number']}"

def _info_text(info: dict) -> str:
    """Testo formattato dei dettagli di un titolo."""
    lines = [f"*{_esc(info['name'])}*"]
    if info.get("year"):
        lines[0] += f" \\({info['year']}\\)"
    if info.get("score"):
        lines.append(f"⭐ {_esc(info['score'])}")
    if info.get("genres"):
        lines.append(f"🎭 {_esc(', '.join(info['genres'][:4]))}")
    if info.get("plot"):
        plot = info["plot"][:200] + ("…" if len(info["plot"]) > 200 else "")
        lines.append(f"\n_{_esc(plot)}_")
    return "\n".join(lines)

def _esc(text: str) -> str:
    """Escape caratteri speciali MarkdownV2."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(text))

def _link_text(name: str, m3u8: str | None) -> str:
    if not m3u8:
        return "❌ Link non trovato\\."
    return (
        f"✅ *{_esc(name)}*\n\n"
        f"`{_esc(m3u8)}`\n\n"
        f"Copia il link e aprilo con mpv o VLC\\."
    )

# ---------------------------------------------------------------------------
# /start  /help
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update); return
    await update.message.reply_text(
        "👋 Benvenuto\\!\n\n"
        "*/anime* \\<titolo\\> — cerca su AnimeUnity\n"
        "*/film* \\<titolo\\> — cerca su StreamingCommunity\n"
        "*/help* — questo messaggio",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)

# ---------------------------------------------------------------------------
# ── ANIMEUNITY ──────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _au(ctx: ContextTypes.DEFAULT_TYPE) -> AnimeUnity:
    if "au" not in ctx.bot_data:
        ctx.bot_data["au"] = AnimeUnity()
    return ctx.bot_data["au"]

async def cmd_anime(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update): await deny(update); return ConversationHandler.END
    query = " ".join(ctx.args)
    if not query:
        await update.message.reply_text("Uso: /anime \\<titolo\\>", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    msg = await update.message.reply_text(f"🔍 Cerco *{_esc(query)}* su AnimeUnity…", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        results = await asyncio.get_event_loop().run_in_executor(None, _au(ctx).search, query)
    except Exception as e:
        await msg.edit_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not results:
        await msg.edit_text("Nessun risultato trovato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    ctx.user_data["au_results"] = results
    ctx.user_data["au_msg_id"] = msg.message_id
    await msg.edit_text(
        f"🔍 Risultati per *{_esc(query)}*:",
        reply_markup=_results_keyboard(results, "au_r"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return AU_RESULTS

async def au_pick_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data  # "au_r:N" | "au_r:cancel"

    if data.endswith(":cancel"):
        await query.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    idx     = int(data.split(":")[1])
    results = ctx.user_data["au_results"]
    chosen  = results[idx]

    await query.edit_message_text(
        f"⏳ Carico *{_esc(chosen['name'])}*…", parse_mode=ParseMode.MARKDOWN_V2
    )

    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None, _au(ctx).load, chosen["url"]
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    ctx.user_data["au_info"] = info
    eps = info["episodes"]

    if not eps:
        await query.edit_message_text("Nessun episodio disponibile\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
    await query.edit_message_text(
        text,
        reply_markup=_episodes_keyboard(eps, "au_e"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return AU_EPISODES

async def au_pick_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    info = ctx.user_data["au_info"]
    eps  = info["episodes"]

    if data.endswith(":cancel"):
        await query.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if ":page:" in data:
        page = int(data.split(":")[-1])
        text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
        await query.edit_message_text(
            text,
            reply_markup=_episodes_keyboard(eps, "au_e", page),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return AU_EPISODES

    if data.endswith(":all"):
        await query.edit_message_text(
            f"⏳ Estraggo tutti i {len(eps)} link\\. Potrebbe richiedere qualche minuto…",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _au_send_links(query, ctx, eps)
        return ConversationHandler.END

    # Episodio singolo
    ep_idx = int(data.split(":")[-1])
    ep     = eps[ep_idx]
    await query.edit_message_text(
        f"⏳ Estraggo *{_esc(_ep_label(ep))}*…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        m3u8 = await asyncio.get_event_loop().run_in_executor(
            None, _au(ctx).get_episode_link, ep["url"]
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    await query.edit_message_text(
        _link_text(_ep_label(ep), m3u8), parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END

async def _au_send_links(query: Any, ctx: ContextTypes.DEFAULT_TYPE, eps: list[dict]) -> None:
    """Estrae e manda i link M3U8 di tutti gli episodi, a blocchi."""
    au   = _au(ctx)
    chat = query.message.chat_id
    bot  = ctx.bot

    for ep in eps:
        try:
            m3u8 = await asyncio.get_event_loop().run_in_executor(
                None, au.get_episode_link, ep["url"]
            )
            await bot.send_message(
                chat_id=chat,
                text=_link_text(_ep_label(ep), m3u8),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            await bot.send_message(chat_id=chat, text=f"❌ {_ep_label(ep)}: {e}")
        await asyncio.sleep(0.3)  # evita flood


# ---------------------------------------------------------------------------
# ── STREAMING COMMUNITY ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _sc(ctx: ContextTypes.DEFAULT_TYPE) -> StreamingCommunity:
    if "sc" not in ctx.bot_data:
        ctx.bot_data["sc"] = StreamingCommunity()
    return ctx.bot_data["sc"]

async def cmd_film(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update): await deny(update); return ConversationHandler.END
    query = " ".join(ctx.args)
    if not query:
        await update.message.reply_text("Uso: /film \\<titolo\\>", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    msg = await update.message.reply_text(
        f"🔍 Cerco *{_esc(query)}* su StreamingCommunity…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        results = await asyncio.get_event_loop().run_in_executor(None, _sc(ctx).search, query)
    except Exception as e:
        await msg.edit_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not results:
        await msg.edit_text("Nessun risultato trovato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    ctx.user_data["sc_results"] = results
    await msg.edit_text(
        f"🔍 Risultati per *{_esc(query)}*:",
        reply_markup=_results_keyboard(results, "sc_r"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return SC_RESULTS

async def sc_pick_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.endswith(":cancel"):
        await query.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    idx     = int(data.split(":")[1])
    results = ctx.user_data["sc_results"]
    chosen  = results[idx]

    await query.edit_message_text(
        f"⏳ Carico *{_esc(chosen['name'])}*…", parse_mode=ParseMode.MARKDOWN_V2
    )

    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None, _sc(ctx).load, chosen["url"]
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    ctx.user_data["sc_info"] = info

    # ── Film: estrai subito il link ──────────────────────────────────────────
    if info["type"] == "movie":
        await query.edit_message_text(
            f"⏳ Film trovato\\. Estraggo il link…", parse_mode=ParseMode.MARKDOWN_V2
        )
        try:
            m3u8 = await asyncio.get_event_loop().run_in_executor(
                None, _sc(ctx).get_movie_link, info
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
            return ConversationHandler.END

        text = _info_text(info) + "\n\n" + _link_text(info["name"], m3u8)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    # ── Serie TV: mostra episodi ─────────────────────────────────────────────
    eps = info.get("episodes", [])
    if not eps:
        await query.edit_message_text("Nessun episodio disponibile\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
    await query.edit_message_text(
        text,
        reply_markup=_episodes_keyboard(eps, "sc_e"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return SC_EPISODES

async def sc_pick_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    info = ctx.user_data["sc_info"]
    eps  = info["episodes"]

    if data.endswith(":cancel"):
        await query.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if ":page:" in data:
        page = int(data.split(":")[-1])
        text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
        await query.edit_message_text(
            text,
            reply_markup=_episodes_keyboard(eps, "sc_e", page),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return SC_EPISODES

    if data.endswith(":all"):
        await query.edit_message_text(
            f"⏳ Estraggo tutti i {len(eps)} link\\. Potrebbe richiedere qualche minuto…",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _sc_send_links(query, ctx, eps)
        return ConversationHandler.END

    ep_idx = int(data.split(":")[-1])
    ep     = eps[ep_idx]
    await query.edit_message_text(
        f"⏳ Estraggo *{_esc(_ep_label(ep))}*…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        m3u8 = await asyncio.get_event_loop().run_in_executor(
            None, _sc(ctx).get_episode_link, ep
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    await query.edit_message_text(
        _link_text(_ep_label(ep), m3u8), parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END

async def _sc_send_links(query: Any, ctx: ContextTypes.DEFAULT_TYPE, eps: list[dict]) -> None:
    sc   = _sc(ctx)
    chat = query.message.chat_id
    bot  = ctx.bot

    for ep in eps:
        try:
            m3u8 = await asyncio.get_event_loop().run_in_executor(
                None, sc.get_episode_link, ep
            )
            await bot.send_message(
                chat_id=chat,
                text=_link_text(_ep_label(ep), m3u8),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            await bot.send_message(chat_id=chat, text=f"❌ {_ep_label(ep)}: {e}")
        await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operazione annullata\\.", parse_mode=ParseMode.MARKDOWN_V2)
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    app   = Application.builder().token(token).build()

    # ── ConversationHandler AnimeUnity ───────────────────────────────────────
    au_conv = ConversationHandler(
        entry_points=[CommandHandler("anime", cmd_anime)],
        states={
            AU_RESULTS:  [CallbackQueryHandler(au_pick_result,  pattern=r"^au_r:")],
            AU_EPISODES: [CallbackQueryHandler(au_pick_episode, pattern=r"^au_e:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    # ── ConversationHandler StreamingCommunity ───────────────────────────────
    sc_conv = ConversationHandler(
        entry_points=[CommandHandler("film", cmd_film)],
        states={
            SC_RESULTS:  [CallbackQueryHandler(sc_pick_result,  pattern=r"^sc_r:")],
            SC_EPISODES: [CallbackQueryHandler(sc_pick_episode, pattern=r"^sc_e:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(au_conv)
    app.add_handler(sc_conv)

    log.info("Bot avviato in polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
