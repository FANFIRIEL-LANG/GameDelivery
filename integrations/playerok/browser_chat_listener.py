"""WebEngine-context GraphQL chat subscriptions and gap catch-up."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from integrations.playerok.browser_chat_api import (
    ChatAPIError, MessageDeduplicator, PlayerokBrowserChatAPI, PlayerokChat, PlayerokChatMessage,
    _canonical_id,
    normalize_chat, normalize_message, system_event_kind,
)
from integrations.playerok.universal_chat_contract import QUERIES

_MAX_BRIDGE_EVENTS = 1000
_MAX_BRIDGE_RESPONSE_BYTES = 2 * 1024 * 1024


def _bridge_start_script(key: str, user_id: str, chat_ids: list[str]) -> str:
    ws_queries = {name: QUERIES[name] for name in (
        "chatUpdated", "chatMessageCreated", "userUpdated",
    )}
    return """(()=>{
      const key=__KEY__, userId=__USER__, initialChats=__CHATS__, queries=__QUERIES__;
      try {
      try { if(window[key]) window[key].stop(); } catch(_) {}
      let stopped=false, ws=null, retryTimer=null, retryDelay=1000, connectedBefore=false;
      let generation=0, nextSub=0, queue=[], droppedEvents=0, chatIds=new Set(initialChats), subscriptions=new Map();
      const state={
        stop(){stopped=true;generation++;clearTimeout(retryTimer);subscriptions.clear();
          try{if(ws)ws.close(1000,'client stop');}catch(_){};ws=null;queue=[];},
        addChats(ids){for(const id of ids||[]) if(typeof id==='string'&&id) chatIds.add(id);
          if(ws&&ws.readyState===WebSocket.OPEN) subscribeChats();},
        drain(){const out=queue;queue=[];const dropped=droppedEvents;droppedEvents=0;return {events:out,dropped};}
      };
      window[key]=state;
      const push=(event)=>{if(queue.length>=__MAX__){queue.shift();droppedEvents++;}queue.push(event);};
      const id=()=>`gd-${Date.now().toString(36)}-${(++nextSub).toString(36)}-${Math.random().toString(36).slice(2,8)}`;
      const send=(operationName,query,variables,chatId)=>{
        if(!ws||ws.readyState!==WebSocket.OPEN)return;
        const sid=id(); if(chatId)subscriptions.set(sid,chatId);
        ws.send(JSON.stringify({id:sid,type:'subscribe',payload:{extensions:{},operationName,query,variables}}));
      };
      const subscribeChats=()=>{for(const chatId of chatIds){
        let active=false;for(const existing of subscriptions.values())if(existing===chatId){active=true;break;}
        if(!active)send('chatMessageCreated',queries.chatMessageCreated,{filter:{chatId}},chatId);
      }};
      const subscribeAll=()=>{
        subscriptions.clear();
        send('chatUpdated',queries.chatUpdated,{filter:{userId},showForbiddenImage:true});
        send('userUpdated',queries.userUpdated,{userId});
        subscribeChats();
      };
      const connect=()=>{
        if(stopped)return;
        const myGeneration=++generation;
        try{ws=new WebSocket('wss://ws.playerok.com/graphql','graphql-transport-ws');}
        catch(_){push({kind:'socket_error'});scheduleReconnect(myGeneration);return;}
        ws.onopen=()=>{if(stopped||myGeneration!==generation)return;retryDelay=1000;
          ws.send(JSON.stringify({type:'connection_init',payload:{'x-gql-op':'ws-subscription','x-gql-path':'/chats/[id]','x-timezone-offset':-180}}));};
        ws.onmessage=(event)=>{if(stopped||myGeneration!==generation)return;
          let frame;try{frame=JSON.parse(event.data);}catch(_){push({kind:'protocol_error'});return;}
          if(frame.type==='ping'){try{ws.send(JSON.stringify({type:'pong'}));}catch(_){};return;}
          if(frame.type==='connection_ack'){const reconnected=connectedBefore;connectedBefore=true;subscribeAll();push({kind:'connected',reconnected});return;}
          if(frame.type==='error'){subscriptions.delete(frame.id);push({kind:'subscription_error'});
            try{ws.close(1011,'subscription error');}catch(_){};return;}
          if(frame.type==='complete'){subscriptions.delete(frame.id);return;}
          if(frame.type==='next'&&frame.payload&&Array.isArray(frame.payload.errors)&&frame.payload.errors.length){
            push({kind:'subscription_error'});try{ws.close(1011,'subscription error');}catch(_){};return;}
          if(frame.type!=='next'||!frame.payload||!frame.payload.data)return;
          const data=frame.payload.data;
          if(data.chatUpdated){const chat=data.chatUpdated;
            if(typeof chat.id==='string'&&chat.id){chatIds.add(chat.id);
              let subscribed=false;for(const value of subscriptions.values())if(value===chat.id){subscribed=true;break;}
              if(!subscribed)send('chatMessageCreated',queries.chatMessageCreated,{filter:{chatId:chat.id}},chat.id);}
            push({kind:'chat_updated',chat});return;}
          if(data.chatMessageCreated){push({kind:'message',chatId:subscriptions.get(frame.id)||null,message:data.chatMessageCreated});return;}
          if(data.userUpdated){push({kind:'user_updated',user:data.userUpdated});}
        };
        ws.onerror=()=>{if(stopped||myGeneration!==generation)return;push({kind:'socket_error'});};
        ws.onclose=()=>{if(stopped||myGeneration!==generation)return;subscriptions.clear();
          push({kind:'disconnected'});scheduleReconnect(myGeneration);};
      };
      const scheduleReconnect=(myGeneration)=>{if(stopped||myGeneration!==generation)return;
        clearTimeout(retryTimer);retryTimer=setTimeout(()=>{if(!stopped)connect();},retryDelay);
        retryDelay=Math.min(30000,retryDelay*2);};
      connect();return true;
    }catch(_){return false;}
    })()""".replace("__KEY__", json.dumps(key)).replace(
        "__USER__", json.dumps(user_id)
    ).replace("__CHATS__", json.dumps(chat_ids)).replace(
        "__QUERIES__", json.dumps(ws_queries, ensure_ascii=False, separators=(",", ":"))
    ).replace("__MAX__", str(_MAX_BRIDGE_EVENTS))


def _drain_script(key: str) -> str:
    return (
        "(()=>{try{const state=window[" + json.dumps(key) + "];"
        "if(!state||typeof state.drain!=='function')return JSON.stringify({error:'bridge_missing'});"
        "return JSON.stringify(state.drain());}catch(_){return JSON.stringify({error:'bridge_error'});}})()"
    )


def _add_chats_script(key: str, ids: list[str]) -> str:
    return (
        "(()=>{try{const s=window[" + json.dumps(key) + "];if(!s)return false;"
        "s.addChats(" + json.dumps(ids) + ");return true;}catch(_){return false;}})()"
    )


def _stop_script(key: str) -> str:
    return (
        "(()=>{try{const s=window[" + json.dumps(key) + "];if(s){s.stop();delete window["
        + json.dumps(key) + "];}return true;}catch(_){return false;}})()"
    )


class PlayerokBrowserChatListener(QObject):
    """Explicit read-only callbacks from GraphQL subscriptions into service consumers."""

    chat_discovered = Signal(object)
    chat_updated = Signal(object)
    message_received = Signal(object)
    system_event = Signal(object)
    connection_state = Signal(str)
    subscription_event = Signal(str)

    def __init__(self, api: PlayerokBrowserChatAPI, run_javascript, parent=None, *, poll_interval_ms: int = 100):
        super().__init__(parent)
        self.api = api
        self._run_javascript = run_javascript
        self._active = False
        self._user_id: str | None = None
        self._bridge_key = "__gd_chat_ws_" + uuid4().hex
        self._chats: dict[str, PlayerokChat] = {}
        self._seen_messages = MessageDeduplicator()
        self._was_connected = False
        self._lost_at: float | None = None
        self._catchup_active = False
        self._catchup_unresolved = False
        self._diagnostic_only = False
        self._catchup_queue: list[tuple[str, PlayerokChat]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self._poll_bridge)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def account_user_id(self) -> str | None:
        return self._user_id

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._diagnostic_only = False
        self._lost_at = time.time()
        self.connection_state.emit("starting")
        self.api.get_viewer(self._viewer_ready)

    def start_for_read_only_diagnostic(self, user_id: str, chat_id: str) -> None:
        """Start read-only subscriptions for the chat currently open in the browser."""
        if self._active:
            return
        try:
            user_id = _canonical_id(user_id)
            chat_id = _canonical_id(chat_id)
        except ChatAPIError:
            self.connection_state.emit("diagnostic_invalid_identifier")
            return
        self._active = True
        self._diagnostic_only = True
        self._user_id = user_id
        self._was_connected = False
        self._lost_at = time.time()
        self.connection_state.emit("diagnostic_starting")
        try:
            self._run_javascript(
                _bridge_start_script(self._bridge_key, user_id, [chat_id]), self._bridge_started
            )
        except Exception:
            self.connection_state.emit("bridge_start_error")
            self.stop()

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self._timer.stop()
        self._catchup_active = False
        self._catchup_queue.clear()
        try:
            self._run_javascript(_stop_script(self._bridge_key), lambda _value: None)
        except Exception:
            pass
        self.api.transport.cancel_all() if hasattr(self.api.transport, "cancel_all") else None
        self.connection_state.emit("stopped")

    def _viewer_ready(self, user_id, error):
        if not self._active:
            return
        if error or not user_id:
            code = error if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error) else "api_error"
            self.connection_state.emit("viewer_error_" + code)
            self.stop()
            return
        self._user_id = user_id
        self.api.get_chats(user_id, self._initial_chats)

    def _initial_chats(self, chats, error):
        if not self._active:
            return
        if error or chats is None:
            code = error if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error) else "api_error"
            self.connection_state.emit("chat_list_error_" + code)
            self.stop()
            return
        for chat in chats:
            self._remember_chat(chat)
            if chat.last_message:
                self._seen_messages.claim(chat.last_message.id)
        ids = list(self._chats)
        try:
            self._run_javascript(
                _bridge_start_script(self._bridge_key, self._user_id, ids), self._bridge_started
            )
        except Exception:
            self.connection_state.emit("bridge_start_error")
            self.stop()

    def _bridge_started(self, value):
        if not self._active:
            return
        if value is not True:
            self.connection_state.emit("bridge_start_error")
            self.stop()
            return
        self._timer.start()
        self.connection_state.emit("connecting")

    def _poll_bridge(self):
        if not self._active:
            return
        try:
            self._run_javascript(_drain_script(self._bridge_key), self._bridge_events_received)
        except Exception:
            self.connection_state.emit("bridge_poll_error")

    def _bridge_events_received(self, value):
        if not self._active:
            return
        try:
            if type(value) is not str or len(value.encode("utf-8")) > _MAX_BRIDGE_RESPONSE_BYTES:
                raise ValueError
            payload = json.loads(value)
            events = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(events, list) or len(events) > _MAX_BRIDGE_EVENTS:
                raise ValueError
            dropped = payload.get("dropped", 0)
            if type(dropped) is not int or dropped < 0:
                raise ValueError
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            self.connection_state.emit("bridge_payload_error")
            return
        if dropped:
            self._lost_at = self._lost_at or time.time()
            self.connection_state.emit("bridge_queue_overflow")
            self._catch_up()
        for event in events:
            self._process_bridge_event(event)

    def _process_bridge_event(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get("kind")
        if kind == "connected":
            self.connection_state.emit("connected")
            if event.get("reconnected") is True or not self._was_connected:
                self._lost_at = self._lost_at or time.time()
                if event.get("reconnected") is True:
                    self.api.get_viewer(self._verify_reconnected_account)
                elif not self._diagnostic_only:
                    self._catch_up()
            self._was_connected = True
            return
        if kind == "disconnected":
            self._lost_at = self._lost_at or time.time()
            self.connection_state.emit("disconnected")
            return
        if kind in {"socket_error", "protocol_error", "subscription_error"}:
            self.connection_state.emit(kind)
            return
        if kind == "user_updated":
            if self._user_id and not self._diagnostic_only:
                self.api.get_chats(self._user_id, self._refresh_chats)
            return
        if kind == "chat_updated":
            try:
                chat = normalize_chat(event.get("chat"))
            except Exception:
                self.connection_state.emit("invalid_chat_event")
                return
            self.subscription_event.emit("chatUpdated")
            is_new = chat.id not in self._chats
            self._remember_chat(chat)
            self.chat_discovered.emit(chat) if is_new else self.chat_updated.emit(chat)
            if chat.last_message:
                self._emit_message(chat.last_message)
            return
        if kind == "message":
            chat_id = event.get("chatId")
            try:
                message = normalize_message(event.get("message"), chat_id=chat_id)
            except Exception:
                self.connection_state.emit("invalid_message_event")
                return
            self.subscription_event.emit("chatMessageCreated")
            self._emit_message(message)

    def _remember_chat(self, chat: PlayerokChat):
        self._chats[chat.id] = chat

    def _emit_message(self, message: PlayerokChatMessage):
        if not self._seen_messages.claim(message.id):
            return
        self.message_received.emit(message)
        event_kind = system_event_kind(message)
        if event_kind and message.deal_id and message.chat_id:
            self.api.get_deal(
                message.deal_id,
                lambda deal, error: self._system_deal_refreshed(
                    event_kind, message, deal, error
                ),
            )

    def _system_deal_refreshed(self, event_kind, message, deal, error):
        if not self._active:
            return
        if error or deal is None:
            self.connection_state.emit("system_deal_refresh_error")
            return
        if deal.id != message.deal_id or deal.chat_id != message.chat_id:
            self.connection_state.emit("system_deal_identity_mismatch")
            return
        self.system_event.emit({
            "kind": event_kind,
            "chat_id": message.chat_id,
            "deal_id": deal.id,
            "listing_id": deal.listing_id,
            "deal_status": deal.status,
            "message_id": message.id,
        })

    def _refresh_chats(self, chats, error):
        if not self._active:
            return
        if error or chats is None:
            self.connection_state.emit("chat_refresh_error")
            return
        new_ids = []
        for chat in chats:
            if chat.id not in self._chats:
                self.chat_discovered.emit(chat)
                new_ids.append(chat.id)
            else:
                self.chat_updated.emit(chat)
            self._remember_chat(chat)
        if new_ids:
            try:
                self._run_javascript(_add_chats_script(self._bridge_key, new_ids), lambda _value: None)
            except Exception:
                self.connection_state.emit("subscription_update_error")

    @staticmethod
    def _timestamp(value: str | None) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    def _catch_up(self):
        if not self._active or self._catchup_active or not self._user_id:
            return
        self._catchup_active = True
        self.api.get_chats(self._user_id, self._catchup_chats)

    def _verify_reconnected_account(self, user_id, error):
        if not self._active:
            return
        if error or not user_id or user_id != self._user_id:
            self.connection_state.emit("account_changed" if user_id and user_id != self._user_id else "session_lost")
            self.stop()
            return
        if not self._diagnostic_only:
            self._catch_up()

    def _catchup_chats(self, chats, error):
        if not self._active:
            return
        if error or chats is None:
            self._catchup_failed()
            return
        cutoff = (self._lost_at or time.time()) - 30
        self._catchup_unresolved = False
        self._catchup_queue = []
        for chat in chats:
            if chat.last_message is None:
                continue
            timestamp = self._timestamp(chat.last_message.created_at)
            if timestamp is None:
                self._catchup_unresolved = True
            elif timestamp >= cutoff:
                self._catchup_queue.append((chat.id, chat))
        self._refresh_chats(chats, None)
        self._catchup_next(cutoff)

    def _catchup_next(self, cutoff):
        if not self._active:
            return
        if not self._catchup_queue:
            self._catchup_active = False
            self._lost_at = None
            self.connection_state.emit("catch_up_incomplete" if self._catchup_unresolved else "caught_up")
            return
        chat_id, _chat = self._catchup_queue.pop(0)

        def messages_ready(messages, error):
            if not self._active:
                return
            if error or messages is None:
                self._catchup_failed()
                return
            for message in messages:
                timestamp = self._timestamp(message.created_at)
                if timestamp is None:
                    self._catchup_unresolved = True
                elif timestamp >= cutoff:
                    self._emit_message(message)
            self._catchup_next(cutoff)

        self.api.get_chat_messages(chat_id, messages_ready)

    def _catchup_failed(self):
        self._catchup_active = False
        self._catchup_queue.clear()
        self.connection_state.emit("catch_up_incomplete")
