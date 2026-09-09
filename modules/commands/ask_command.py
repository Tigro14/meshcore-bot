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

Notes:
- last_heard is a datetime string (ISO format)
- timestamp in message_stats is Unix epoch (integer)
- Use LIMIT 20 max
- Read-only: SELECT only
- Many rows have NULL latitude/longitude. For distance queries ALWAYS add: WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0
"""


class AskCommand(BaseCommand):
    """Text-to-SQL agent: converts natural language questions to SQL queries."""

    name = "ask"
    keywords = ["ask", "query", "sql"]
    description = "Ask a question about the mesh network data (contacts, messages, paths)"
    requires_dm = False
    cooldown_seconds = 30
    category = "special"

    short_description = "Query the mesh database with natural language (contacts, messages, paths, stats)"
    usage = "ask <question about the mesh>"
    examples = [
        "ask combien de répéteurs actifs sur 7 jours",
        "ask top 10 expéditeurs sur 30 jours",
        "ask quel est le chemin le plus long observé",
        "ask évolution des contacts uniques sur 30 jours",
        "ask les nœuds avec le meilleur SNR",
        "ask combien de messages par jour cette semaine",
        "ask quels pays sont représentés dans le mesh",
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

    def _get_sender_position(self, message: MeshMessage) -> tuple[float, float] | None:
        """Look up the sender's GPS position from the DB."""
        sender_id = message.sender_id or ""
        if not sender_id:
            return None
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT latitude, longitude FROM complete_contact_tracking WHERE name = ? AND latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0 LIMIT 1",
                    (sender_id,),
                )
                row = cursor.fetchone()
                if not row:
                    cursor.execute(
                        "SELECT latitude, longitude FROM complete_contact_tracking WHERE public_key = ? AND latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0 LIMIT 1",
                        (sender_id,),
                    )
                    row = cursor.fetchone()
                if row:
                    return (float(row[0]), float(row[1]))
        except Exception:
            pass
        return None

    def _generate_sql(self, question: str, sender_pos: tuple[float, float] | None) -> str | None:
        """Ask the LLM to generate a SQL query for the question."""
        pos_info = ""
        haversine = ""
        if sender_pos:
            lat, lon = sender_pos
            pos_info = f"\nUser position: {lat:.5f}, {lon:.5f} (use for distance calculations)"
            haversine = (
                f"\nHaversine formula: 6371*2*ASIN(SQRT("
                f"POWER(SIN(RADIANS(latitude-{lat:.5f})/2),2)+"
                f"COS(RADIANS({lat:.5f}))*COS(RADIANS(latitude))*"
                f"POWER(SIN(RADIANS(longitude-{lon:.5f})/2),2)))"
            )

        system_prompt = (
            "You are a SQL query generator for a mesh network database. "
            "Given a question, respond with ONLY a single SQL SELECT query. "
            "No explanations, no markdown, just the SQL. "
            "Use LIMIT 20. Read-only. " + DB_SCHEMA + haversine + pos_info
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
            sql_match = re.search(r"```(?:sql)?\s*(SELECT.+?)```", content, re.DOTALL | re.IGNORECASE)
            if sql_match:
                return sql_match.group(1).strip()
            select_match = re.search(r"(SELECT\s+.+?)(?:;\s*)$", content, re.DOTALL | re.IGNORECASE)
            if select_match:
                return select_match.group(1).strip()
            if content.upper().startswith("SELECT"):
                return content.rstrip(";").strip()
            return None
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            self.logger.warning(f"Ask command SQL generation error: {e}")
            return None

    def _execute_sql(self, sql: str) -> str:
        """Execute a read-only SQL query and return compact results."""
        if not sql.strip().upper().startswith("SELECT"):
            return "(not a SELECT query)"
        if not re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE):
            sql += " LIMIT 20"
        sql = re.sub(r"\bLIMIT\s+\d+", "LIMIT 20", sql, flags=re.IGNORECASE)
        sql = sql.rstrip(";")

        try:
            with self.bot.db_manager.connection() as conn:
                conn.execute("PRAGMA query_only = ON")
                cursor = conn.cursor()
                cursor.execute(sql)
                rows = cursor.fetchall()
                if not rows:
                    return "(no results)"
                lines = []
                for row in rows[:20]:
                    parts = [str(v) for v in row if v is not None]
                    lines.append(", ".join(parts))
                return "\n".join(lines)
        except Exception as e:
            self.logger.warning(f"Ask command SQL execution error: {e} | SQL: {sql[:200]}")
            return f"(query error: {e})"

    def _format_followup(self, question: str, sql_results: str) -> str | None:
        """Second LLM call to format results into a mesh-friendly answer."""
        prompt = (
            f"The query returned these results:\n{sql_results}\n\n"
            f"Answer the question: {question}\n\n"
            "FORMAT RULES (mesh network, max 150 chars per message):\n"
            "- One item per line: 'name: value unit'\n"
            "- Use the CORRECT unit for the data: km/m for distance, messages for counts, days/hours for time, % for percentages\n"
            "- NEVER show raw coordinates (lat/lon)\n"
            "- Max 10 items, no tables, no pipes\n"
            "- Total response under 500 chars\n"
            "- If the data is a single aggregate (count, total), just answer with the number and its unit"
        )
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": "You are a concise assistant on a low-bandwidth mesh network. Reply briefly.",
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 150,
            "temperature": 0.2,
            "top_p": 0.9,
        }
        try:
            response = requests.post(self.endpoint, json=payload, timeout=self.timeout_seconds)
            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError, IndexError) as e:
            self.logger.warning(f"Ask command followup error: {e}")
        return None

    async def execute(self, message: MeshMessage) -> bool:
        question = self._extract_question(message)
        if not question:
            pfx = (self.bot.config.get("Bot", "command_prefix", fallback="") or "").strip()
            return await self.send_response(message, f"Usage: {pfx}ask <question about the mesh>")

        self.logger.info(f"Ask command: {question}")

        # Get sender position for distance-based queries
        sender_pos = self._get_sender_position(message)

        # Step 1: Generate SQL
        sql = await asyncio.to_thread(self._generate_sql, question, sender_pos)
        if not sql:
            return await self.send_response(message, "Could not generate a query. Try rephrasing.")

        self.logger.debug(f"Ask command generated SQL: {sql}")

        # Step 2: Execute
        sql_results = await asyncio.to_thread(self._execute_sql, sql)
        self.logger.debug(f"Ask command SQL results: {sql_results[:500]}")

        # Step 3: Format with LLM followup
        formatted = await asyncio.to_thread(self._format_followup, question, sql_results)
        if not formatted:
            formatted = sql_results

        # Truncate for mesh message limits
        max_length = self.get_max_message_length(message)
        if len(formatted) > max_length:
            formatted = formatted[:max_length]

        return await self.send_response(message, formatted)
