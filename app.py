#!/usr/bin/env python3
"""
WebSocket IRC Server
完全なIRCプロトコル実装 (RFC 1459 ベース)
依存: pip install websockets
使用: python irc_server.py [--host 0.0.0.0] [--port 6667]
"""

import asyncio
import websockets
import json
import logging
import argparse
import time
import re
from datetime import datetime
from typing import Dict, Set, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("irc-server")

SERVER_NAME = "SHINYA-IRC"
SERVER_VERSION = "1.0"
MOTD = [
    f"*** Welcome to {SERVER_NAME} v{SERVER_VERSION} ***",
    "*** WebSocket IRC Server (RFC 1459) ***",
    "*** Have fun and be nice! ***",
]

# IRC Numeric Replies
RPL_WELCOME       = "001"
RPL_YOURHOST      = "002"
RPL_CREATED       = "003"
RPL_MYINFO        = "004"
RPL_ISUPPORT      = "005"
RPL_MOTDSTART     = "375"
RPL_MOTD          = "372"
RPL_ENDOFMOTD     = "376"
RPL_NOTOPIC       = "331"
RPL_TOPIC         = "332"
RPL_NAMREPLY      = "353"
RPL_ENDOFNAMES    = "366"
RPL_LIST          = "322"
RPL_LISTEND       = "323"
RPL_WHOREPLY      = "352"
RPL_ENDOFWHO      = "315"
RPL_WHOISUSER     = "311"
RPL_WHOISSERVER   = "312"
RPL_WHOISCHANNELS = "319"
RPL_ENDOFWHOIS    = "318"
RPL_CHANNELMODEIS = "324"
RPL_INVITING      = "341"
RPL_BANLIST       = "367"
RPL_ENDOFBANLIST  = "368"
RPL_AWAY          = "301"
RPL_UNAWAY        = "305"
RPL_NOWAWAY       = "306"
RPL_INFO          = "371"
RPL_ENDOFINFO     = "374"
RPL_VERSION       = "351"
RPL_LUSERCLIENT   = "251"
RPL_LUSEROP       = "252"
RPL_LUSERUNKNOWN  = "253"
RPL_LUSERCHANNELS = "254"
RPL_LUSERME       = "255"
RPL_TIME          = "391"
RPL_ISON          = "303"
RPL_USERHOST      = "302"

ERR_NOSUCHNICK     = "401"
ERR_NOSUCHSERVER   = "402"
ERR_NOSUCHCHANNEL  = "403"
ERR_CANNOTSENDTOCHAN = "404"
ERR_TOOMANYCHANNELS = "405"
ERR_UNKNOWNCOMMAND = "421"
ERR_NOMOTD         = "422"
ERR_NONICKNAMEGIVEN = "431"
ERR_ERRONEUSNICKNAME = "432"
ERR_NICKNAMEINUSE  = "433"
ERR_USERNOTINCHANNEL = "441"
ERR_NOTONCHANNEL   = "442"
ERR_USERONCHANNEL  = "443"
ERR_NEEDMOREPARAMS = "461"
ERR_ALREADYREGISTRED = "462"
ERR_CHANNELISFULL  = "471"
ERR_INVITEONLYCHAN = "473"
ERR_BANNEDFROMCHAN = "474"
ERR_BADCHANNELKEY  = "475"
ERR_CHANOPRIVSNEEDED = "482"
ERR_UMODEUNKNOWNFLAG = "501"
ERR_USERSDONTMATCH = "502"
ERR_NOPRIVILEGES   = "481"


def valid_nick(nick: str) -> bool:
    return bool(re.match(r'^[a-zA-Z\[\\\]^_`{|}][a-zA-Z0-9\[\\\]^_`{|}\-]{0,29}$', nick))

def valid_channel(name: str) -> bool:
    return bool(re.match(r'^[#&!+][^\x00\x07\x0a\x0d ,]{1,49}$', name))


