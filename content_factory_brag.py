"""Run the real latent-spaces/brag Codex plugin inside Content Factory.

The full Brag workflow owns the storyboard and delegates composition/rendering
to Hyperframes. This module deliberately passes only the OpenAI API key and a
sanitized copy of the Agency W project into the Codex subprocess.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class BragError(RuntimeError):
    pass


def _safe_copy_project(destination: Path) -> None:
    source = Path("/app")
    if not source.is_dir():
        raise BragError("Исходный проект Агентства W недоступен Brag.")
    ignored = shutil.ignore_patterns(
        ".git",
        ".streamlit",
        ".env",
        ".env.*",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "tests",
        "deploy",
        "brag-output*",
        "*.sqlite3",
        "*.log",
    )
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignored)


def _prepare_codex_home(destination: Path, openai_api_key: str) -> None:
    source = Path(os.environ.get("CODEX_HOME", "/opt/codex-home"))
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("auth.json", ".tmp"),
        )
    else:
        destination.mkdir(parents=True, exist_ok=True)
    auth_path = destination / "auth.json"
    auth_path.write_text(
        json.dumps({"OPENAI_API_KEY": str(openai_api_key)}, ensure_ascii=False),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)


def _subprocess_env(openai_api_key: str, home: Path, codex_home: Path) -> dict[str, str]:
    # Do not forward the worker's Supabase, Telegram, Runway or social keys.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": openai_api_key,
        "HYPERFRAMES_NO_UPDATE_CHECK": "1",
        "HYPERFRAMES_AGENT": "1",
        "PRODUCER_LOW_MEMORY_MODE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _prompt(project: dict, job: dict) -> str:
    brief = job.get("launch_brief") if isinstance(job.get("launch_brief"), dict) else {}
    scenes = brief.get("scenes") if isinstance(brief.get("scenes"), list) else []
    scene_direction = "\n".join(
        f"- {index}. {str(scene.get('visual') or '').strip()} | "
        f"on-screen text: {str(scene.get('text') or '').strip()}"
        for index, scene in enumerate(scenes[:5], 1)
        if isinstance(scene, dict)
    )
    title = str(brief.get("title") or project.get("name") or "Agency W").strip()
    tone = str(brief.get("tone") or "polished").strip()
    hook = str(brief.get("hook") or "").strip()
    share = str(brief.get("share_copy") or "").strip()

    return f"""Use the $brag skill and its FULL Hyperframes workflow for this repository.
Do not use brag-slim. Do not use Runway or any other video generator.
Create a polished vertical launch video for the actual project in this workspace.

