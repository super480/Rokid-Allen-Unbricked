"""Chat through the Mac's signed-in Google Antigravity CLI."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from backends.base import Backend, BackendError, report
from config import GEMINI_NAME
from sandbox import clean_env

DEFAULT_MODEL = "gemini-4-hydrogen-low"
MODEL_ID = re.compile(r"gemini-[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}")
MAX_TURNS = 6
LOGIN_HINT = "请在 Mac 终端运行 agy，用你的个人 Google 账号登录。"
ACCOUNT_SETTINGS = json.dumps({"modelProvider": "", "useG1Credits": False})
CHAT_SETTINGS = json.dumps({
    **json.loads(ACCOUNT_SETTINGS),
    "permissions": {
        "allow": [],
        "deny": ["command(*)", "unsandboxed(*)", "read_file(*)", "write_file(*)",
                 "read_url(*)", "execute_url(*)", "mcp(*)"],
    },
})


def normalise_model(value):
    model = str(value or "").strip()
    if model and not MODEL_ID.fullmatch(model):
        raise ValueError("请填写 agy models 列出的 Gemini 模型 ID，例如 " + DEFAULT_MODEL)
    return model or DEFAULT_MODEL


def executable():
    launcher = Path.home() / ".local" / "bin" / "agy"
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return str(launcher)
    return shutil.which("agy")


def _cli_env():
    env = clean_env()
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL",
                "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI"):
        env.pop(key, None)
    return env


def _invoke(args, timeout, cwd, prompt=None):
    input_options = {"stdin": subprocess.DEVNULL}
    if prompt is not None:
        message = {"event": "user", "message": {"content": prompt}}
        input_options = {"input": json.dumps(message, ensure_ascii=False) + "\n"}
    try:
        return subprocess.run(args, cwd=cwd, env=_cli_env(), **input_options,
                              capture_output=True, text=True, errors="replace",
                              timeout=timeout, start_new_session=True)
    except subprocess.TimeoutExpired:
        raise BackendError("Antigravity CLI 响应超时，请检查 Mac 的网络或登录状态。") from None
    except OSError:
        raise BackendError("无法启动本机 Antigravity CLI。") from None


def _cli_error(detail):
    detail = detail.lower()
    if any(word in detail for word in ("sign in", "authentication", "unauthorized",
                                        "log in", "gemini_api_key")):
        return LOGIN_HINT
    if any(word in detail for word in ("429", "quota", "rate limit", "resource exhausted",
                                        "resource_exhausted", "credits")):
        return "Antigravity 账号额度暂不可用，请等待额度重置。"
    if "invalid model" in detail or "unknown model" in detail:
        return "这个模型在本机账号中不可用，请在哈基米设置里检查模型 ID。"
    return "Antigravity CLI 未返回有效结果，请检查 Mac 的网络和 agy 状态。"


class GeminiBackend(Backend):
    id = "gemini"
    name = GEMINI_NAME

    def __init__(self, settings, state_store=None):
        self.model = str(settings.get("model") or "").strip()
        self.timeout = int(settings.get("timeout_seconds") or 60)
        self.turns = []
        self.used_model = ""

    def health(self):
        if not executable():
            return False, "找不到 agy 命令，请在 Mac 上安装 Google Antigravity CLI。"
        return True, "已检测到 Antigravity CLI"

    def current_model(self):
        return normalise_model(self.model)

    def models(self):
        return list(dict.fromkeys([self.current_model(), DEFAULT_MODEL]))

    def _account_models(self, path, cwd):
        result = _invoke([path, "--settings", ACCOUNT_SETTINGS, "models"], 12, cwd)
        if result.returncode:
            raise BackendError(_cli_error(result.stderr + result.stdout))
        models = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and MODEL_ID.fullmatch(parts[0]):
                models.append(parts[0])
        return models

    def check_account(self):
        path = executable()
        if not path:
            raise BackendError(self.health()[1])
        with tempfile.TemporaryDirectory(prefix="rokid-gemini-") as cwd:
            return self._account_models(path, cwd)

    def run(self, text, request_id, on_progress=None):
        path = executable()
        if not path:
            raise BackendError(self.health()[1])
        text = (text or "").strip()
        if not text:
            raise BackendError("没有收到问题内容。")
        model = self.current_model()
        prompt = text
        if self.turns:
            history = "\n".join(f"用户：{question}\n哈基米：{answer}" for question, answer in self.turns)
            prompt = f"以下是对话历史，请回答最后一条用户消息。\n{history}\n用户：{text}"
        report(on_progress, "thinking")
        with tempfile.TemporaryDirectory(prefix="rokid-gemini-") as cwd:
            self._account_models(path, cwd)
            result = _invoke([
                path, "--settings", CHAT_SETTINGS, "--mode", "plan", "--sandbox",
                "--disable-slash-commands", "--input-format", "stream-json",
                "--output-format", "stream-json", "--model", model,
            ], self.timeout, cwd, prompt=prompt)
        payload = {}
        for line in reversed(result.stdout.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("result"), dict):
                payload = event["result"]
                break
        answer = str(payload.get("response") or "").strip()
        if result.returncode or payload.get("status") != "SUCCESS" or not answer:
            raise BackendError(_cli_error(result.stderr + str(payload.get("error") or "")))
        self.turns = (self.turns + [(text, answer)])[-MAX_TURNS:]
        self.used_model = model
        return answer