class Channel:
    def __init__(self, name: str):
        self.name = name
        self.topic = ""
        self.topic_setter = ""
        self.topic_time = 0
        self.members: Dict[str, "Client"] = {}   # nick -> client
        self.ops: Set[str] = set()               # nicks with op
        self.voices: Set[str] = set()            # nicks with voice
        self.modes: Set[str] = set()             # channel modes
        self.key: Optional[str] = None
        self.limit: Optional[int] = None
        self.invites: Set[str] = set()
        self.bans: list = []
        self.created = int(time.time())

    def add_member(self, client: "Client"):
        self.members[client.nick] = client

    def remove_member(self, nick: str):
        self.members.pop(nick, None)
        self.ops.discard(nick)
        self.voices.discard(nick)

    def rename_member(self, old_nick: str, new_nick: str, client: "Client"):
        if old_nick in self.members:
            del self.members[old_nick]
            self.members[new_nick] = client
        if old_nick in self.ops:
            self.ops.discard(old_nick)
            self.ops.add(new_nick)
        if old_nick in self.voices:
            self.voices.discard(old_nick)
            self.voices.add(new_nick)

    def prefix_for(self, nick: str) -> str:
        if nick in self.ops:
            return "@"
        if nick in self.voices:
            return "+"
        return ""

    def mode_string(self) -> str:
        m = "+" + "".join(sorted(self.modes))
        if self.key:
            m += " " + self.key
        if self.limit is not None:
            m += f" {self.limit}"
        return m

    def is_banned(self, mask: str) -> bool:
        for ban in self.bans:
            if re.fullmatch(ban.replace("*", ".*").replace("?", "."), mask, re.IGNORECASE):
                return True
        return False


class Client:
    def __init__(self, ws, server: "IRCServer"):
        self.ws = ws
        self.server = server
        self.nick = "*"
        self.user = ""
        self.realname = ""
        self.host = ws.remote_address[0] if ws.remote_address else "unknown"
        self.registered = False
        self.away_msg: Optional[str] = None
        self.channels: Set[str] = set()
        self.modes: Set[str] = set()
        self.signon = int(time.time())
        self.last_activity = time.time()
        self._nick_set = False
        self._user_set = False

    @property
    def mask(self) -> str:
        return f"{self.nick}!{self.user}@{self.host}"

    async def send(self, line: str):
        try:
            await self.ws.send(line)
            log.debug(f"→ {self.nick}: {line}")
        except Exception:
            pass

    async def reply(self, code: str, *params):
        target = self.nick if self.nick != "*" else "*"
        parts = list(params)
        if parts:
            parts[-1] = ":" + parts[-1]
        await self.send(f":{self.server.name} {code} {target} {' '.join(parts)}")

    async def send_from(self, origin: str, cmd: str, *params):
        parts = list(params)
        if parts:
            parts[-1] = ":" + parts[-1]
        await self.send(f":{origin} {cmd} {' '.join(parts)}")


