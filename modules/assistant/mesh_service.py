#!/usr/bin/env python3
"""
Network data service extracted from the original Ask command.
Generates a SQL query from a natural language question and executes it.
"""

import asyncio
import re
import sqlite3
import time
from typing import Any

import requests

from ..db_manager import validate_readonly_sql
from ..models import MeshMessage
from ..utils import get_cpu_temperature
from .llm_client import apply_reasoning_effort, configured_reasoning_effort, post_chat
from .sender_policy import sender_may_use_llm

DB_SCHEMA = """\
Tables:
- complete_contact_tracking: name, public_key, role(repeater/companion/roomserver/sensor), city, country, last_heard, hop_count, snr, signal_strength, is_currently_tracked, latitude, longitude
- message_stats: timestamp, sender_id, channel, content, is_dm, hops, snr, rssi
- observed_paths: public_key, path_hex, path_length, bytes_per_hop, observation_count, last_seen, snr, rssi
- mesh_connections: from_prefix, to_prefix, from_public_key, to_public_key, observation_count, last_seen, geographic_distance
- neighbor_links: self_public_key, neighbor_public_key, last_snr, best_snr, last_status, last_seen
- daily_stats: date, public_key, advert_count
- repeater_contacts: public_key, name, device_type, last_seen, latitude, longitude, city, country, is_active
- unique_advert_packets: date, public_key, packet_hash, first_seen
- path_stats: timestamp, sender_id, channel, path_length, path_string, hops

Notes:
- last_heard is a datetime string (ISO format)
- timestamp in message_stats is Unix epoch (integer)
- Use LIMIT 20 max
- Read-only: SELECT only
- Many rows have NULL latitude/longitude. For distance queries ALWAYS add: WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND latitude != 0
- No firmware version or hardware/model info is stored in this database. If asked about version or hardware, reply: 'not tracked in DB'
- Only query the tables listed above. Do NOT query: bbs_messages, bot_metadata, channels, clock_sync_*, command_stats, daily_rollup, dashboard_snapshot, feed_*, generic_cache, geocoding_cache, greeted_users, greeter_rollout, neighbor_observations, packet_stream, purging_log, schema_version
- ALWAYS resolve public_key to name: JOIN complete_contact_tracking c ON c.public_key = <table>.public_key and SELECT c.name. Truncate names to 15 chars: SUBSTR(c.name, 1, 15) AS name. Never SELECT a raw public_key or prefix as the primary identifier.
- When selecting a distance, alias it with its unit, e.g. ROUND(<haversine>,1) AS distance_km
"""

NETWORK_TABLES = {
    "complete_contact_tracking", "message_stats", "observed_paths",
    "mesh_connections", "neighbor_links", "daily_stats", "repeater_contacts",
    "unique_advert_packets", "path_stats",
}


