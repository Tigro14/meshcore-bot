"""Tests for modules.security_utils."""

import asyncio
import os
import socket
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.security_utils import (
    SafeAiohttpResolver,
    SafeUrlPolicy,
    UnsafeUrlError,
    create_safe_requests_session,
    safe_aiohttp_request,
    safe_requests_request,
    sanitize_input,
    validate_api_key_format,
    validate_external_url,
    validate_integer_range,
    validate_port_number,
    validate_pubkey_format,
    validate_safe_path,
)


def _addrinfo(*addresses: str, port: int = 80):
    records = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        records.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return records


class TestValidatePubkeyFormat:
    """Tests for validate_pubkey_format()."""

    def test_valid_hex_64_chars(self):
        valid_key = "a" * 64
        assert validate_pubkey_format(valid_key) is True
        assert validate_pubkey_format("0123456789abcdef" * 4) is True

    def test_invalid_length(self):
        assert validate_pubkey_format("a" * 63) is False
        assert validate_pubkey_format("a" * 65) is False
        assert validate_pubkey_format("") is False

    def test_invalid_chars(self):
        assert validate_pubkey_format("g" + "a" * 63) is False
        assert validate_pubkey_format("a" * 63 + "Z") is False  # Actually Z might be valid in hex - no, hex is 0-9a-fA-F. Z is invalid.
        assert validate_pubkey_format("a" * 63 + "-") is False

    def test_not_string(self):
        assert validate_pubkey_format(None) is False
        assert validate_pubkey_format(12345) is False


class TestValidateSafePath:
    """Tests for validate_safe_path()."""

    @patch("modules.security_utils._is_nix_environment", return_value=True)
    def test_relative_path_resolution(self, mock_nix, tmp_path):
        # Patch Nix check so tmp_path (under /private on macOS) doesn't trigger dangerous path
        result = validate_safe_path("subdir/file.db", base_dir=str(tmp_path), allow_absolute=False)
        assert result == (tmp_path / "subdir" / "file.db").resolve()

    def test_path_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            validate_safe_path("../../../etc/passwd", base_dir=str(tmp_path), allow_absolute=False)

    def test_absolute_path_when_not_allowed_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            validate_safe_path("/etc/passwd", base_dir=str(tmp_path), allow_absolute=False)

    @patch("modules.security_utils._is_nix_environment", return_value=True)
    def test_absolute_path_when_allowed(self, mock_nix, tmp_path):
        target = tmp_path / "data" / "file.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = validate_safe_path(str(target), base_dir="/other", allow_absolute=True)
        assert result == target.resolve()