class IRCServer:
    def __init__(self, host="0.0.0.0", port=6667):
        self.host = host
        self.port = port
        self.name = SERVER_NAME
        self.clients: Dict[str, Client] = {}   # nick -> client (registered)
        self.channels: Dict[str, Channel] = {} # name -> channel
        self.started = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        self._all_clients: Set[Client] = set() # all connections

    # ─── Connection lifecycle ────────────────────────────────────────────────

    async def handle(self, ws):
        client = Client(ws, self)
        self._all_clients.add(client)
        log.info(f"New connection from {client.host}")
        try:
            async for raw in ws:
                client.last_activity = time.time()
                # Support JSON-wrapped messages from browser clients
                if raw.strip().startswith("{"):
                    try:
                        data = json.loads(raw)
                        line = data.get("line", "")
                    except json.JSONDecodeError:
                        line = raw
                else:
                    line = raw
                for msg_line in line.split("\n"):
                    msg_line = msg_line.strip("\r\n")
                    if msg_line:
                        log.debug(f"← {client.nick}: {msg_line}")
                        await self.dispatch(client, msg_line)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self.cleanup(client)
            self._all_clients.discard(client)
            log.info(f"Disconnected: {client.nick} ({client.host})")

    async def cleanup(self, client: Client, quit_msg="Connection closed"):
        if client.registered:
            await self.broadcast_quit(client, quit_msg)
        self.clients.pop(client.nick, None)
        for ch_name in list(client.channels):
            ch = self.channels.get(ch_name)
            if ch:
                ch.remove_member(client.nick)
                if not ch.members:
                    del self.channels[ch_name]

    # ─── Message dispatcher ──────────────────────────────────────────────────

    async def dispatch(self, client: Client, line: str):
        if line.startswith(":"):
            parts = line[1:].split(" ", 1)
            line = parts[1] if len(parts) > 1 else ""
        
        if " :" in line:
            head, trailing = line.split(" :", 1)
            tokens = head.split() + [trailing]
        else:
            tokens = line.split()
        
        if not tokens:
            return
        
        cmd = tokens[0].upper()
        params = tokens[1:]
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler:
            await handler(client, params)
        else:
            if client.registered:
                await client.reply(ERR_UNKNOWNCOMMAND, cmd, f"Unknown command")

    # ─── IRC Commands ────────────────────────────────────────────────────────

    async def cmd_CAP(self, client: Client, params):
        # Basic CAP negotiation - acknowledge but don't enable extended caps
        sub = params[0].upper() if params else ""
        if sub == "LS":
            await client.send(f":{self.name} CAP * LS :")
        elif sub == "END":
            pass

    async def cmd_NICK(self, client: Client, params):
        if not params:
            await client.reply(ERR_NONICKNAMEGIVEN, "No nickname given")
            return
        nick = params[0]
        if not valid_nick(nick):
            await client.reply(ERR_ERRONEUSNICKNAME, nick, "Erroneous nickname")
            return
        if nick.lower() in {n.lower() for n in self.clients} and nick != client.nick:
            await client.reply(ERR_NICKNAMEINUSE, nick, "Nickname is already in use")
            return
        
        old_nick = client.nick
        old_mask = client.mask

        if client.registered:
            # Notify all channels
            self.clients.pop(old_nick, None)
            self.clients[nick] = client
            client.nick = nick
            affected = {client}
            for ch_name in client.channels:
                ch = self.channels.get(ch_name)
                if ch:
                    ch.rename_member(old_nick, nick, client)
                    affected.update(ch.members.values())
            for c in affected:
                await c.send(f":{old_mask} NICK :{nick}")
        else:
            client.nick = nick
            client._nick_set = True
            await self.try_register(client)

    async def cmd_USER(self, client: Client, params):
        if client.registered:
            await client.reply(ERR_ALREADYREGISTRED, "You may not reregister")
            return
        if len(params) < 4:
            await client.reply(ERR_NEEDMOREPARAMS, "USER", "Not enough parameters")
            return
        client.user = params[0][:10]
        client.realname = params[3]
        client._user_set = True
        await self.try_register(client)

    async def try_register(self, client: Client):
        if not (client._nick_set and client._user_set):
            return
        client.registered = True
        self.clients[client.nick] = client
        await client.reply(RPL_WELCOME, f"Welcome to {self.name} {client.mask}")
        await client.reply(RPL_YOURHOST, f"Your host is {self.name}, running version {SERVER_VERSION}")
        await client.reply(RPL_CREATED, f"This server was created {self.started}")
        await client.reply(RPL_MYINFO, self.name, SERVER_VERSION, "o", "imnopqrstv")
        await self.send_motd(client)
        await self.send_lusers(client)

    async def send_motd(self, client: Client):
        await client.reply(RPL_MOTDSTART, f"- {self.name} Message of the Day -")
        for line in MOTD:
            await client.reply(RPL_MOTD, f"- {line}")
        await client.reply(RPL_ENDOFMOTD, "End of /MOTD command")

    async def send_lusers(self, client: Client):
        reg = len(self.clients)
        unk = len(self._all_clients) - reg
        ops = sum(1 for c in self.clients.values() if "o" in c.modes)
        await client.reply(RPL_LUSERCLIENT, f"There are {reg} users and 0 invisible on 1 servers")
        if ops:
            await client.reply(RPL_LUSEROP, str(ops), "IRC Operators online")
        if unk:
            await client.reply(RPL_LUSERUNKNOWN, str(unk), "unknown connection(s)")
        await client.reply(RPL_LUSERCHANNELS, str(len(self.channels)), "channels formed")
        await client.reply(RPL_LUSERME, f"I have {reg} clients and 1 servers")

    async def cmd_PING(self, client: Client, params):
        token = params[0] if params else self.name
        await client.send(f":{self.name} PONG {self.name} :{token}")

    async def cmd_PONG(self, client: Client, params):
        pass  # keep-alive acknowledged

    async def cmd_QUIT(self, client: Client, params):
        msg = params[0] if params else "Quit"
        await self.cleanup(client, msg)
        try:
            await client.ws.close()
        except Exception:
            pass

    async def cmd_JOIN(self, client: Client, params):
        if not client.registered:
            return
        if not params:
            await client.reply(ERR_NEEDMOREPARAMS, "JOIN", "Not enough parameters")
            return
        
        channels = params[0].split(",")
        keys = params[1].split(",") if len(params) > 1 else []
        
        for i, ch_name in enumerate(channels):
            ch_name = ch_name.strip()
            if not ch_name:
                continue
            if not valid_channel(ch_name):
                await client.reply(ERR_NOSUCHCHANNEL, ch_name, "No such channel")
                continue
            key = keys[i] if i < len(keys) else None
            
            ch = self.channels.get(ch_name.lower())
            if ch is None:
                ch = Channel(ch_name)
                self.channels[ch_name.lower()] = ch
            
            # Mode checks
            if ch_name.lower() in client.channels:
                continue
            if "l" in ch.modes and ch.limit and len(ch.members) >= ch.limit:
                await client.reply(ERR_CHANNELISFULL, ch_name, "Cannot join channel (+l)")
                continue
            if "k" in ch.modes and ch.key and key != ch.key:
                await client.reply(ERR_BADCHANNELKEY, ch_name, "Cannot join channel (+k)")
                continue
            if "i" in ch.modes and client.nick not in ch.invites:
                await client.reply(ERR_INVITEONLYCHAN, ch_name, "Cannot join channel (+i)")
                continue
            mask = f"{client.nick}!{client.user}@{client.host}"
            if ch.is_banned(mask):
                await client.reply(ERR_BANNEDFROMCHAN, ch_name, "Cannot join channel (+b)")
                continue
            
            first = len(ch.members) == 0
            ch.add_member(client)
            client.channels.add(ch_name.lower())
            if first:
                ch.ops.add(client.nick)
            
            # Broadcast JOIN to channel members
            for member in ch.members.values():
                await member.send(f":{client.mask} JOIN :{ch_name}")
            
            # Send topic
            if ch.topic:
                await client.reply(RPL_TOPIC, ch_name, ch.topic)
            else:
                await client.reply(RPL_NOTOPIC, ch_name, "No topic is set")
            
            # Send names
            await self.send_names(client, ch)

    async def send_names(self, client: Client, ch: Channel):
        names = " ".join(ch.prefix_for(n) + n for n in ch.members)
        await client.reply(RPL_NAMREPLY, "=", ch.name, names)
        await client.reply(RPL_ENDOFNAMES, ch.name, "End of /NAMES list")

    async def cmd_PART(self, client: Client, params):
        if not params:
            await client.reply(ERR_NEEDMOREPARAMS, "PART", "Not enough parameters")
            return
        channels = params[0].split(",")
        msg = params[1] if len(params) > 1 else client.nick
        for ch_name in channels:
            ch = self.channels.get(ch_name.lower())
            if not ch:
                await client.reply(ERR_NOSUCHCHANNEL, ch_name, "No such channel")
                continue
            if ch_name.lower() not in client.channels:
                await client.reply(ERR_NOTONCHANNEL, ch_name, "You're not on that channel")
                continue
            for member in ch.members.values():
                await member.send(f":{client.mask} PART {ch_name} :{msg}")
            ch.remove_member(client.nick)
            client.channels.discard(ch_name.lower())
            if not ch.members:
                del self.channels[ch_name.lower()]

    async def cmd_PRIVMSG(self, client: Client, params):
        await self._send_message(client, params, "PRIVMSG")

    async def cmd_NOTICE(self, client: Client, params):
        await self._send_message(client, params, "NOTICE")

    async def _send_message(self, client: Client, params, cmd: str):
        if not client.registered:
            return
        if len(params) < 2:
            await client.reply(ERR_NEEDMOREPARAMS, cmd, "Not enough parameters")
            return
        target = params[0]
        text = params[1]
        
        if target.startswith(("#", "&", "!", "+")):
            ch = self.channels.get(target.lower())
            if not ch:
                await client.reply(ERR_NOSUCHCHANNEL, target, "No such channel")
                return
            if "n" in ch.modes and target.lower() not in client.channels:
                await client.reply(ERR_CANNOTSENDTOCHAN, target, "Cannot send to channel")
                return
            if "m" in ch.modes and client.nick not in ch.ops and client.nick not in ch.voices:
                await client.reply(ERR_CANNOTSENDTOCHAN, target, "Cannot send to channel (+m)")
                return
            for member in ch.members.values():
                if member != client:
                    await member.send(f":{client.mask} {cmd} {target} :{text}")
        else:
            dest = self.clients.get(target)
            if not dest:
                await client.reply(ERR_NOSUCHNICK, target, "No such nick/channel")
                return
            if dest.away_msg and cmd == "PRIVMSG":
                await client.reply(RPL_AWAY, target, dest.away_msg)
            await dest.send(f":{client.mask} {cmd} {target} :{text}")

    async def cmd_TOPIC(self, client: Client, params):
        if not params:
            await client.reply(ERR_NEEDMOREPARAMS, "TOPIC", "Not enough parameters")
            return
        ch_name = params[0]
        ch = self.channels.get(ch_name.lower())
        if not ch:
            await client.reply(ERR_NOSUCHCHANNEL, ch_name, "No such channel")
            return
        if len(params) == 1:
            if ch.topic:
                await client.reply(RPL_TOPIC, ch_name, ch.topic)
            else:
                await client.reply(RPL_NOTOPIC, ch_name, "No topic is set")
            return
        if "t" in ch.modes and client.nick not in ch.ops:
            await client.reply(ERR_CHANOPRIVSNEEDED, ch_name, "You're not channel operator")
            return
        ch.topic = params[1]
        ch.topic_setter = client.nick
        ch.topic_time = int(time.time())
        for member in ch.members.values():
            await member.send(f":{client.mask} TOPIC {ch_name} :{ch.topic}")

    async def cmd_NAMES(self, client: Client, params):
        if not params:
            for ch in self.channels.values():
                await self.send_names(client, ch)
        else:
            for ch_name in params[0].split(","):
                ch = self.channels.get(ch_name.lower())
                if ch:
                    await self.send_names(client, ch)

    async def cmd_LIST(self, client: Client, params):
        for ch in self.channels.values():
            await client.reply(RPL_LIST, ch.name, str(len(ch.members)), ch.topic or "")
        await client.reply(RPL_LISTEND, "End of /LIST")

    async def cmd_INVITE(self, client: Client, params):
        if len(params) < 2:
            await client.reply(ERR_NEEDMOREPARAMS, "INVITE", "Not enough parameters")
            return
        nick, ch_name = params[0], params[1]
        ch = self.channels.get(ch_name.lower())
        if ch and ch_name.lower() not in client.channels:
            await client.reply(ERR_NOTONCHANNEL, ch_name, "You're not on that channel")
            return
        if ch and "i" in ch.modes and client.nick not in ch.ops:
            await client.reply(ERR_CHANOPRIVSNEEDED, ch_name, "You're not channel operator")
            return
        target = self.clients.get(nick)
        if not target:
            await client.reply(ERR_NOSUCHNICK, nick, "No such nick/channel")
            return
        if ch:
            ch.invites.add(nick)
        await client.reply(RPL_INVITING, nick, ch_name)
        await target.send(f":{client.mask} INVITE {nick} :{ch_name}")

    async def cmd_KICK(self, client: Client, params):
        if len(params) < 2:
            await client.reply(ERR_NEEDMOREPARAMS, "KICK", "Not enough parameters")
            return
        ch_name, nick = params[0], params[1]
        reason = params[2] if len(params) > 2 else client.nick
        ch = self.channels.get(ch_name.lower())
        if not ch:
            await client.reply(ERR_NOSUCHCHANNEL, ch_name, "No such channel")
            return
        if ch_name.lower() not in client.channels:
            await client.reply(ERR_NOTONCHANNEL, ch_name, "You're not on that channel")
            return
        if client.nick not in ch.ops:
            await client.reply(ERR_CHANOPRIVSNEEDED, ch_name, "You're not channel operator")
            return
        target = self.clients.get(nick)
        if not target or ch_name.lower() not in target.channels:
            await client.reply(ERR_USERNOTINCHANNEL, nick, ch_name, "They aren't on that channel")
            return
        for member in ch.members.values():
            await member.send(f":{client.mask} KICK {ch_name} {nick} :{reason}")
        ch.remove_member(nick)
        target.channels.discard(ch_name.lower())

    async def cmd_MODE(self, client: Client, params):
        if not params:
            await client.reply(ERR_NEEDMOREPARAMS, "MODE", "Not enough parameters")
            return
        target = params[0]
        
        if target.startswith(("#", "&", "!", "+")):
            await self.handle_channel_mode(client, target, params[1:])
        else:
            await self.handle_user_mode(client, target, params[1:])

    async def handle_user_mode(self, client: Client, target: str, mode_params):
        if target != client.nick and "o" not in client.modes:
            await client.reply(ERR_USERSDONTMATCH, "Cannot change mode for other users")
            return
        t = self.clients.get(target)
        if not t:
            await client.reply(ERR_NOSUCHNICK, target, "No such nick")
            return
        if not mode_params:
            await client.send(f":{self.name} 221 {client.nick} +{''.join(sorted(t.modes))}")
            return
        mode_str = mode_params[0]
        adding = True
        for ch in mode_str:
            if ch == "+":
                adding = True
            elif ch == "-":
                adding = False
            elif ch in "io":
                if adding:
                    t.modes.add(ch)
                else:
                    t.modes.discard(ch)
            else:
                await client.reply(ERR_UMODEUNKNOWNFLAG, "Unknown MODE flag")
        await client.send(f":{client.mask} MODE {target} {mode_str}")

    async def handle_channel_mode(self, client: Client, ch_name: str, mode_params):
        ch = self.channels.get(ch_name.lower())
        if not ch:
            await client.reply(ERR_NOSUCHCHANNEL, ch_name, "No such channel")
            return
        if not mode_params:
            await client.reply(RPL_CHANNELMODEIS, ch_name, ch.mode_string())
            return
        if client.nick not in ch.ops and "o" not in client.modes:
            await client.reply(ERR_CHANOPRIVSNEEDED, ch_name, "You're not channel operator")
            return
        
        mode_str = mode_params[0]
        mp = list(mode_params[1:])
        adding = True
        applied = []
        for c in mode_str:
            if c == "+":
                adding = True; applied.append("+")
            elif c == "-":
                adding = False; applied.append("-")
            elif c in "mntisp":
                if adding:
                    ch.modes.add(c)
                else:
                    ch.modes.discard(c)
                applied.append(c)
            elif c == "k":
                if adding and mp:
                    ch.modes.add("k"); ch.key = mp.pop(0); applied.append("k")
                elif not adding:
                    ch.modes.discard("k"); ch.key = None; applied.append("k")
            elif c == "l":
                if adding and mp:
                    try:
                        ch.limit = int(mp.pop(0)); ch.modes.add("l"); applied.append("l")
                    except ValueError:
                        pass
                elif not adding:
                    ch.modes.discard("l"); ch.limit = None; applied.append("l")
            elif c == "o":
                if mp:
                    nick = mp.pop(0)
                    if nick in ch.members:
                        if adding:
                            ch.ops.add(nick)
                        else:
                            ch.ops.discard(nick)
                        applied.append("o")
                        for m in ch.members.values():
                            await m.send(f":{client.mask} MODE {ch_name} {'+'if adding else'-'}o {nick}")
                        continue
            elif c == "v":
                if mp:
                    nick = mp.pop(0)
                    if nick in ch.members:
                        if adding:
                            ch.voices.add(nick)
                        else:
                            ch.voices.discard(nick)
                        applied.append("v")
                        for m in ch.members.values():
                            await m.send(f":{client.mask} MODE {ch_name} {'+'if adding else'-'}v {nick}")
                        continue
            elif c == "b":
                if mp:
                    ban = mp.pop(0)
                    if adding:
                        if ban not in ch.bans:
                            ch.bans.append(ban)
                    else:
                        ch.bans = [b for b in ch.bans if b != ban]
                    for m in ch.members.values():
                        await m.send(f":{client.mask} MODE {ch_name} {'+'if adding else'-'}b {ban}")
                    continue
                else:
                    for ban in ch.bans:
                        await client.reply(RPL_BANLIST, ch_name, ban)
                    await client.reply(RPL_ENDOFBANLIST, ch_name, "End of channel ban list")
                    continue
        
        mode_applied = "".join(applied)
        if mode_applied.strip("+-"):
            for m in ch.members.values():
                await m.send(f":{client.mask} MODE {ch_name} {mode_applied}")

    async def cmd_WHO(self, client: Client, params):
        if not params:
            return
        mask = params[0]
        ch = self.channels.get(mask.lower())
        if ch:
            for nick, member in ch.members.items():
                flags = "H" if not member.away_msg else "G"
                if nick in ch.ops:
                    flags += "@"
                await client.reply(RPL_WHOREPLY, ch.name, member.user, member.host,
                                   self.name, nick, flags, f"0 {member.realname}")
            await client.reply(RPL_ENDOFWHO, mask, "End of /WHO list")
        else:
            for nick, member in self.clients.items():
                if re.fullmatch(mask.replace("*", ".*").replace("?", "."), nick, re.I):
                    flags = "H" if not member.away_msg else "G"
                    await client.reply(RPL_WHOREPLY, "*", member.user, member.host,
                                       self.name, nick, flags, f"0 {member.realname}")
            await client.reply(RPL_ENDOFWHO, mask, "End of /WHO list")

    async def cmd_WHOIS(self, client: Client, params):
        if not params:
            await client.reply(ERR_NEEDMOREPARAMS, "WHOIS", "Not enough parameters")
            return
        nick = params[0]
        target = self.clients.get(nick)
        if not target:
            await client.reply(ERR_NOSUCHNICK, nick, "No such nick/channel")
            return
        await client.reply(RPL_WHOISUSER, nick, target.user, target.host, "*", target.realname)
        await client.reply(RPL_WHOISSERVER, nick, self.name, "WebSocket IRCd")
        chans = " ".join(
            (ch.prefix_for(nick) + ch.name)
            for ch in self.channels.values()
            if nick in ch.members
        )
        if chans:
            await client.reply(RPL_WHOISCHANNELS, nick, chans)
        await client.reply(RPL_ENDOFWHOIS, nick, "End of /WHOIS list")

    async def cmd_AWAY(self, client: Client, params):
        if params:
            client.away_msg = params[0]
            await client.reply(RPL_NOWAWAY, "You have been marked as being away")
        else:
            client.away_msg = None
            await client.reply(RPL_UNAWAY, "You are no longer marked as being away")

    async def cmd_ISON(self, client: Client, params):
        online = [n for n in params if n in self.clients]
        await client.reply(RPL_ISON, " ".join(online))

    async def cmd_USERHOST(self, client: Client, params):
        results = []
        for nick in params[:5]:
            c = self.clients.get(nick)
            if c:
                away = "-" if c.away_msg else "+"
                results.append(f"{nick}={away}{c.user}@{c.host}")
        await client.reply(RPL_USERHOST, " ".join(results))

    async def cmd_VERSION(self, client: Client, params):
        await client.reply(RPL_VERSION, f"{SERVER_VERSION}.{self.name}", self.name, "")

    async def cmd_TIME(self, client: Client, params):
        await client.reply(RPL_TIME, self.name, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

    async def cmd_INFO(self, client: Client, params):
        await client.reply(RPL_INFO, f"{SERVER_NAME} v{SERVER_VERSION}")
        await client.reply(RPL_INFO, "WebSocket-based IRC server written in Python")
        await client.reply(RPL_INFO, f"Running since {self.started}")
        await client.reply(RPL_ENDOFINFO, "End of /INFO list")

    async def cmd_LUSERS(self, client: Client, params):
        await self.send_lusers(client)

    async def cmd_MOTD(self, client: Client, params):
        await self.send_motd(client)

    async def cmd_OPER(self, client: Client, params):
        # Simplified: no oper passwords in this implementation
        await client.reply(ERR_NOPRIVILEGES, "Permission Denied- You're not an IRC operator")

    # ─── Broadcast helpers ───────────────────────────────────────────────────

    async def broadcast_quit(self, client: Client, msg: str):
        notified = set()
        for ch_name in list(client.channels):
            ch = self.channels.get(ch_name)
            if ch:
                for member in ch.members.values():
                    if member != client and member not in notified:
                        await member.send(f":{client.mask} QUIT :{msg}")
                        notified.add(member)
                ch.remove_member(client.nick)
                client.channels.discard(ch_name)
                if not ch.members:
                    del self.channels[ch_name]

    # ─── Server entry point ──────────────────────────────────────────────────

    async def start(self):
        async with websockets.serve(
            self.handle,
            self.host,
            self.port,
            ping_interval=30,
            ping_timeout=10,
        ):
            log.info(f"✓ IRC Server listening on ws://{self.host}:{self.port}")
            log.info(f"  Server name : {self.name} v{SERVER_VERSION}")
            log.info(f"  Started     : {self.started}")
            await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description="WebSocket IRC Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=6667, help="Bind port (default: 6667)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    server = IRCServer(args.host, args.port)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        log.info("Server shut down.")


if __name__ == "__main__":
    main()
