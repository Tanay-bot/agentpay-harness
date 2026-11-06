"""Drive the Stripe CLI to get *genuine* test-mode webhook deliveries.

Used by scenarios with ``webhook_source: stripe_cli``.  The harness starts a
local receiver, runs ``stripe listen --forward-to <receiver>``, fires
``stripe trigger <event>`` for each configured trigger, and collects the real
bodies and ``Stripe-Signature`` headers Stripe sends.  Those are delivered to
the agent untouched, so signature verification is tested against Stripe's own
signing, not the harness's.

Requires the ``stripe`` CLI on PATH and a test-mode secret key.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from types import TracebackType

from agentpay_harness.safety import validate_stripe_key

logger = logging.getLogger(__name__)


class StripeCLIError(RuntimeError):
    """The Stripe CLI is missing, misconfigured, or did not deliver the expected events."""


def find_stripe_cli() -> str | None:
    """Path to the ``stripe`` executable, or None if it is not installed."""
    return shutil.which("stripe")


def require_test_key(api_key: str) -> str:
    """The CLI talks to real Stripe, so only a real ``sk_test_``/``rk_test_`` key will do."""
    validate_stripe_key(api_key)
    if not api_key.startswith(("sk_test_", "rk_test_")) or "mock" in api_key:
        raise StripeCLIError(
            "webhook_source: stripe_cli needs a real Stripe test-mode key "
            "(set STRIPE_TEST_SECRET_KEY=sk_test_... or pass --stripe-key)."
        )
    return api_key


class StripeCLISession:
    """A running ``stripe listen`` forwarding to a local URL.

    Use as a context manager; call :meth:`trigger` inside it.
    """

    def __init__(
        self,
        api_key: str,
        forward_url: str,
        cli_path: str | None = None,
        ready_timeout_s: float = 30.0,
    ) -> None:
        self.api_key = require_test_key(api_key)
        self.forward_url = forward_url
        path = cli_path or find_stripe_cli()
        if path is None:
            raise StripeCLIError(
                "The Stripe CLI ('stripe') is not on PATH. Install it from "
                "https://docs.stripe.com/stripe-cli and try again."
            )
        self.cli_path = path
        self.ready_timeout_s = ready_timeout_s
        self._proc: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._output: list[str] = []

    def _run(self, *args: str, timeout_s: float = 120.0) -> str:
        cmd = [self.cli_path, *args, "--api-key", self.api_key]
        try:
            done = subprocess.run(  # noqa: S603 - fixed executable, harness-built args
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise StripeCLIError(f"'stripe {args[0]}' timed out after {timeout_s}s") from exc
        if done.returncode != 0:
            raise StripeCLIError(
                f"'stripe {' '.join(args)}' failed ({done.returncode}): {done.stderr.strip()}"
            )
        return done.stdout.strip()

    def webhook_secret(self) -> str:
        """The ``whsec_`` secret ``stripe listen`` signs forwarded events with."""
        secret = self._run("listen", "--print-secret")
        if not secret.startswith("whsec_"):
            raise StripeCLIError(
                f"Unexpected output from 'stripe listen --print-secret': {secret!r}"
            )
        return secret

    def trigger(self, event: str) -> None:
        """Run ``stripe trigger <event>`` (creates real test-mode objects)."""
        self._run("trigger", event)

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self._output.append(line.rstrip())
            if "Ready!" in line:
                self._ready.set()

    def __enter__(self) -> StripeCLISession:
        cmd = [
            self.cli_path,
            "listen",
            "--forward-to",
            self.forward_url,
            "--api-key",
            self.api_key,
        ]
        self._proc = subprocess.Popen(  # noqa: S603 - fixed executable, harness-built args
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        threading.Thread(target=self._pump, daemon=True).start()
        if not self._ready.wait(self.ready_timeout_s):
            self.__exit__(None, None, None)
            tail = "\n".join(self._output[-5:])
            raise StripeCLIError(f"'stripe listen' did not become ready:\n{tail}")
        logger.info("stripe listen forwarding to %s", self.forward_url)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            if self._proc.stderr is not None:
                self._proc.stderr.close()
            self._proc = None


def wait_for(predicate: Callable[[], bool], timeout_s: float, poll_s: float = 0.25) -> bool:
    """Poll ``predicate()`` until true or ``timeout_s`` passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()
