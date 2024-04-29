#!/usr/bin/env python3
import asyncio
import os
from typing import List, Tuple, Literal, Dict, Any, Optional

import aiofiles
import aiosessions
from pyrogram import generate_filter, Client, filters, raw
from pyrogram.errors import FloodWait
from pyrogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

# Create a new Pyrogram client instance with the name ":memory:" and 1 worker.
app = Client(":memory:", workers=1)

# Add handlers for various commands.
# Each handler is a coroutine function that takes a Client and Message as arguments.
app.add_handler(filters.command(["start"]), start_command)
app.add_handler(filters.command(["help"]), help_command)
app.add_handler(filters.command(["start"]), start_command)
app.add_handler(filters.command(["speedtest"]), speedtest_command)
app.add_handler(filters.command(["uptime"]), uptime_command)
app.add_handler(filters.command(["restart"]), restart_command)
app.add_handler(filters.command(["shutdown"]), shutdown_command)
app.add_handler(filters.command(["stats"]), stats_command)
app.add_handler(filters.command(["sysinfo"]), sysinfo_command)
app.add_handler(filters.command(["ping"]), ping_command)
app.add_handler(filters.command(["btselect"]), select)  # Changed command name from "broadcast" to "btselect"
app.add_handler(CallbackQueryHandler(get_confirm, filters=filters.regex("^btsel")))

async def select(client: Client, message: Message):
    """
    Handles the /btselect command.
    This function is called when the user sends the /btselect command.
    It extracts the gid (unique identifier for a torrent) from the user's message,
    and then calls the get_download_by_gid function to get the download object.
    If the download object is not found, it sends an error message.
    Otherwise, it checks if the user is the owner of the download or has sudo privileges.
    If not, it sends an error message.
    If the user is the owner or has sudo privileges, it checks if the download is in a valid state.
    If not, it sends an error message.
    If the download is in a valid state, it pauses the download and sends a message with a keyboard
    to allow the user to select files.
    """
    user_id = message.from_user.id
    cmd_data = message.text.split('_', maxsplit=1)
    gid = None

    if len(cmd_data) > 1:
        gid = cmd_data[1].split('@', maxsplit=1)[0].strip()
    elif message.reply_to_message:
        reply_message = message.reply_to_message
        async with download_dict_lock:
            download_info = download_dict.get(reply_message.message_id, None)
            if download_info:
                gid = download_info.get('gid')
    else:
        await client.send_message(message.chat.id, "Invalid usage. Reply to a task or use /btselect <gid>")
        return

    if not gid:
        await client.send_message(message.chat.id, "Task not found.")
        return

    try:
        dl = await get_download_by_gid(gid)
    except Exception as e:
        await client.send_message(message.chat.id, f"Error: {e}")
        return

    if not dl:
        await client.send_message(message.chat.id, "Task not found.")
        return

    if user_id not in (dl.message.from_user.id, OWNER_ID) and (user_id not in user_data or not user_data[user_id].get('is_sudo')):
        await client.send_message(message.chat.id, "This task is not for you!")
        return

    if dl.status not in (MirrorStatus.STATUS_DOWNLOADING, MirrorStatus.STATUS_PAUSED, MirrorStatus.STATUS_QUEUED):
        await client.send_message(message.chat.id, 'Task should be in download state or pause (incase message deleted by wrong) or queued (status incase you used torrent file)!')
        return

    if dl.name.startswith('[METADATA]'):
        await client.send_message(message.chat.id, 'Try after downloading metadata finished!')
        return

    try:
        if dl.is_qbit:
            id_ = dl.hash
            client_ = dl.client
            if not dl.queued:
                await sync_to_async(client_.torrents_pause, torrent_hashes=[id_])
        else:
            id_ = dl.gid
            if not dl.queued:
                await sync_to_async(aria2.client.force_pause, id_)
        dl.listener.select = True
    except Exception as e:
        await client.send_message(message.chat.id, "This is not a bittorrent task!")
        return

    buttons = bt_selection_buttons(id_)
    msg = "Your download paused. Choose files then press Done Selecting button to resume downloading."
    await client.send_message(message.chat.id, msg, reply_markup=InlineKeyboardMarkup(buttons))


async def get_confirm(client: Client, query: CallbackQuery):
    """
    Handles the callback query for the /btselect command.
    This function is called when the user presses a button in the keyboard sent by the select function.
    It extracts the gid and the button type (pin, done, or rm) from the query data.
    If the button type is pin, it sends a message with the selected files.
    If the button type is done, it resumes the download and sends a message with the selected files.
    If the button type is rm, it cancels the download and sends a message with the selected files.
    """
    user_id = query.from_user.id
    data = query.data.split()
    message = query.message
    dl = await get_download_by_gid(data[2])

    if not dl:
        await query.answer("This task has been cancelled!", show_alert=True)
        await client.edit_message_text("", message.chat.id, message.message_id)
        return

    if hasattr(dl, 'listener'):
        listener = dl.listener
    else:
        await query.answer("Not in download state anymore! Keep this message to resume the seed if seed enabled!", show_alert=True)
        return

    if user_id != listener.message.from_user.id and not await CustomFilters.sudo(client, query):
        await query.answer("This task is not for you!", show_alert=True)
        return

    if data[1] == "pin":
        await query.answer(data[3], show_alert=True)
    elif data[1] == "done":
        await query.answer()
        id_ = data[3]
        if len(id_) > 20:
            client_ = dl.client
            tor_info = (await sync_to_async(client_.torrents_info, torrent_hash=[id_]))[0]
            path = tor_info.content_path.rsplit('/', 1)[0]
            async with aiofiles.open(f"{path}/.selected_files", "w") as f:
                f.write("")
            res = await sync_to_async(client_.torrents_files, torrent_hash=[id_])
            for f in res:
                if f.priority == 0:
                    f_paths = [os.path.join(path, f.name), os.path.join(path, f.name + '.!qB')]
                    for f_path in f_paths:
                        if await aiosessions.aiofiles.os.path.exists(f_path):
                            try:
                                await aiosessions.aiofiles.os.remove(f_path)
                            except Exception:
                                pass
            if not dl.queued:
                await sync_to_async(client_.torrents_resume, torrent_hashes=[id_])
        else:
            res = await sync_to_async(aria2.client.get_files, id_)
            for f in res:
                if not f['selected'] and await aiosessions.aiofiles.os.path.exists(f['path']):
                    try:
                        await aiosessions.aiofiles.os.remove(f['path'])
                    except Exception:
                        pass
            if not dl.queued:
                try:
                    await sync_to_async(aria2.client.unpause, id_)
                except Exception as e:
                    LOGGER.error(f"{e} Error in resume, this mostly happens after abuse aria2. Try to use select cmd again!")
        await client.send_animation(message.chat.id, "mdi://action/content-save-all", caption="Download resumed.", reply_to_message_id=message.message_id)
        await client.edit_message_text("", message.chat.id, message.message_id)
    elif data[1] == "rm":
        await query.answer()
        try:
            await dl.download().cancel_download()
        except FloodWait as e:
            await asyncio.sleep(e.x)
        await client.edit_message_text("", message.chat.id, message.message_id)


if __name__ == "__main__":
    app.run()