class MeshService:
    """Natural-language queries over locally observed mesh data; no RF output."""

    def __init__(self, bot: Any, config_reader):
        self.bot = bot
        self.logger = bot.logger
        self.get_config_value = config_reader
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
        self.model = self.get_config_value("Llm_Command", "model", fallback="", value_type="str")
        self.reasoning_effort = configured_reasoning_effort(self.get_config_value)


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


    def _live_db_schema(self, *, include_empty: bool = True) -> str:
        """Return the authoritative columns for the allowed tables in this database."""
        try:
            with self.bot.db_manager.connection() as conn:
                existing = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                    if row[0] in NETWORK_TABLES
                }
                entries: list[tuple[int, str]] = []
                for table in sorted(existing):
                    columns = []
                    for column in conn.execute(f'PRAGMA table_info("{table}")'):
                        name = column[1]
                        declared_type = (column[2] or "").strip().upper()
                        columns.append(f"{name} {declared_type}".strip())
                    if columns:
                        row_count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                        entries.append((row_count, f"- {table} [rows={row_count}]: {', '.join(columns)}"))
                lines = [line for count, line in entries if include_empty or count > 0]
                if not lines and entries:
                    lines = [line for _, line in entries]
                if lines:
                    return "Live SQLite schema (authoritative):\n" + "\n".join(lines)
        except Exception as exc:
            self.logger.warning("Mesh live schema inspection failed: %s", exc)
        return DB_SCHEMA


    def _generate_sql(
        self,
        question: str,
        sender_pos: tuple[float, float] | None,
        *,
        failed_sql: str | None = None,
        sqlite_error: str | None = None,
    ) -> str | None:
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

        ranking_question = self._is_ranking_question(question)
        count_question = self._is_count_question(question)
        constrained_retry = bool(
            failed_sql
            and (sqlite_error == "query returned no rows" or ranking_question or count_question)
        )
        schema = self._live_db_schema(include_empty=not constrained_retry)
        ranking_instruction = ""
        if ranking_question:
            ranking_instruction = (
                "\nThis is a ranking question. Preserve every entity constraint from the question. "
                "If no ranking criterion is stated, use the highest available activity or observation count. "
                "Select the item name and the numeric metric, ORDER BY that metric DESC, and LIMIT 1. "
                "Never return an unranked list."
            )
        count_instruction = ""
        if count_question:
            count_instruction = (
                "\nThis is a counting question. The SELECT list MUST use a COUNT(...) aggregate "
                "and return counts, never a list of matching entity names. Use COUNT(DISTINCT ...) "
                "when joins could duplicate the entities being counted."
            )
        correction = ""
        if failed_sql and sqlite_error:
            empty_result_instruction = ""
            if constrained_retry:
                empty_result_instruction = (
                    " Use a table from the schema below; every listed table now contains rows."
                )
            correction = (
                "\nA previous query did not produce usable data. Correct it using the authoritative schema above. "
                "Do not repeat a table or column name that SQLite rejected."
                + empty_result_instruction
                + "\n"
                f"FAILED_SQL_BEGIN\n{failed_sql[:1200]}\nFAILED_SQL_END\n"
                f"SQLITE_ERROR_BEGIN\n{sqlite_error[:300]}\nSQLITE_ERROR_END\n"
            )

        system_prompt = (
            "You are a SQL query generator for a mesh network database. "
            "Given a question, respond with ONLY a single SQL SELECT query. "
            "No explanations, no markdown, just the SQL. "
            "Use LIMIT 20. Read-only. "
            "The live SQLite schema below is authoritative: use only its exact table and column names. "
            "Never infer, translate, or invent an identifier. "
            "Prefer a relevant table with rows over an empty table. "
            "For rankings or superlatives, select the item name and the real metric used to rank it. "
            "Every query must read at least one table from the live schema; never SELECT literal data as an answer. "
            + schema + haversine + pos_info + ranking_instruction + count_instruction + correction
        )
        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            "top_p": 0.9,
        }
        if self.model:
            payload["model"] = self.model
        apply_reasoning_effort(payload, self.reasoning_effort)
        try:
            response = post_chat(self.endpoint, payload, self.timeout_seconds)
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
            self.logger.warning(f"Mesh service SQL generation error: {e}")
            return None


    def _execute_sql_detailed(self, sql: str) -> tuple[str, str | None]:
        """Execute read-only SQL and retain a private SQLite error for one repair attempt."""
        ok, reason = validate_readonly_sql(sql)
        if not ok:
            return f"(rejected: {reason})", None
        sql = sql.strip().rstrip(";")
        if not re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE):
            sql += " LIMIT 20"
        sql = re.sub(
            r"\bLIMIT\s+(\d+)",
            lambda match: f"LIMIT {min(int(match.group(1)), 20)}",
            sql,
            flags=re.IGNORECASE,
        )
        sql = sql.rstrip(";")

        read_tables: set[str] = set()

        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and table not in NETWORK_TABLES:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ:
                read_tables.add(table)
            return sqlite3.SQLITE_OK

        try:
            with self.bot.db_manager.connection() as conn:
                previous_query_only = conn.execute("PRAGMA query_only").fetchone()[0]
                conn.execute("PRAGMA query_only = ON")
                deadline = time.monotonic() + 2.0
                conn.set_authorizer(authorize)
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                try:
                    rows = conn.execute(sql).fetchmany(20)
                finally:
                    conn.set_authorizer(None)
                    conn.set_progress_handler(None, 0)
                    conn.execute(f"PRAGMA query_only = {int(previous_query_only)}")
                if not read_tables:
                    return "(query error: unavailable or unauthorized data)", "query did not read an allowed network table"
                if not rows:
                    return "(no results)", None
                result = "\n".join(", ".join(str(v) for v in row if v is not None) for row in rows)
                return result, None
        except Exception as e:
            self.logger.warning("Mesh query failed: %s | SQL: %s", e, sql[:500])
            return "(query error: unavailable or unauthorized data)", str(e)


    def _execute_sql(self, sql: str) -> str:
        """Compatibility wrapper returning only the public query result."""
        return self._execute_sql_detailed(sql)[0]


    @staticmethod
    def _is_repairable_sql_error(error: str | None) -> bool:
        """Limit model retries to SQL construction errors, never authorization failures."""
        if not error:
            return False
        lowered = error.casefold()
        return any(
            marker in lowered
            for marker in (
                "no such column",
                "no such table",
                "ambiguous column name",
                "syntax error",
                "misuse of aggregate",
                "wrong number of arguments",
                "query did not read an allowed network table",
                "query returned no rows",
                "ranking query must",
                "query must preserve requested entity type",
                "counting query must",
            )
        )

    @staticmethod
    def _is_ranking_question(question: str) -> bool:
        normalized = (question or "").casefold()
        return any(
            marker in normalized
            for marker in (
                "meilleur", "meilleure", "meilleurs", "top ", "best ",
                "plus actif", "plus active", "most active", "strongest",
                "plus fort", "plus forte", "nearest", "closest", "plus proche",
            )
        )

    @staticmethod
    def _is_count_question(question: str) -> bool:
        """Recognize requests whose answer must contain aggregate counts."""
        normalized = " ".join((question or "").casefold().split())
        return bool(
            re.search(r"\bcombien\b", normalized)
            or re.search(r"\bnombre\s+(?:de|d['’])", normalized)
            or re.search(r"\bhow\s+many\b", normalized)
            or re.search(r"\bnumber\s+of\b", normalized)
        )

    @classmethod
    def _question_shape_error(cls, question: str, sql: str) -> str | None:
        requested_role = cls._requested_role(question)
        if requested_role:
            lowered_sql = sql.casefold()
            role_is_implicit = requested_role == "repeater" and "repeater_contacts" in lowered_sql
            if not role_is_implicit and not re.search(
                rf"['\"]{re.escape(requested_role)}['\"]", lowered_sql
            ):
                return f"query must preserve requested entity type {requested_role}"
        if cls._is_ranking_question(question):
            if not re.search(r"\bORDER\s+BY\b", sql, re.IGNORECASE):
                return "ranking query must include ORDER BY a real metric"
            if not re.search(r"\bLIMIT\s+1\b", sql, re.IGNORECASE):
                return "ranking query must use LIMIT 1"
        if cls._is_count_question(question) and not re.search(r"\bCOUNT\s*\(", sql, re.IGNORECASE):
            return "counting query must use COUNT(...) instead of returning entity rows"
        return None

    @staticmethod
    def _requested_role(question: str) -> str | None:
        normalized = (question or "").casefold()
        role_markers = {
            "repeater": ("répéteur", "repeteur", "repeater"),
            "companion": ("companion",),
            "roomserver": ("roomserver", "room server"),
            "sensor": ("capteur", "sensor"),
        }
        for role, markers in role_markers.items():
            if any(marker in normalized for marker in markers):
                return role
        return None

    @classmethod
    def _apply_requested_role_constraint(cls, question: str, sql: str) -> str:
        """Add a missing role predicate when querying the shared contacts table."""
        role = cls._requested_role(question)
        if not role or "complete_contact_tracking" not in sql.casefold():
            return sql
        if re.search(rf"['\"]{re.escape(role)}['\"]", sql, re.IGNORECASE):
            return sql

        table = re.search(
            r"\bFROM\s+complete_contact_tracking\b(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?",
            sql,
            re.IGNORECASE,
        )
        alias = table.group(1) if table else None
        if alias and alias.casefold() in {"where", "join", "order", "group", "limit", "having"}:
            alias = None
        predicate = f"{alias + '.' if alias else ''}role = '{role}'"
        suffix = re.search(r"\b(?:GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING)\b", sql, re.IGNORECASE)
        split_at = suffix.start() if suffix else len(sql)
        head, tail = sql[:split_at].rstrip(), sql[split_at:].lstrip()
        connector = " AND " if re.search(r"\bWHERE\b", head, re.IGNORECASE) else " WHERE "
        return f"{head}{connector}{predicate}" + (f" {tail}" if tail else "")

    def describe_tables(self) -> str:
        """Return the actual allowed network tables and columns, as Tigro's ask did."""
        try:
            with self.bot.db_manager.connection() as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                lines = []
                for row in rows:
                    table = row[0]
                    if table not in NETWORK_TABLES:
                        continue
                    columns = [column[1] for column in conn.execute(f'PRAGMA table_info("{table}")')]
                    lines.append(f"{table}: {', '.join(columns)}")
                return "\n".join(lines) if lines else "(no tables found)"
        except Exception as exc:
            self.logger.warning("Mesh table description failed: %s", exc)
            return "(table list unavailable)"


    def _question_asks_for_datetime(self, question: str) -> bool:
        """Return True if the question is specifically about a date/time (keep timestamps)."""
        q = (question or "").lower()
        markers = (
            "date", "jour", "quand", "qu'elle", "qu'elle est", "dernier", "derniere", "dernière",
            "récent", "recente", "récente", "recent", "latest", "when", "time", "heure",
            "timestamp", "moment", "plus récent", "plus recemment", "plus récente",
        )
        return any(m in q for m in markers)


    def _postprocess_results(self, results: str, keep_datetime: bool = False) -> str:
        """Clean raw SQL results before they are formatted for the mesh.

        1. Replace hex public keys / prefixes with node names (best-effort).
           The LLM sometimes selects a ``public_key`` (hex, commonly 64 chars but
           occasionally longer) or a short prefix (e.g. ``from_prefix``) instead of a
           name. Each such token is resolved against ``complete_contact_tracking``
           (exact match first, then prefix) and swapped for the node name.
           A token is a candidate if it is 8+ hex chars; prefix matching is only
           attempted when it contains at least one hex letter (a-f), so pure numeric
           values (timestamps, counts) are not mistaken for key prefixes.
        2. Drop verbose ISO datetimes (date + time) to save message space — unless
           the question is specifically about a date/time, in which case they are kept.
        """
        if not results or not results.strip():
            return results
        candidates: set[str] = set()
        for line in results.splitlines():
            for part in line.split(", "):
                p = part.strip()
                if re.fullmatch(r"[0-9a-fA-F]{8,}", p):
                    candidates.add(p.lower())
        name_map: dict[str, str] = {}
        if candidates:
            try:
                with self.bot.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    for cand in candidates:
                        name = None
                        # 1) Exact match (any length).
                        cursor.execute(
                            "SELECT name FROM complete_contact_tracking WHERE public_key = ? LIMIT 1",
                            (cand,),
                        )
                        row = cursor.fetchone()
                        if row:
                            name = row[0]
                        # 2) Prefix match, only if the token has at least one hex letter.
                        if not name and re.search(r"[a-fA-F]", cand):
                            cursor.execute(
                                "SELECT name FROM complete_contact_tracking WHERE public_key LIKE ? LIMIT 1",
                                (cand + "%",),
                            )
                            row = cursor.fetchone()
                            if row:
                                name = row[0]
                        if name:
                            name_map[cand] = name[:15]
            except Exception as e:
                self.logger.debug(f"Mesh service pubkey->name resolution error: {e}")

        def _process_part(part: str) -> str | None:
            p = part.strip()
            # Resolve a public key / prefix to a name.
            if p.lower() in name_map:
                return name_map[p.lower()]
            # Drop verbose datetimes unless the question is about a date/time.
            if not keep_datetime and self._DATETIME_RE.match(p):
                return None
            return part

        out_lines = []
        for line in results.splitlines():
            parts = [processed for processed in (_process_part(part) for part in line.split(", ")) if processed is not None]
            if parts:
                out_lines.append(", ".join(parts))
        return "\n".join(out_lines)


    def _format_followup(self, question: str, sql_results: str) -> str | None:
        """Second LLM call to format results into a mesh-friendly answer."""
        prompt = (
            f"The query returned these results:\n{sql_results}\n\n"
            f"Answer the question: {question}\n\n"
            "FORMAT RULES (mesh network, max 150 chars per message):\n"
            "- One item per line: 'name: value unit'\n"
            "- ALWAYS attach a unit to every number: km for distance, hops for path length, messages for counts, days/hours/minutes for time, % for percentages, dBm for signal strength, bytes for data\n"
            "- Use node NAMES, never hex public keys or short prefixes\n"
            "- NEVER show raw coordinates (lat/lon) or raw hex keys\n"
            "- Use only facts and values present in the query results; never add a metric, name, value, or conclusion\n"
            "- Max 10 items, no tables, no pipes\n"
            "- Total response under 500 chars\n"
            "- If the data is a single aggregate (count, total), just answer with the number and its unit"
        )
        payload: dict[str, Any] = {
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
        if self.model:
            payload["model"] = self.model
        apply_reasoning_effort(payload, self.reasoning_effort)
        try:
            response = post_chat(self.endpoint, payload, self.timeout_seconds)
            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            self.logger.warning(f"Mesh service followup error: {e}")
        return None

    @classmethod
    def _format_single_count(cls, question: str, sql_results: str) -> str | None:
        """Return a stable one-line answer for a single aggregate count."""
        match = re.fullmatch(r"\s*(\d+)\s*", sql_results or "")
        if not match or not cls._is_count_question(question):
            return None
        count = int(match.group(1))
        role = cls._requested_role(question)
        english = bool(re.search(r"\b(?:how\s+many|number\s+of)\b", question.casefold()))
        labels = {
            "repeater": ("repeater", "repeaters") if english else ("répéteur", "répéteurs"),
            "companion": ("companion", "companions") if english else ("compagnon", "compagnons"),
            "roomserver": ("room server", "room servers") if english else ("serveur de salon", "serveurs de salon"),
            "sensor": ("sensor", "sensors") if english else ("capteur", "capteurs"),
        }
        if role in labels:
            singular, plural = labels[role]
            return f"{count} {singular if count == 1 else plural}"
        return f"Total: {count}"


    @staticmethod
    def _formatted_is_grounded(formatted: str, sql_results: str) -> bool:
        """Reject formatted answers that introduce numeric facts absent from SQL."""
        number_pattern = r"(?<![\w])[-+]?\d+(?:[.,]\d+)?"

        def numbers(text: str) -> set[str]:
            return {match.replace(",", ".") for match in re.findall(number_pattern, text or "")}

        return numbers(formatted).issubset(numbers(sql_results))


    _DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$")

    async def answer(self, question: str, message: MeshMessage) -> str:
        if not sender_may_use_llm(self.bot, message):
            return ""
        if question.strip().casefold() in {
            "tables", "table", "schema", "db", "base de données", "base de donnees", "database"
        }:
            return await asyncio.to_thread(self.describe_tables)
        threshold = self.get_config_value("Llm_Command", "cpu_temp_threshold", fallback=60.0, value_type="float")
        temperature = get_cpu_temperature() if threshold > 0 else None
        if temperature is not None and temperature >= threshold:
            return "trop chaud:"
        # Get sender position for distance-based queries
        sender_pos = self._get_sender_position(message)

        # Step 1: Generate SQL
        sql = await asyncio.to_thread(self._generate_sql, question, sender_pos)
        if not sql:
            return "Could not generate a query. Try rephrasing."
        sql = self._apply_requested_role_constraint(question, sql)

        self.logger.debug(f"Mesh service generated SQL: {sql}")

        # Step 2: Execute
        sql_results, sql_error = "(query error: invalid query)", None
        for attempt in range(3):
            shape_error = self._question_shape_error(question, sql)
            if shape_error:
                sql_results, sql_error = "(query error: invalid query shape)", shape_error
            else:
                sql_results, sql_error = await asyncio.to_thread(self._execute_sql_detailed, sql)
            if sql_results == "(no results)" and sql_error is None:
                sql_error = "query returned no rows"
            if not self._is_repairable_sql_error(sql_error) or attempt == 2:
                break
            self.logger.info("Mesh query invalid; regenerating from the live SQLite schema")
            repaired_sql = await asyncio.to_thread(
                self._generate_sql,
                question,
                sender_pos,
                failed_sql=sql,
                sqlite_error=sql_error,
            )
            if not repaired_sql:
                break
            sql = self._apply_requested_role_constraint(question, repaired_sql)
            self.logger.debug("Mesh service repaired SQL: %s", sql)
        if sql_results.startswith(("(rejected:", "(query error:", "(no results)")):
            return "Aucune donnée disponible." if sql_results == "(no results)" else "La requête réseau n’a pas pu être exécutée."
        # Step 2b: Post-process — resolve pubkeys to names, drop verbose datetimes
        #          (unless the question is specifically about a date/time).
        keep_dt = self._question_asks_for_datetime(question)
        sql_results = await asyncio.to_thread(self._postprocess_results, sql_results, keep_dt)
        self.logger.debug(f"Mesh service SQL results: {sql_results[:500]}")

        count_answer = self._format_single_count(question, sql_results)
        if count_answer:
            return count_answer

        # Step 3: Format with LLM followup
        formatted = await asyncio.to_thread(self._format_followup, question, sql_results)
        if formatted and not self._formatted_is_grounded(formatted, sql_results):
            self.logger.warning("Mesh formatted answer introduced values absent from SQL; using raw results")
            formatted = None
        if not formatted:
            formatted = sql_results

        return formatted
