"""Start the FastAPI backend and Vue development server from the repo root."""
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
processes: list[tuple[str, subprocess.Popen]] = []


def pipe_output(name: str, stream) -> None:
    for line in iter(stream.readline, ""):
        print(f"[{name}] {line}", end="", flush=True)
    stream.close()


def stop_all(*_):
    for _, process in processes:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            else:
                process.terminate()
    deadline = time.time() + 5
    for _, process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                process.kill()


def run_setup(command: list[str], *, cwd: Path, label: str) -> None:
    print(f"[{label}] 正在准备依赖…", flush=True)
    result = subprocess.run(command, cwd=cwd)
    if result.returncode:
        raise RuntimeError(f"{label}依赖准备失败（退出码 {result.returncode}）")


def main() -> int:
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        print("未找到 Node.js，请先安装 Node.js。", file=sys.stderr)
        return 1
    npm_cli = Path(node).resolve().parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not npm_cli.exists():
        print(f"未找到 npm CLI：{npm_cli}", file=sys.stderr)
        return 1

    backend_dir = ROOT / "backend"
    venv_python = backend_dir / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not venv_python.exists():
        run_setup([sys.executable, "-m", "venv", str(backend_dir / ".venv")], cwd=ROOT, label="Python")
    check = subprocess.run(
        [str(venv_python), "-c", "import fastapi, uvicorn, langgraph, httpx, sqlalchemy, docx, pypdf, multipart, pydantic_settings, jose, passlib"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if check.returncode:
        run_setup([str(venv_python), "-m", "pip", "install", "-r", str(backend_dir / "requirements.txt")], cwd=ROOT, label="Python")

    frontend_dir = ROOT / "frontend"
    if not (frontend_dir / "node_modules").exists():
        run_setup([node, str(npm_cli), "install"], cwd=frontend_dir, label="Node.js")

    backend = subprocess.Popen(
        [str(venv_python), "-m", "uvicorn", "app.main:app", "--reload", "--reload-dir", "app"],
        cwd=backend_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    processes.append(("API", backend))
    frontend = subprocess.Popen(
        [node, str(npm_cli), "run", "dev"], cwd=frontend_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    processes.append(("Web", frontend))

    for name, process in processes:
        threading.Thread(target=pipe_output, args=(name, process.stdout), daemon=True).start()
    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)
    print("正在启动：Web http://localhost:5173 · API http://localhost:8000/docs（Ctrl+C 停止）", flush=True)
    try:
        while all(process.poll() is None for _, process in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()
    return next((p.returncode or 0 for _, p in processes if p.returncode not in (None, 0)), 0)


if __name__ == "__main__":
    raise SystemExit(main())
