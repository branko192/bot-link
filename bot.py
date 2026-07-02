"""
Bot Telegram — AnimeUnity + StreamingCommunity
Usa python-telegram-bot v20+ (async)

Variabili d'ambiente richieste:
    TELEGRAM_TOKEN   — token del bot (@BotFather)
    ALLOWED_USER_ID  — (opzionale) tuo user_id Telegram per uso privato
"""

import asyncio
import io
import logging
import os
import re
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from animeunity import AnimeUnity, MAIN_URL as AU_URL
from streamingcommunity import StreamingCommunity, MAIN_URL as SC_URL

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stati conversazione
# ---------------------------------------------------------------------------

AU_RESULTS, AU_EPISODES = range(2)
SC_RESULTS, SC_EPISODES = range(10, 12)

# ---------------------------------------------------------------------------
# Accesso
# ---------------------------------------------------------------------------

ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == ALLOWED_USER_ID

async def deny(update: Update) -> None:
    await update.effective_message.reply_text("⛔ Non autorizzato.")

# ---------------------------------------------------------------------------
# Helper: chiave titolo unificata
# AnimeUnity  → "title"
# StreamingCommunity → "name"
# ---------------------------------------------------------------------------

def _get_title(item: dict) -> str:
    return item.get("title") or item.get("name") or "?"

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _chunk(lst: list, n: int) -> list[list]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]

