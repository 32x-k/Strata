"""A local browser chat for the Windows AMD bounded expert-RAM Ring.

This is deliberately separate from the normal external-compatible server, but it keeps the bounded Ring engine
resident by default.  The adapter renders the existing chat template and sends each request through the engine's
persistent ``strata --serve`` loop, so the model, Hybrid/Ring allocation, static GPU cache and conversation state stay
alive between browser turns.  ``--reload-each-turn`` is the opt-in diagnostic fallback that starts one direct Ring
process per turn.

    python serve/ring_chat.py --config strata-q2_0-ring-chat.json --port 8080 --open
    python serve/ring_chat.py --config strata-q2_0-ring-chat.json --reload-each-turn
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from serve.server import ChatTemplate, Service, StrataEngine, child_env, serve  # noqa: E402
from serve.winjob import contain  # noqa: E402


class RingChatError(ValueError):
    """The one-shot Ring process could not produce a completion."""


class RingOneShotEngine:
    """Opt-in Engine-compatible adapter around direct ``strata --tokens-file`` runs.

    ``serve.server.Service`` does the chat-template rendering, tokenization, incremental output parsing and browser
    SSE framing.  This class only supplies the engine boundary: one direct process in, generated token IDs out.  The
    normal browser path uses ``StrataEngine`` below, which keeps the Ring process resident between turns.
    """

    batch = 0
    can_stop = True
    silence_s = 0
    # The direct executable prints the complete token list only after the one-shot run.  Service must not interpret
    # the resulting fast ID burst as model throughput; it uses ``last.decode_ms`` for that adapter instead.
    buffered_output = True

    def __init__(self, exe: str, args: list[str], cwd: str | None = None, log: str | None = None,
                 env: dict | None = None, ring_ram_gb: int | float | None = None):
        self.spawn = (exe, list(args), cwd, log, env, ring_ram_gb)
        self.exe = exe
        self.args = list(args)
        self.cwd = cwd or str(ROOT)
        self.log_path = log
        self.log_start = 0
        self.model_path = self._value("--native") or self._value("--pack", "pack/full")
        self.max_context = self._int_value("--max-context", 4096)
        self.known_ctx = self.max_context
        self.last: dict = {}
        self.info = {
            "version": "ring-direct",
            "ring_mode": "direct one-shot",
            "ring_ram_gb": float(ring_ram_gb) if ring_ram_gb is not None else self._float_value("--expert-ram-gb", 0),
            "backend": "hip",
        }
        self.progress = None
        self.prefill_tok_s_mean = None
        self.unloaded = False
        self.ended = False
        self.starting = False
        self._closed = False
        self._active = None
        self._active_lock = threading.Lock()

    def _value(self, flag: str, default=None):
        try:
            return self.args[self.args.index(flag) + 1]
        except (ValueError, IndexError):
            return default

    def _int_value(self, flag: str, default: int) -> int:
        try:
            return int(self._value(flag, default))
        except (TypeError, ValueError):
            return default

    def _float_value(self, flag: str, default: float) -> float:
        try:
            return float(self._value(flag, default))
        except (TypeError, ValueError):
            return default

    def alive(self) -> bool:
        # There is no resident child between turns.  From Service's point of view the direct adapter is ready.
        return not self._closed

    def exit_code(self):
        with self._active_lock:
            return self._active.poll() if self._active is not None else None

    def restart(self):
        # The next generate() creates a fresh direct process.
        self._closed = False
        self.unloaded = False

    def unload(self):
        self.close_active()
        self.unloaded = True

    def close_active(self):
        with self._active_lock:
            proc = self._active
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def close(self):
        self.close_active()
        self._closed = True
        self.ended = True

    @staticmethod
    def _sampling_args(sampling: dict | None) -> list[str]:
        sampling = sampling or {}
        out: list[str] = []
        try:
            temperature = float(sampling.get("temperature", 0) or 0)
        except (TypeError, ValueError):
            temperature = 0.0
        if temperature > 0:
            out += ["--temperature", f"{temperature:g}"]
            try:
                top_p = float(sampling.get("top_p", 1) or 1)
            except (TypeError, ValueError):
                top_p = 1.0
            if 0 < top_p < 1:
                out += ["--top-p", f"{top_p:g}"]
            try:
                top_k = int(sampling.get("top_k", 0) or 0)
            except (TypeError, ValueError):
                top_k = 0
            if top_k > 0:
                out += ["--top-k", str(top_k)]
        else:
            out.append("--greedy")
        try:
            seed = int(sampling.get("seed", 0) or 0)
        except (TypeError, ValueError):
            seed = 0
        if seed > 0:
            out += ["--seed", str(seed)]
        return out

    @staticmethod
    def _output_ids(text: str) -> list[int]:
        # The direct engine prints the final IDs only after the one-shot run.  Keep this parser strict: a diagnostic
        # line that merely looks similar must not become a partial answer.
        match = re.search(r"(?m)^output\s*:\s*(.*)$", text)
        if not match:
            raise RingChatError("Ring did not print an output token line")
        raw = match.group(1).strip()
        if not raw:
            return []
        try:
            return [int(x) for x in raw.replace(",", " ").split()]
        except ValueError as e:
            raise RingChatError(f"Ring printed malformed output IDs: {raw[:200]!r}") from e

    def _record_stats(self, text: str, prompt_tokens: int, output_tokens: int) -> None:
        def ms_for(kind: str) -> float | None:
            pattern = rf"(?im)^{kind}[^\n]*?in\s+([0-9]+(?:\.[0-9]+)?)\s*ms"
            found = re.search(pattern, text)
            return float(found.group(1)) if found else None

        prompt_ms = ms_for("prefill")
        decode_ms = ms_for("decode")
        self.prefill_tok_s_mean = (prompt_tokens / (prompt_ms / 1000.0)
                                   if prompt_ms and prompt_ms > 0 and prompt_tokens else None)
        self.last = {
            "generated": output_tokens,
            "prompt_tokens": prompt_tokens,
            "prompt_ms": prompt_ms or 0.0,
            "decode_ms": decode_ms or 0.0,
            "finish": "length",
        }
        # --stats is part of the Ring chat command.  Keep its useful figures in /metrics when the direct build prints
        # them, without making the parser depend on a particular wording of the summary line.
        ring = re.search(r"(?im)^\s*expert ring\s+.*?slots\s+(\d+)\s+x\s+(\d+)\s*,?\s*hits\s+(\d+)\s*,?\s*misses\s+(\d+)", text)
        if ring:
            self.info.update({"ring_slots": int(ring.group(1)), "ring_slot_bytes": int(ring.group(2)),
                              "ring_hits": int(ring.group(3)), "ring_misses": int(ring.group(4))})

    def _write_log(self, text: str) -> None:
        if not self.log_path or not text:
            return
        try:
            path = Path(self.log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(text)
                if not text.endswith("\n"):
                    f.write("\n")
        except OSError:
            pass

    def generate(self, ids: list[int], max_new: int, sampling: dict | None, cancel: threading.Event,
                 embeddings=None):
        if embeddings:
            raise ValueError("the Ring chat screen is text-only; image input is not supported")
        if self._closed:
            raise RingChatError("the Ring chat adapter is closed")

        token_path = None
        proc = None
        reader = None
        output: list[str] = []
        try:
            with tempfile.NamedTemporaryFile("w", encoding="ascii", suffix=".ids", delete=False) as f:
                token_path = f.name
                f.write(",".join(str(int(t)) for t in ids))
                f.write("\n")
            cmd = [self.exe, *self.args, "--tokens-file", token_path, "--max-new", str(int(max_new)),
                   *self._sampling_args(sampling)]
            # The API stops forwarding at the first end-of-turn ID, but the direct executable otherwise keeps
            # decoding to max_new before it prints output.  Stop in the executable too, or a short browser answer
            # needlessly runs the whole 1024-token allowance before Service can see it.
            if "--stop-eos" not in cmd and "--eos-ids" not in cmd:
                cmd.append("--stop-eos")
            if "--stats" not in cmd:
                cmd.append("--stats")
            env = dict(self.spawn[4] or os.environ)
            self._write_log(f"\n=== Ring run {time.strftime('%Y-%m-%d %H:%M:%S')} max_new={int(max_new)} ===\n")
            proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            contain(proc)
            with self._active_lock:
                self._active = proc

            def read_output():
                if proc.stdout is not None:
                    for line in proc.stdout:
                        output.append(line)
                        self._write_log(line)             # keep startup/load diagnostics available before the run ends

            reader = threading.Thread(target=read_output, name="ring-output", daemon=True)
            reader.start()
            heartbeat = time.monotonic()
            while proc.poll() is None:
                if cancel.is_set():
                    self.close_active()
                    return
                if time.monotonic() - heartbeat >= 10:
                    heartbeat = time.monotonic()
                    yield None
                time.sleep(0.2)
            reader.join(timeout=5)
            text = "".join(output)
            if proc.returncode != 0:
                tail = text[-4000:].strip()
                raise RingChatError(f"Ring exited with code {proc.returncode}" + (f": {tail}" if tail else ""))
            produced = self._output_ids(text)
            self._record_stats(text, len(ids), len(produced))
            for token in produced:
                if cancel.is_set():
                    return
                yield token
        finally:
            if proc is not None and proc.poll() is None:
                self.close_active()
            with self._active_lock:
                if self._active is proc:
                    self._active = None
            if token_path:
                try:
                    os.unlink(token_path)
                except OSError:
                    pass


def load_config(path: Path, reload_each_turn: bool = False) -> tuple[dict, object, object, ChatTemplate]:
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict):
        raise SystemExit("Ring chat config must be a JSON object")
    cwd = str(Path(cfg.get("cwd") or ROOT).resolve())
    exe = cfg.get("exe")
    args = cfg.get("args")
    if not isinstance(exe, str) or not exe or not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise SystemExit("Ring chat config needs string 'exe' and string-list 'args'")
    if not os.path.isabs(exe):
        exe = str(Path(cwd) / exe)
    if "--serve" in args:
        raise SystemExit("Ring chat uses the direct engine; remove --serve from the Ring args")
    if "--expert-ram-gb" not in args:
        raise SystemExit("Ring chat needs --expert-ram-gb in the direct Ring args")
    if not reload_each_turn:
        missing = [flag for flag in ("--spec", "--mtp", "--prefill") if flag not in args]
        if missing:
            raise SystemExit("persistent Ring chat needs " + ", ".join(missing) +
                             "; use --reload-each-turn for the one-shot diagnostic mode")
    tokenizer_path = cfg.get("tokenizer")
    if not isinstance(tokenizer_path, str) or not tokenizer_path:
        pack = next((args[i + 1] for i, x in enumerate(args[:-1]) if x == "--pack"), None)
        tokenizer_path = str(Path(pack) / "tokenizer") if pack else ""
    tpath = Path(tokenizer_path)
    if not tpath.is_absolute():
        tpath = Path(cwd) / tpath
    if not (tpath / "vocab.json").is_file():
        raise SystemExit(f"the Ring chat tokenizer is missing: {tpath / 'vocab.json'}")
    import strata_tokenizer as ST
    tok = ST.Tokenizer.from_pack(tpath)
    template_path = tpath / "chat_template.jinja"
    if not template_path.is_file():
        template_path = ROOT / "serve" / "chat_template.jinja"
    ring_ram = cfg.get("ring_ram_gb")
    if ring_ram is None:
        try:
            ring_ram = int(args[args.index("--expert-ram-gb") + 1])
        except (ValueError, IndexError):
            ring_ram = 0
    env = child_env(cfg)
    if reload_each_turn:
        engine = RingOneShotEngine(exe, args, cwd=cwd, log=cfg.get("log"), env=env, ring_ram_gb=ring_ram)
    else:
        # StrataEngine starts the same executable with --serve.  The bounded Ring now supports this single-GPU,
        # fixed-profile, fixed-verifier path, so the model and the Hybrid/Ring arenas are allocated only once.
        engine = StrataEngine(exe, args, cwd=cwd, log=cfg.get("log"), env=env)
        engine.info.update({"ring_mode": "persistent serve", "ring_ram_gb": float(ring_ram), "backend": "hip"})
    return cfg, engine, tok, ChatTemplate(template_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="the generated *-ring-chat.json")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--open", action="store_true", help="open the local chat page in the browser")
    ap.add_argument("--reload-each-turn", action="store_true",
                    help="diagnostic mode: reload the model and bounded Ring/Hybrid allocation for every turn")
    a = ap.parse_args()
    cfg, engine, tok, template = load_config(Path(a.config).resolve(), reload_each_turn=a.reload_each_turn)
    host = a.host or cfg.get("host") or "127.0.0.1"
    svc = Service(engine, tok, template, model_name=cfg.get("model_name", "qwen3.8-flash-next-ring"),
                  fit_max_tokens=cfg.get("fit_max_tokens") is True)
    svc.ring_mode = True
    svc.api_key = ""                         # loopback-only browser page; no external API is advertised
    svc.backend = "hip"
    svc.gpu_index = int(cfg.get("gpu", 0) if not isinstance(cfg.get("gpu"), list) else cfg["gpu"][0])
    svc.gpu_indices = [svc.gpu_index]
    svc.config_path = None
    svc.shared_path = None
    svc.repeat_stop_tokens = 0                 # the direct run already has a finite --max-new boundary
    httpd = serve(svc, host=host, port=a.port)
    here = "127.0.0.1" if host in ("0.0.0.0", "") else host
    mode = "direct one-shot; the model reloads for each turn" if a.reload_each_turn else \
           "persistent serve; the model and Ring/Hybrid allocation stay loaded"
    print(f"Ring chat ready: http://{here}:{a.port}/ ({mode})", flush=True)
    if a.open:
        import webbrowser
        webbrowser.open(f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{a.port}/")

    def on_sigterm(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[ring-chat] stopping ...", flush=True)
        httpd.shutdown()
        engine.close()
        print("[ring-chat] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