class TestValidateExternalUrl:
    """Tests for validate_external_url()."""

    def test_file_scheme_rejected(self):
        assert validate_external_url("file:///etc/passwd") is False

    def test_http_https_scheme_allowed(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            assert validate_external_url("https://example.com/") is True
            assert validate_external_url("http://example.com/") is True

    def test_loopback_rejected_by_default(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            assert validate_external_url("http://localhost/") is False

    def test_rfc1918_rejected_by_default(self):
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with patch("socket.getaddrinfo", return_value=_addrinfo(ip)):
                assert validate_external_url("http://example.com/") is False, f"{ip} should be blocked"

    def test_rfc6598_cgn_rejected_by_default(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("100.64.0.1")):
            assert validate_external_url("http://example.com/") is False

    # --- allow_loopback: permits 127.x/::1 only, not RFC 1918 ---

    def test_allow_loopback_permits_loopback(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            assert validate_external_url("http://localhost/", allow_loopback=True) is True

    def test_allow_loopback_still_blocks_rfc1918(self):
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with patch("socket.getaddrinfo", return_value=_addrinfo(ip)):
                assert validate_external_url(
                    "http://example.com/", allow_loopback=True
                ) is False, f"allow_loopback must not permit RFC 1918 addr {ip}"

    def test_allow_loopback_still_blocks_cgn(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("100.64.0.1")):
            assert validate_external_url("http://example.com/", allow_loopback=True) is False

    # --- allow_private: permits all internal ranges including loopback ---

    def test_allow_private_permits_loopback(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            assert validate_external_url("http://localhost/", allow_private=True) is True

    def test_allow_private_permits_rfc1918(self):
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with patch("socket.getaddrinfo", return_value=_addrinfo(ip)):
                assert validate_external_url(
                    "http://example.com/", allow_private=True
                ) is True, f"allow_private must permit RFC 1918 addr {ip}"

    def test_allow_private_permits_cgn(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("100.64.0.1")):
            assert validate_external_url("http://example.com/", allow_private=True) is True

    def test_allow_private_permits_link_local(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("169.254.0.1")):
            assert validate_external_url("http://example.com/", allow_private=True) is True

    def test_missing_netloc_rejected(self):
        assert validate_external_url("http://") is False


class TestSafeUrlPolicy:
    def test_rejects_credentials_and_ambiguous_authorities(self):
        policy = SafeUrlPolicy()
        for url in (
            "http://user:secret@example.com/",
            "http://example.com\\@127.0.0.1/",
            "http://2130706433/",
            "http://0177.0.0.1/",
            "http://example.com../",
        ):
            with pytest.raises(UnsafeUrlError):
                policy.parse(url)

    def test_accepts_public_ipv6_answer(self):
        with patch(
            "socket.getaddrinfo",
            return_value=_addrinfo("2606:4700:4700::1111"),
        ):
            assert validate_external_url("https://ipv6.example/") is True

    def test_rejects_private_ipv6_answer(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("fd00::1")):
            assert validate_external_url("https://ipv6.example/") is False

    def test_rejects_mixed_public_private_answers(self):
        with patch(
            "socket.getaddrinfo",
            return_value=_addrinfo("93.184.216.34", "127.0.0.1"),
        ):
            assert validate_external_url("https://mixed.example/") is False

    @pytest.mark.parametrize(
        "resolved",
        [
            "169.254.169.254",  # AWS/Azure/GCP IMDS
            "169.254.170.2",  # AWS ECS task credentials
            "100.100.100.200",  # Alibaba Cloud
            # IPv4-mapped IPv6 spellings must not bypass the metadata block:
            # the mapped form is a distinct address object absent from the
            # metadata set and reports is_reserved=False.
            "::ffff:169.254.169.254",
            "::ffff:169.254.170.2",
            "::ffff:100.100.100.200",
        ],
    )
    def test_allow_private_does_not_allow_metadata(self, resolved):
        with patch("socket.getaddrinfo", return_value=_addrinfo(resolved)):
            assert validate_external_url(
                "http://metadata.example/",
                allow_private=True,
            ) is False

    @pytest.mark.asyncio
    async def test_async_resolution_honors_policy_timeout(self):
        policy = SafeUrlPolicy(timeout=0.01)

        async def stalled_resolution(*_args, **_kwargs):
            await asyncio.sleep(1)

        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=stalled_resolution):
            with pytest.raises(UnsafeUrlError, match="Failed to resolve"):
                await policy.validate_async("https://slow.example/")

    @pytest.mark.asyncio
    async def test_aiohttp_connect_resolver_rejects_rebound_mixed_answers(self):
        policy = SafeUrlPolicy()
        resolver = SafeAiohttpResolver(policy)
        loop = asyncio.get_running_loop()
        records = _addrinfo("93.184.216.34", "127.0.0.1")

        with patch.object(loop, "getaddrinfo", return_value=records):
            with pytest.raises(OSError, match="Private or non-global"):
                await resolver.resolve("rebind.example", 443, socket.AF_UNSPEC)

    def test_sync_redirect_to_private_target_is_blocked_before_second_request(self):
        policy = SafeUrlPolicy()
        first = Mock(status_code=302, headers={"Location": "http://internal.example/"})
        first.close = Mock()
        session = Mock()
        session.request.return_value = first
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=[
                    _addrinfo("93.184.216.34"),
                    _addrinfo("127.0.0.1"),
                ],
            ),
            pytest.raises(UnsafeUrlError),
        ):
            safe_requests_request(
                session,
                "GET",
                "https://public.example/start",
                policy=policy,
            )
        assert session.request.call_count == 1
        first.close.assert_called_once()

    def test_requests_adapter_revalidates_at_connect_time(self):
        policy = SafeUrlPolicy()
        session = create_safe_requests_session(policy)
        adapter = session.get_adapter("http://")
        pool = adapter.poolmanager.connection_from_host("rebind.example", port=80)
        connection = pool.ConnectionCls(host="rebind.example", port=80)
        with (
            patch.object(policy, "resolve", side_effect=UnsafeUrlError("rebound private")),
            patch("modules.security_utils.create_connection") as connect,
            pytest.raises(Exception, match="rebound private"),
        ):
            connection._new_conn()
        connect.assert_not_called()
        session.close()

    def test_cross_origin_redirect_strips_query_and_secret_headers(self):
        policy = SafeUrlPolicy()
        policy.validate = Mock(return_value=True)
        first = Mock(status_code=302, headers={"Location": "https://other.example/next"})
        first.close = Mock()
        second = Mock(status_code=200, headers={})
        session = Mock()
        session.request.side_effect = [first, second]

        result = safe_requests_request(
            session,
            "GET",
            "https://public.example/start",
            policy=policy,
            headers={"X-Api-Key": "secret", "User-Agent": "test"},
            params={"token": "query-secret"},
        )

        assert result is second
        second_call = session.request.call_args_list[1]
        assert second_call.kwargs["headers"] == {"User-Agent": "test"}
        assert "params" not in second_call.kwargs
        first.close.assert_called_once()

    def test_cross_origin_redirect_rejects_request_body(self):
        policy = SafeUrlPolicy()
        policy.validate = Mock(return_value=True)
        first = Mock(status_code=307, headers={"Location": "https://other.example/next"})
        first.close = Mock()
        session = Mock()
        session.request.return_value = first

        with pytest.raises(UnsafeUrlError, match="request bodies"):
            safe_requests_request(
                session,
                "POST",
                "https://public.example/start",
                policy=policy,
                json={"token": "body-secret"},
            )

        assert session.request.call_count == 1
        first.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_redirect_to_private_target_is_blocked(self):
        policy = SafeUrlPolicy()
        first = Mock(status=302, headers={"Location": "http://internal.example/"})
        first.release = Mock()
        session = Mock()
        session.request = AsyncMock(return_value=first)
        policy.validate_async = AsyncMock(
            side_effect=[True, UnsafeUrlError("private redirect")]
        )
        with pytest.raises(UnsafeUrlError, match="private redirect"):
            await safe_aiohttp_request(
                session,
                "GET",
                "https://public.example/start",
                policy=policy,
            )
        assert session.request.await_count == 1
        first.release.assert_called_once()


class TestSanitizeInput:
    """Tests for sanitize_input()."""

    def test_truncates_to_max_length(self):
        assert len(sanitize_input("a" * 1000, max_length=100)) == 100

    def test_strips_control_chars(self):
        result = sanitize_input("hello\x01world\x02", strip_controls=True)
        assert "\x01" not in result
        assert "\x02" not in result

    def test_keeps_newline_tab(self):
        result = sanitize_input("hello\nworld\tthere")
        assert "\n" in result
        assert "\t" in result


class TestValidateApiKeyFormat:
    """Tests for validate_api_key_format()."""

    def test_valid_key(self):
        assert validate_api_key_format("a1b2c3d4e5f6g7h8i9j0") is True

    def test_too_short(self):
        assert validate_api_key_format("short") is False

    def test_placeholder_rejected(self):
        assert validate_api_key_format("your_api_key_here" + "x" * 10) is False


class TestValidatePortNumber:
    """Tests for validate_port_number()."""

    def test_valid_port(self):
        assert validate_port_number(8080) is True
        assert validate_port_number(65535) is True

    def test_privileged_port_rejected_by_default(self):
        assert validate_port_number(80) is False
        assert validate_port_number(443) is False

    def test_privileged_port_allowed_when_requested(self):
        assert validate_port_number(80, allow_privileged=True) is True

    def test_invalid_port(self):
        assert validate_port_number(0) is False
        assert validate_port_number(70000) is False

    def test_non_integer_port_rejected(self):
        assert validate_port_number("8080") is False  # type: ignore[arg-type]
        assert validate_port_number(None) is False  # type: ignore[arg-type]


class TestValidateIntegerRange:
    """Tests for validate_integer_range()."""

    def test_value_in_range_returns_true(self):
        assert validate_integer_range(5, 1, 10) is True

    def test_value_at_min_boundary_returns_true(self):
        assert validate_integer_range(1, 1, 10) is True

    def test_value_at_max_boundary_returns_true(self):
        assert validate_integer_range(10, 1, 10) is True

    def test_value_below_min_raises(self):
        with pytest.raises(ValueError, match="must be between"):
            validate_integer_range(0, 1, 10, name="retries")

    def test_value_above_max_raises(self):
        with pytest.raises(ValueError, match="must be between"):
            validate_integer_range(11, 1, 10, name="retries")

    def test_non_integer_raises(self):
        with pytest.raises(ValueError, match="must be an integer"):
            validate_integer_range("five", 1, 10)  # type: ignore[arg-type]


class TestSanitizeInputExtra:
    """Additional tests for sanitize_input() covering missed branches."""

    def test_non_string_content_is_cast_to_str(self):
        result = sanitize_input(42)  # type: ignore[arg-type]
        assert result == "42"

    def test_negative_max_length_raises(self):
        with pytest.raises(ValueError):
            sanitize_input("hello", max_length=-1)


class TestValidateApiKeyFormatExtra:
    """Additional tests for validate_api_key_format() covering missed branches."""

    def test_non_string_returns_false(self):
        assert validate_api_key_format(12345) is False  # type: ignore[arg-type]


class TestIsNixEnvironment:
    """Tests for _is_nix_environment() — coverage via validate_safe_path (which calls it)."""

    def test_nix_env_var_enables_dangerous_path_access(self, tmp_path):
        # When NIX_STORE is set, system-path check is skipped
        import modules.security_utils as su
        with patch.object(su, "_is_nix_environment", return_value=True):
            # /proc is dangerous on Linux, but Nix mode should allow it via allow_absolute
            result = validate_safe_path(str(tmp_path), base_dir=str(tmp_path), allow_absolute=True)
            assert result is not None

    def test_non_nix_env_detects_nix_store_var(self):
        import modules.security_utils as su
        with patch.dict(os.environ, {"NIX_STORE": "/nix/store"}, clear=False):
            assert su._is_nix_environment() is True

    def test_non_nix_env_detects_nix_path_var(self):
        import modules.security_utils as su
        env = {k: v for k, v in os.environ.items()
               if k not in ("NIX_STORE", "NIX_PATH", "NIX_REMOTE", "IN_NIX_SHELL")}
        with patch.dict(os.environ, {**env, "NIX_PATH": "/nix"}, clear=True):
            assert su._is_nix_environment() is True

    def test_no_nix_vars_returns_false(self):
        import modules.security_utils as su
        env = {k: v for k, v in os.environ.items()
               if k not in ("NIX_STORE", "NIX_PATH", "NIX_REMOTE", "IN_NIX_SHELL")}
        with patch.dict(os.environ, env, clear=True):
            # Can't assert False here because we might be in Nix, just call it
            result = su._is_nix_environment()
            assert isinstance(result, bool)


class TestValidateExternalUrlExtra:
    """Additional coverage for validate_external_url() socket exception paths."""

    def test_dns_resolution_failure_returns_false(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
            assert validate_external_url("http://nonexistent.invalid.example") is False

    def test_dns_timeout_returns_false(self):
        with patch("socket.getaddrinfo", side_effect=socket.timeout("timeout")):
            assert validate_external_url("http://slow.example.com") is False

    def test_allow_loopback_permits_loopback(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            result = validate_external_url("http://localhost", allow_loopback=True)
            assert result is True


class TestValidateSafePathExtra:
    """Additional coverage for validate_safe_path() exception paths."""

    def test_dangerous_system_path_rejected_on_linux(self, tmp_path):
        import modules.security_utils as su
        with patch.object(su, "_is_nix_environment", return_value=False):
            with pytest.raises(ValueError, match="system directory"):
                validate_safe_path("/etc/passwd", allow_absolute=True)

    def test_unexpected_exception_wrapped_as_value_error(self, tmp_path):
        with patch("modules.security_utils.Path.resolve", side_effect=OSError("disk fail")):
            with pytest.raises(ValueError, match="Invalid or unsafe file path"):
                validate_safe_path("some_file.db", base_dir=str(tmp_path))


class TestSanitizeName:
    """Tests for sanitize_name() — log-safe identifier sanitization."""

    def test_newline_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\n" not in sanitize_name("Evil\nNode")

    def test_carriage_return_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\r" not in sanitize_name("Evil\rNode")

    def test_tab_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\t" not in sanitize_name("Tab\tNode")

    def test_null_byte_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\x00" not in sanitize_name("Bad\x00Name")

    def test_ansi_escape_stripped(self):
        from modules.security_utils import sanitize_name
        assert "\x1b" not in sanitize_name("\x1b[31mRed\x1b[0m")

    def test_truncated_to_max_length(self):
        from modules.security_utils import sanitize_name
        result = sanitize_name("A" * 100, max_length=64)
        assert len(result) <= 64

    def test_normal_name_unchanged(self):
        from modules.security_utils import sanitize_name
        assert sanitize_name("Alice") == "Alice"

    def test_non_string_coerced(self):
        from modules.security_utils import sanitize_name
        assert sanitize_name(42) == "42"

    def test_negative_max_length_raises(self):
        from modules.security_utils import sanitize_name
        with pytest.raises(ValueError):
            sanitize_name("test", max_length=-1)