def _results_keyboard(results: list[dict], prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{'🎬' if r.get('type')=='movie' else '📺'} {_get_title(r)[:40]}",
            callback_data=f"{prefix}:{i}",
        )
        for i, r in enumerate(results)
    ]
    keyboard = [[b] for b in buttons]
    keyboard.append([InlineKeyboardButton("❌ Annulla", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(keyboard)

def _episodes_keyboard(episodes: list[dict], prefix: str, page: int = 0) -> InlineKeyboardMarkup:
    page_size = 20
    start     = page * page_size
    page_eps  = episodes[start:start + page_size]

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
    if "season_number" in ep:
        return f"S{ep['season_number']:02d}E{ep['number']:02d}"
    return f"Ep {ep['number']}"

def _esc(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(text))

def _info_text(info: dict) -> str:
    name  = _get_title(info)
    lines = [f"*{_esc(name)}*"]
    year  = info.get("year") or info.get("date", "")
    if year:
        lines[0] += f" \\({_esc(str(year)[:4])}\\)"
    score = info.get("score")
    if score:
        lines.append(f"⭐ {_esc(str(score))}")
    genres = info.get("genres", [])
    if genres:
        lines.append(f"🎭 {_esc(', '.join(genres[:4]))}")
    plot = info.get("plot", "")
    if plot:
        short = plot[:200] + ("…" if len(plot) > 200 else "")
        lines.append(f"\n_{_esc(short)}_")
    return "\n".join(lines)

def _make_m3u(label: str, m3u8: str, referer: str) -> io.BytesIO:
    """
    Genera un file .m3u8 con direttive #EXTVLCOPT per impostare
    il Referer header — necessario perché VixCloud blocca con 403
    le richieste senza Referer corretto.
    Funziona con VLC, mpv, e qualsiasi player che supporta .m3u8.
    """
    content = (
        "#EXTM3U\n"
        f"#EXTINF:-1,{label}\n"
        f"#EXTVLCOPT:http-referrer={referer}\n"
        f"#EXTVLCOPT:network-caching=1000\n"
        f"{m3u8}\n"
    )
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"{re.sub(r'[^\\w\\s-]', '', label)[:40]}.m3u8"
    return buf

def _link_caption(label: str, m3u8: str) -> str:
    """Testo della didascalia allegata al file .m3u8."""
    return (
        f"✅ *{_esc(label)}*\n\n"
        f"Apri il file \\. m3u8 con VLC o mpv\\.\n\n"
        f"Oppure copia il link raw:\n"
        f"`{_esc(m3u8)}`"
    )

async def _send_link(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    label: str,
    m3u8: str | None,
    referer: str,
    reply_to_message_id: int | None = None,
) -> None:
    """
    Manda il link M3U8 come file .m3u8 allegato (con Referer) + testo.
    Se m3u8 è None manda solo il messaggio di errore.
    """
    if not m3u8:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Link non trovato per *{_esc(label)}*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    m3u_file = _make_m3u(label, m3u8, referer)
    await ctx.bot.send_document(
        chat_id=chat_id,
        document=InputFile(m3u_file, filename=m3u_file.name),
        caption=_link_caption(label, m3u8),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_to_message_id=reply_to_message_id,
    )

async def _safe_answer(query) -> None:
    """answer() con retry su TimedOut — va chiamato subito all'inizio di ogni callback."""
    for attempt in range(3):
        try:
            await query.answer()
            return
        except (TimedOut, NetworkError):
            if attempt < 2:
                await asyncio.sleep(1)

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
        "*/cancel* — annulla operazione in corso\n"
        "*/help* — questo messaggio",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)

# ---------------------------------------------------------------------------
# Error handler globale
# ---------------------------------------------------------------------------

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    log.error("Eccezione:", exc_info=err)

    if isinstance(err, (TimedOut, NetworkError)):
        # Errori di rete transitori — non avvisare l'utente
        return

    # Avvisa l'utente per altri errori
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"❌ Errore inatteso: {_esc(str(err))}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            pass

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

    msg = await update.message.reply_text(
        f"🔍 Cerco *{_esc(query)}* su AnimeUnity…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        results = await asyncio.get_event_loop().run_in_executor(None, _au(ctx).search, query)
    except Exception as e:
        await msg.edit_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if not results:
        await msg.edit_text("Nessun risultato trovato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    ctx.user_data["au_results"] = results
    await msg.edit_text(
        f"🔍 Risultati per *{_esc(query)}*:",
        reply_markup=_results_keyboard(results, "au_r"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return AU_RESULTS

async def au_pick_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await _safe_answer(q)

    if q.data.endswith(":cancel"):
        await q.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    idx     = int(q.data.split(":")[1])
    chosen  = ctx.user_data["au_results"][idx]
    name    = _get_title(chosen)

    await q.edit_message_text(
        f"⏳ Carico *{_esc(name)}*…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, _au(ctx).load, chosen["url"])
    except Exception as e:
        await q.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    ctx.user_data["au_info"] = info
    eps = info["episodes"]

    if not eps:
        await q.edit_message_text("Nessun episodio disponibile\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
    await q.edit_message_text(
        text,
        reply_markup=_episodes_keyboard(eps, "au_e"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return AU_EPISODES

async def au_pick_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q    = update.callback_query
    await _safe_answer(q)
    data = q.data
    info = ctx.user_data["au_info"]
    eps  = info["episodes"]

    if data.endswith(":cancel"):
        await q.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if ":page:" in data:
        page = int(data.split(":")[-1])
        text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
        await q.edit_message_text(
            text,
            reply_markup=_episodes_keyboard(eps, "au_e", page),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return AU_EPISODES

    if data.endswith(":all"):
        await q.edit_message_text(
            f"⏳ Estraggo tutti i {len(eps)} link\\. Potrebbe richiedere qualche minuto…",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _au_send_links(q, ctx, eps)
        return ConversationHandler.END

    ep_idx = int(data.split(":")[-1])
    ep     = eps[ep_idx]
    await q.edit_message_text(
        f"⏳ Estraggo *{_esc(_ep_label(ep))}*…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        m3u8 = await asyncio.get_event_loop().run_in_executor(
            None, _au(ctx).get_episode_link, ep["url"]
        )
    except Exception as e:
        await q.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    await q.edit_message_text("✅ Link estratto\\! Invio il file…", parse_mode=ParseMode.MARKDOWN_V2)
    await _send_link(ctx, q.message.chat_id, _ep_label(ep), m3u8, referer=AU_URL)
    return ConversationHandler.END

async def _au_send_links(q: Any, ctx: ContextTypes.DEFAULT_TYPE, eps: list[dict]) -> None:
    au   = _au(ctx)
    chat = q.message.chat_id
    for ep in eps:
        try:
            m3u8 = await asyncio.get_event_loop().run_in_executor(
                None, au.get_episode_link, ep["url"]
            )
            await _send_link(ctx, chat, _ep_label(ep), m3u8, referer=AU_URL)
        except Exception as e:
            await ctx.bot.send_message(chat_id=chat, text=f"❌ {_ep_label(ep)}: {e}")
        await asyncio.sleep(0.4)

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
    q = update.callback_query
    await _safe_answer(q)

    if q.data.endswith(":cancel"):
        await q.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    idx    = int(q.data.split(":")[1])
    chosen = ctx.user_data["sc_results"][idx]
    name   = _get_title(chosen)

    await q.edit_message_text(
        f"⏳ Carico *{_esc(name)}*…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, _sc(ctx).load, chosen["url"])
    except Exception as e:
        await q.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    ctx.user_data["sc_info"] = info

    # Film → link diretto
    if info["type"] == "movie":
        await q.edit_message_text(
            f"⏳ Film trovato\\. Estraggo il link…", parse_mode=ParseMode.MARKDOWN_V2
        )
        try:
            m3u8 = await asyncio.get_event_loop().run_in_executor(
                None, _sc(ctx).get_movie_link, info
            )
        except Exception as e:
            await q.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
            return ConversationHandler.END

        await q.edit_message_text(
            _info_text(info) + "\n\n✅ Link estratto\\! Invio il file…",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _send_link(ctx, q.message.chat_id, name, m3u8, referer=SC_URL)
        return ConversationHandler.END

    # Serie → episodi
    eps = info.get("episodes", [])
    if not eps:
        await q.edit_message_text("Nessun episodio disponibile\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
    await q.edit_message_text(
        text,
        reply_markup=_episodes_keyboard(eps, "sc_e"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return SC_EPISODES

async def sc_pick_episode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q    = update.callback_query
    await _safe_answer(q)
    data = q.data
    info = ctx.user_data["sc_info"]
    eps  = info["episodes"]

    if data.endswith(":cancel"):
        await q.edit_message_text("Annullato\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    if ":page:" in data:
        page = int(data.split(":")[-1])
        text = _info_text(info) + f"\n\n📋 *{len(eps)} episodi disponibili*"
        await q.edit_message_text(
            text,
            reply_markup=_episodes_keyboard(eps, "sc_e", page),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return SC_EPISODES

    if data.endswith(":all"):
        await q.edit_message_text(
            f"⏳ Estraggo tutti i {len(eps)} link\\. Potrebbe richiedere qualche minuto…",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _sc_send_links(q, ctx, eps)
        return ConversationHandler.END

    ep_idx = int(data.split(":")[-1])
    ep     = eps[ep_idx]
    await q.edit_message_text(
        f"⏳ Estraggo *{_esc(_ep_label(ep))}*…", parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        m3u8 = await asyncio.get_event_loop().run_in_executor(
            None, _sc(ctx).get_episode_link, ep
        )
    except Exception as e:
        await q.edit_message_text(f"❌ Errore: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    await q.edit_message_text("✅ Link estratto\\! Invio il file…", parse_mode=ParseMode.MARKDOWN_V2)
    await _send_link(ctx, q.message.chat_id, _ep_label(ep), m3u8, referer=SC_URL)
    return ConversationHandler.END

async def _sc_send_links(q: Any, ctx: ContextTypes.DEFAULT_TYPE, eps: list[dict]) -> None:
    sc   = _sc(ctx)
    chat = q.message.chat_id
    for ep in eps:
        try:
            m3u8 = await asyncio.get_event_loop().run_in_executor(
                None, sc.get_episode_link, ep
            )
            await _send_link(ctx, chat, _ep_label(ep), m3u8, referer=SC_URL)
        except Exception as e:
            await ctx.bot.send_message(chat_id=chat, text=f"❌ {_ep_label(ep)}: {e}")
        await asyncio.sleep(0.4)

# ---------------------------------------------------------------------------
# /cancel
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

    au_conv = ConversationHandler(
        entry_points=[CommandHandler("anime", cmd_anime)],
        states={
            AU_RESULTS:  [CallbackQueryHandler(au_pick_result,  pattern=r"^au_r:")],
            AU_EPISODES: [CallbackQueryHandler(au_pick_episode, pattern=r"^au_e:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

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
    app.add_error_handler(error_handler)

    log.info("Bot avviato in polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
