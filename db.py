import os
from datetime import date, timedelta

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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS checkin_sessions (
                patient_chat_id BIGINT PRIMARY KEY,
                step INTEGER NOT NULL,
                answers JSONB NOT NULL,
                lang TEXT NOT NULL,
                multiselect_selected INTEGER[] NOT NULL DEFAULT '{}'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS checkin_schedules (
                id SERIAL PRIMARY KEY,
                patient_chat_id BIGINT NOT NULL,
                day INTEGER NOT NULL,
                due_date DATE NOT NULL,
                sent BOOLEAN NOT NULL DEFAULT FALSE
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


def get_all_patients() -> list[tuple[int, str | None, str | None]]:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT chat_id, full_name, username FROM patients ORDER BY full_name")
        return cur.fetchall()


def delete_patient(chat_id: int) -> bool:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM patients WHERE chat_id = %s", (chat_id,))
        conn.commit()
        return cur.rowcount > 0


def save_checkin(patient_chat_id: int, answers: dict, severity: str) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkins (patient_chat_id, answers, severity) VALUES (%s, %s, %s)",
            (patient_chat_id, Json(answers), severity),
        )
        conn.commit()


def save_checkin_session(patient_chat_id: int, state: dict) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO checkin_sessions (patient_chat_id, step, answers, lang, multiselect_selected)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (patient_chat_id) DO UPDATE SET
                step = EXCLUDED.step,
                answers = EXCLUDED.answers,
                lang = EXCLUDED.lang,
                multiselect_selected = EXCLUDED.multiselect_selected
            """,
            (
                patient_chat_id,
                state["step"],
                Json(state["answers"]),
                state["lang"],
                sorted(state.get("multiselect_selected", set())),
            ),
        )
        conn.commit()


def get_checkin_session(patient_chat_id: int) -> dict | None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step, answers, lang, multiselect_selected FROM checkin_sessions WHERE patient_chat_id = %s",
            (patient_chat_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        step, answers, lang, multiselect_selected = row
        return {"step": step, "answers": answers, "lang": lang, "multiselect_selected": set(multiselect_selected)}


def has_checkin_session(patient_chat_id: int) -> bool:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM checkin_sessions WHERE patient_chat_id = %s", (patient_chat_id,))
        return cur.fetchone() is not None


def delete_checkin_session(patient_chat_id: int) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM checkin_sessions WHERE patient_chat_id = %s", (patient_chat_id,))
        conn.commit()


def schedule_followups(patient_chat_id: int, anchor: date) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        for day, offset in ((3, 2), (7, 6)):
            cur.execute(
                "INSERT INTO checkin_schedules (patient_chat_id, day, due_date) VALUES (%s, %s, %s)",
                (patient_chat_id, day, anchor + timedelta(days=offset)),
            )
        conn.commit()


def get_due_schedules(today: date) -> list[tuple[int, int]]:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, patient_chat_id FROM checkin_schedules WHERE due_date <= %s AND sent = FALSE",
            (today,),
        )
        return cur.fetchall()


def mark_schedule_sent(schedule_id: int) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE checkin_schedules SET sent = TRUE WHERE id = %s", (schedule_id,))
        conn.commit()
