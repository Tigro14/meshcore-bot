#!/usr/bin/env python3
"""
Ask command - Text-to-SQL agent for the MeshCore Bot.
Generates a SQL query from a natural language question and executes it.
"""

import asyncio
import re
from typing import Any

import requests

from ..models import MeshMessage
from .base_command import BaseCommand

DB_SCHEMA = """\
Tables:
- complete_contact_tracking: name, public_key, role(repeater/companion/roomserver/sensor), city, country, last_heard, hop_count, snr, signal_strength, is_currently_tracked, latitude, longitude
- message_stats: timestamp, sender_id, channel, content, is_dm, hops, snr, rssi
- observed_paths: public_key, path_hex, path_length, bytes_per_hop, observation_count, last_seen, snr, rssi
- mesh_connections: from_prefix, to_prefix, from_public_key, to_public_key, observation_count, last_seen, geographic_distance
- neighbor_links: self_public_key, neighbor_public_key, last_snr, best_snr, last_status, last_seen
- daily_stats: date, public_key, advert_count
- packet_stream: timestamp, type, data(JSON)

Notes:
- last_heard is a datetime string (ISO format)
- timestamp in message_stats is Unix epoch (integer)
- Use LIMIT 20 max
- Read-only: SELECT only
"""


class AskCommand(BaseCommand):
    """Text-to-SQL agent: converts natural language questions to SQL queries."""

    name = "ask"
    keywords = ["ask", "query", "sql"]
    description = "Ask a question about the mesh network data (contacts, messages, paths)"
    requires_dm = False
    cooldown_seconds = 30
    category = "special"

    short_description = "Query the mesh database with natural language"
    usage = "ask <question>"
    examples = [
        "ask how many repeaters are there",
        "ask top 5 most active senders last 7 days",
        "ask who are the neighbors of Cergy",
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.ask_enabled = self.get_config_value("Ask_Command", "enabled", fallback=True, value_type="bool")
        self.endpoint = self.get_config_value(
            "Llm_Command", "endpoint", fallback="http://127.0.0.1:8080/v1/chat/completions", value_type="str"
        )
        self.timeout_seconds = max(
            1.0,
            min(
                300.0,
                self.get_config_value("Llm_Command", "timeout_seconds", fallback=60.0, value_type="float"),
            ),
        )
        self.max_tokens = max(
            8, min(500, self.get_config_value("Llm_Command", "max_tokens", fallback=200, value_type="int"))
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.ask_enabled:
            return False
        return super().can_execute(message)

    def _extract_question(self, message: MeshMessage) -> str | None:
        prefix = (self.bot.config.get("Bot", "command_prefix", fallback="") or "").strip()
        content = message.content or ""
        keywords_alt = "|".join(re.escape(k) for k in [self.name] + self.keywords)
        if prefix:
            pattern = re.compile(rf"^{re.escape(prefix)}?(?:{keywords_alt})\s+(.+)", re.IGNORECASE)
        else:
            pattern = re.compile(rf"^(?:{keywords_alt})\s+(.+)", re.IGNORECASE)
        match = pattern.match(content)
        return match.group(1).strip() if match else None

    def _generate_sql(self, question: str) -> str | None:
        """Ask the LLM to generate a SQL query for the question."""
        system_prompt = (
            "You are a SQL query generator for a mesh network database. "
            "Given a question, respond with ONLY a single SQL SELECT query. "
            "No explanations, no markdown, just the SQL. "
            "Use LIMIT 20. Read-only. " + DB_SCHEMA
        )
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            "top_p": 0.9,
        }
        try:
            response = requests.post(self.endpoint, json=payload, timeout=self.timeout_seconds)
            if response.status_code != 200:
                return None
            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            # Extract SQL from response (handle markdown code blocks)
            sql_match = re.search(r"```(?:sql)?\s*(SELECT.+?)```", content, re.DOTALL | re.IGNORECASE)
            if sql_match:
                return sql_match.group(1).strip()
            # Try to find raw SELECT
            select_match = re.search(r"(SELECT\s+.+?)(?:;\s*)$", content, re.DOTALL | re.IGNORECASE)
            if select_match:
                return select_match.group(1).strip()
            # Fallback: if it starts with SELECT, use as-is
            if content.upper().startswith("SELECT"):
                return content.rstrip(";").strip()
            return None
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            self.logger.warning(f"Ask command SQL generation error: {e}")
            return None

    def _execute_sql(self, sql: str) -> tuple[list[str], list[tuple]] | None:
        """Execute a read-only SQL query with LIMIT enforcement."""
        # Enforce read-only
        if not sql.strip().upper().startswith("SELECT"):
            return None
        # Enforce LIMIT
        if not re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE):
            sql = sql.rstrip(";") + " LIMIT 20"
        # Cap LIMIT at 20
        sql = re.sub(r"\bLIMIT\s+\d+", "LIMIT 20", sql, flags=re.IGNORECASE)
        sql = sql.rstrip(";")

        try:
            with self.bot.db_manager.connection() as conn:
                conn.execute("PRAGMA query_only = ON")
                cursor = conn.cursor()
                cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                return columns, rows
        except Exception as e:
            self.logger.warning(f"Ask command SQL execution error: {e} | SQL: {sql[:200]}")
            return None

    def _format_results(self, columns: list[str], rows: list[tuple]) -> str:
        """Format query results as a readable string."""
        if not rows:
            return "(no results)"
        lines = [" | ".join(columns)]
        for row in rows[:20]:
            lines.append(" | ".join(str(v) if v is not None else "-" for v in row))
        return "\n".join(lines)

    async def execute(self, message: MeshMessage) -> bool:
        question = self._extract_question(message)
        if not question:
            pfx = (self.bot.config.get("Bot", "command_prefix", fallback="") or "").strip()
            return await self.send_response(message, f"Usage: {pfx}ask <question about the mesh>")

        self.logger.info(f"Ask command: {question}")

        # Step 1: Generate SQL
        sql = await asyncio.to_thread(self._generate_sql, question)
        if not sql:
            return await self.send_response(message, "Could not generate a query. Try rephrasing.")

        self.logger.debug(f"Ask command generated SQL: {sql}")

        # Step 2: Execute
        result = await asyncio.to_thread(self._execute_sql, sql)
        if result is None:
            return await self.send_response(message, "Query failed. Try a simpler question.")

        columns, rows = result
        formatted = self._format_results(columns, rows)

        # Truncate for mesh message limits
        max_length = self.get_max_message_length(message)
        if len(formatted) > max_length:
            formatted = formatted[: max_length - 3] + "..."

        return await self.send_response(message, formatted)
