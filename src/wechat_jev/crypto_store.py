from __future__ import annotations

import ctypes
import json
import os
import sqlite3
from contextlib import closing
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def dpapi_protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    target = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "微信Jev助手", None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


def dpapi_unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    target = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


class EncryptedHistoryStore:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "history.db"
        self.key_path = data_dir / "history.key.dpapi"
        self._key = self._load_or_create_key()
        self._initialize()

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return dpapi_unprotect(self.key_path.read_bytes())
        key = AESGCM.generate_key(bit_length=256)
        protected = dpapi_protect(key)
        temp = self.key_path.with_suffix(".tmp")
        temp.write_bytes(protected)
        os.replace(temp, self.key_path)
        return key

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            with db:
                db.execute(
                "CREATE TABLE IF NOT EXISTS analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "created_at TEXT NOT NULL, contact_hash TEXT NOT NULL, encrypted_payload BLOB NOT NULL, "
                "status TEXT NOT NULL)"
            )
                db.execute("CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS conversation_memories ("
                    "memory_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, contact_hash TEXT NOT NULL, "
                    "encrypted_payload BLOB NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversation_memories_updated "
                    "ON conversation_memories(updated_at)"
                )

    def _encrypt(self, value: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return nonce + AESGCM(self._key).encrypt(nonce, raw, None)

    def _decrypt(self, value: bytes) -> dict[str, Any]:
        nonce, ciphertext = value[:12], value[12:]
        raw = AESGCM(self._key).decrypt(nonce, ciphertext, None)
        return json.loads(raw.decode("utf-8"))

    def add(self, contact: str, payload: dict[str, Any], status: str = "ok") -> int:
        import hashlib

        contact_hash = hashlib.sha256(contact.encode("utf-8")).hexdigest()
        with closing(self._connect()) as db:
            with db:
                cursor = db.execute(
                "INSERT INTO analyses(created_at, contact_hash, encrypted_payload, status) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), contact_hash, self._encrypt(payload), status),
            )
                return int(cursor.lastrowid)

    def list_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, created_at, encrypted_payload, status FROM analyses ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"id": row["id"], "created_at": row["created_at"], "status": row["status"], **self._decrypt(row["encrypted_payload"])}
            for row in rows
        ]

    def upsert_memory(self, memory_id: str, contact: str, payload: dict[str, Any]) -> None:
        import hashlib

        contact_hash = hashlib.sha256(contact.encode("utf-8")).hexdigest()
        with closing(self._connect()) as db:
            with db:
                db.execute(
                    "INSERT INTO conversation_memories(memory_id, updated_at, contact_hash, encrypted_payload) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(memory_id) DO UPDATE SET "
                    "updated_at=excluded.updated_at, contact_hash=excluded.contact_hash, "
                    "encrypted_payload=excluded.encrypted_payload",
                    (
                        memory_id,
                        datetime.now(timezone.utc).isoformat(),
                        contact_hash,
                        self._encrypt(payload),
                    ),
                )

    def list_memories(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT memory_id, updated_at, encrypted_payload FROM conversation_memories "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "updated_at": row["updated_at"],
                **self._decrypt(row["encrypted_payload"]),
            }
            for row in rows
        ]

    def delete_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as db:
            with db:
                cursor = db.execute(f"DELETE FROM analyses WHERE id IN ({placeholders})", ids)
                return cursor.rowcount

    def delete_all(self) -> int:
        with closing(self._connect()) as db:
            with db:
                cursor = db.execute("DELETE FROM analyses")
                deleted = cursor.rowcount
                db.execute("DELETE FROM conversation_memories")
                return deleted
