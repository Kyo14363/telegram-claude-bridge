#!/usr/bin/env python3
"""
獨立 SDK smoke test — 繞過 T-M-B，純測 claude-agent-sdk + 你的 claude.exe 能否啟動。
Run from the repository root with: python sdk_smoke_test.py

目的：把 SDK 子進程的 stderr 全部攤出來，定位 "Failed to start Claude Code" 真正原因。
"""
import asyncio
import os
import sys
import traceback
from pathlib import Path

CLI_PATH = Path(os.path.expandvars(
    r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
))

print("=" * 60)
print("Claude Agent SDK smoke test")
print("=" * 60)
print(f"Python : {sys.executable}")
print(f"Python ver: {sys.version}")
print(f"CLI path  : {CLI_PATH}")
print(f"CLI exists: {CLI_PATH.exists()}")

# 1. SDK import
try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock, ResultMessage
    import claude_agent_sdk
    print(f"SDK ver   : {claude_agent_sdk.__version__}")
except Exception as e:
    print(f"[FATAL] SDK import failed: {e}")
    traceback.print_exc()
    input("\nPress Enter to exit...")
    sys.exit(1)

# 2. 直接呼叫 claude.exe --version 看能不能跑
import subprocess
print("\n[Test 1] subprocess.run claude.exe --version")
try:
    r = subprocess.run(
        [str(CLI_PATH), "--version"],
        capture_output=True, text=True, timeout=15,
    )
    print(f"  returncode: {r.returncode}")
    print(f"  stdout: {r.stdout.strip()}")
    if r.stderr:
        print(f"  stderr: {r.stderr.strip()}")
except Exception as e:
    print(f"  FAILED: {type(e).__name__}: {e}")

# 3. SDK connect 測試（短任務）
print("\n[Test 2] SDK connect + simple query")

async def smoke():
    captured_stderr = []
    def on_stderr(line):
        captured_stderr.append(line)
        print(f"  [CLI stderr] {line.rstrip()}")

    options = ClaudeAgentOptions(
        cwd=str(Path(os.environ.get("WORKING_DIR", Path.home() / "claude-workspace"))),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project", "local"],
        skills="all",
        system_prompt={"type": "preset", "preset": "claude_code"},
        cli_path=str(CLI_PATH),
        stderr=on_stderr,
    )
    print(f"  options.cli_path = {options.cli_path}")
    print(f"  options.cwd      = {options.cwd}")

    client = ClaudeSDKClient(options=options)
    try:
        print("  -> connect()...")
        await client.connect()
        print("  ✅ connected")

        print("  -> query('say hi')...")
        await client.query("just reply with the word: hi")

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, TextBlock):
                        print(f"  Claude: {blk.text}")
            elif isinstance(msg, ResultMessage):
                print(f"  ResultMessage: subtype={msg.subtype}, is_error={msg.is_error}, session={msg.session_id[:12] if msg.session_id else None}")
                break
        print("  ✅ query OK")

    except Exception as e:
        print(f"  ❌ FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        if captured_stderr:
            print("\n  --- captured CLI stderr ---")
            for line in captured_stderr:
                print(f"  {line.rstrip()}")
        else:
            print("  (no CLI stderr captured)")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

asyncio.run(smoke())
print("\n" + "=" * 60)
input("Press Enter to exit...")
