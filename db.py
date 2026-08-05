import os

import psycopg2
from psycopg2.extras import Json

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS patients (
                chat_id BIGINT PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                language TEXT NOT NULL DEFAULT 'uk'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS checkins (
                id SERIAL PRIMARY KEY,
                patient_chat_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                answers JSONB NOT NULL,
                severity TEXT NOT NULL
            )
            """
        )
        conn.commit()


def upsert_patient(chat_id: int, full_name: str, username: str | None) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO patients (chat_id, full_name, username)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET full_name = EXCLUDED.full_name, username = EXCLUDED.username
            """,
            (chat_id, full_name, username),
        )
        conn.commit()


def get_language(chat_id: int) -> str:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT language FROM patients WHERE chat_id = %s", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else "uk"


def set_language(chat_id: int, language: str) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE patients SET language = %s WHERE chat_id = %s", (language, chat_id))
        conn.commit()


def save_checkin(patient_chat_id: int, answers: dict, severity: str) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkins (patient_chat_id, answers, severity) VALUES (%s, %s, %s)",
            (patient_chat_id, Json(answers), severity),
        )
        conn.commit()