Invocation intent:
- title: {title}
- tone: {tone}
- format: vertical
- duration: 20 seconds (stay within Brag's normal 15–25 second window)
- scene budget: no more than 5 scenes; do not invent a sixth scene
- voiceover: off
- music: on
- output: the normal brag-output directory with brag.mp4, brag.jpg and share-copy.txt

Creative direction from the owner:
Hook: {hook or 'Show the strongest real product result immediately.'}
The product is Agency W. Show the REAL product/interface/code where possible.
Do not invent fake UI, fake customers, fake metrics, fake testimonials or fake product modules.
Use the exact real module names in Russian: «Стагирит», «Неония», «Неона», «Контент-завод».
For Agency W branding, the official mark is the repository asset assets/agency_w_icon.png.
Whenever the brand name «Агентство W» is shown as a visual title or final brand lockup, do not typeset
a plain text letter W. Use assets/agency_w_icon.png as the W brand mark, preserving its proportions
and transparency. Do not redraw, restyle or replace this logo with a generic W glyph.
Never replace Agency W modules with third-party apps such as Google Calendar, Trello, Gmail,
Outlook, Figma, Photoshop or generic SaaS dashboards.
Never invent Latinized product names such as StagirIT or Neoniya.
Never use the obsolete Agency W URL agency-w.streamlit.app. The current public Agency W URL is
https://agency-w-web.onrender.com, and the browser address bar should not be emphasized unless it
adds real value to the story.
The user especially wants the real workflow: Стагирит/coordination → Неония/candidate search →
real candidate context → Неона/personalized first message → Контент-завод.
If an authenticated internal UI cannot be inspected, explicitly use a real owner-provided Agency W
screenshot/asset or a truthful branded text scene; do not fabricate the screen.
The point is to show how AI takes routine work so the person gets time back.

Owner-approved storyboard direction (use as direction, not as permission to invent screens):
{scene_direction or '- Inspect the project and choose the strongest real UI moments.'}

Suggested share copy direction:
{share or 'Explain briefly that Agency W delegates routine to AI and returns time to the person.'}

Important:
1. Inspect the repository first, as the Brag skill requires.
2. Follow the actual $brag gates and Hyperframes checks.
3. At least one scene must display actual product UI/copy/key visual.
4. Render locally with Hyperframes and FFmpeg.
5. Finish only when brag.mp4 exists and the Brag delivery gate passes.
"""


def run_brag(project: dict, job: dict, openai_api_key: str, progress=None) -> tuple[dict, list[dict]]:
    if not str(openai_api_key or "").strip():
        raise BragError("Для Brag не подключён OpenAI API key.")

    root = Path(tempfile.mkdtemp(prefix="agency-w-brag-"))
    workspace = root / "workspace"
    home = root / "home"
    codex_home = root / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    try:
        if progress:
            progress({"stage": "brag_prepare", "message": "Brag готовит безопасную копию проекта."})
        _safe_copy_project(workspace)
        _prepare_codex_home(codex_home, str(openai_api_key))

        prompt = _prompt(project, job)
        prompt_path = root / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        result_path = root / "codex-result.txt"

        if progress:
            progress({"stage": "brag_run", "message": "Запущен настоящий /brag: сценарий → Hyperframes → MP4."})

        command = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(result_path),
            prompt,
        ]
        proc = subprocess.run(
            command,
            cwd=workspace,
            env=_subprocess_env(str(openai_api_key), home, codex_home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
            check=False,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
            safe_tail = "\n".join(line[:500] for line in tail)
            raise BragError(
                "Brag не завершил создание ролика. "
                + (safe_tail[-2000:] if safe_tail else f"Код выхода: {proc.returncode}")
            )

        candidates = sorted(
            workspace.glob("brag-output*/brag.mp4"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise BragError("Brag завершился без файла brag.mp4.")
        video = candidates[0]
        output_dir = video.parent
        if video.stat().st_size <= 0:
            raise BragError("Brag создал пустой видеофайл.")

        share_path = output_dir / "share-copy.txt"
        share_copy = (
            share_path.read_text(encoding="utf-8").strip()
            if share_path.is_file()
            else str((job.get("launch_brief") or {}).get("share_copy") or "").strip()
        )
        if not share_copy:
            share_copy = "Агентство W показывает, какие рутинные задачи можно передать ИИ и вернуть себе время."

        assets = [{
            "filename": "brag.mp4",
            "data": video.read_bytes(),
            "mime_type": "video/mp4",
        }]
        poster = output_dir / "brag.jpg"
        if poster.is_file() and poster.stat().st_size > 0:
            assets.append({
                "filename": "brag.jpg",
                "data": poster.read_bytes(),
                "mime_type": "image/jpeg",
            })

        content = {
            "title": str((job.get("launch_brief") or {}).get("title") or project.get("name") or "Agency W"),
            "text": share_copy,
            "quality_review": {"approved": True, "issues": []},
            "video_engine": "brag",
            "brag_runtime": {
                "workflow": "latent-spaces/brag",
                "renderer": "hyperframes",
                "voiceover": False,
            },
        }
        if progress:
            progress({"stage": "brag_ready", "message": "Brag.mp4 готов и сохраняется в «Мои генерации»."})
        return content, assets
    except subprocess.TimeoutExpired as exc:
        raise BragError("Brag не завершил рендер за допустимое время.") from exc
    finally:
        shutil.rmtree(root, ignore_errors=True)
