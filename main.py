import asyncio
import threading
import json
import re
import sys
import traceback
import base64
import binascii
from pathlib import Path

import pyaudio
from google import genai
from google.genai import types
import time 
from ui import JarvisUI
from memory.memory_manager import load_memory, update_memory, format_memory_for_prompt

from agent.task_queue import get_queue

from actions.flight_finder import flight_finder
from actions.open_app         import open_app
from actions.weather_report   import weather_action
from actions.send_message     import send_message
from actions.reminder         import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor import screen_process
from actions.youtube_video    import youtube_video
from actions.cmd_control      import cmd_control
from actions.desktop          import desktop_control
from actions.browser_control  import browser_control as _browser_control_original
from actions.browser_control  import _bt as _global_bt, _ensure_started as _global_ensure_started
import types as _types_global

async def _edge_launch_global(self_bt):
    """Always use Microsoft Edge. Falls back to Chromium if Edge not found."""
    if self_bt._browser and self_bt._browser.is_connected():
        # Already running — reuse existing browser
        if self_bt._page is None or self_bt._page.is_closed():
            self_bt._page = await self_bt._context.new_page()
        return
    print("[Browser] 🔵 Launching Microsoft Edge...")
    try:
        self_bt._browser = await self_bt._playwright.chromium.launch(
            headless=False,
            channel="msedge",
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        print("[Browser] ✅ Edge launched.")
    except Exception as e:
        print(f"[Browser] ⚠️ Edge failed ({e}), trying Chrome...")
        try:
            self_bt._browser = await self_bt._playwright.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--start-maximized"],
            )
            print("[Browser] ✅ Chrome launched.")
        except Exception as e2:
            print(f"[Browser] ⚠️ Chrome failed ({e2}), using built-in Chromium...")
            self_bt._browser = await self_bt._playwright.chromium.launch(
                headless=False,
                args=["--start-maximized"],
            )
    self_bt._context = await self_bt._browser.new_context(
        viewport=None,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
        ),
    )
    self_bt._page = await self_bt._context.new_page()

# Patch the global browser thread to always use Edge
_global_bt._launch = _types_global.MethodType(_edge_launch_global, _global_bt)

def browser_control(parameters: dict, player=None, **kwargs):
    """
    Wrapper around the original browser_control that adds extra actions:
    play_first_video, click_first_result, go_back, go_forward, refresh, accept_cookies.
    All original actions pass through unchanged.
    """
    from actions.browser_control import _bt, _ensure_started
    import asyncio, concurrent.futures

    _ensure_started()
    action = (parameters or {}).get("action", "").lower().strip()

    # ── New actions ──────────────────────────────────────────────────────────
    async def _play_first_video():
        page = await _bt._get_page()
        current_url = page.url

        # Wait for page to settle
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(1)

        # YouTube
        if "youtube.com" in current_url:
            # Wait up to 12s for YouTube results to render
            try:
                await page.wait_for_selector("ytd-video-renderer", timeout=12000)
            except Exception:
                pass
            await asyncio.sleep(1)
            yt_selectors = [
                "ytd-video-renderer a#video-title",
                "ytd-video-renderer h3 a",
                "#video-title",
                "a#video-title",
                "ytd-rich-item-renderer a#video-title-link",
                "ytd-compact-video-renderer a",
                ".ytd-video-renderer a[href*='watch']",
            ]
            for sel in yt_selectors:
                try:
                    await page.wait_for_selector(sel, timeout=2000)
                    await page.click(sel)
                    return f"Clicked first YouTube video ({sel})."
                except Exception:
                    continue
            # Last resort: find any /watch link
            try:
                el = await page.query_selector('a[href*="/watch?v="]')
                if el:
                    await el.click()
                    return "Clicked YouTube watch link."
            except Exception:
                pass
            return "YouTube: could not find video to click."

        # Dailymotion
        if "dailymotion.com" in current_url:
            dm_selectors = [
                "article.VideoCard a",
                "a.VideoCard_videoCard__oHyBn",
                ".VideoCard a",
                'a[href*="/video/"]',
                "a.video_card",
            ]
            for sel in dm_selectors:
                try:
                    await page.wait_for_selector(sel, timeout=2000)
                    await page.click(sel)
                    return "Clicked first Dailymotion video."
                except Exception:
                    continue
            return "Dailymotion: could not find video link."

        # Generic — any site
        generic_selectors = [
            'a[href*="/watch?v="]',
            'a[href*="watch"]',
            'a[href*="/video/"]',
            ".video-title a",
            ".title a",
            "h3 a",
            "a.title",
        ]
        for sel in generic_selectors:
            try:
                await page.wait_for_selector(sel, timeout=3000)
                await page.click(sel)
                return "Clicked first video link."
            except Exception:
                continue
        return "Could not find any video link on page."

    async def _click_first_result():
        page = await _bt._get_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        for sel in ["h3 a", ".result a", ".rc a", "a[data-ved]",
                    ".search a", "li a", ".item a", "a[href]"]:
            try:
                await page.wait_for_selector(sel, timeout=3000)
                await page.click(sel)
                return "Clicked first search result."
            except Exception:
                continue
        return "Could not find first result."

    async def _go_back():
        page = await _bt._get_page()
        await page.go_back()
        return "Went back."

    async def _go_forward():
        page = await _bt._get_page()
        await page.go_forward()
        return "Went forward."

    async def _refresh():
        page = await _bt._get_page()
        await page.reload()
        return "Page refreshed."

    async def _accept_cookies():
        page = await _bt._get_page()
        for text in ["Accept", "Accept all", "Accept All", "I agree",
                     "Allow all", "OK", "Agree", "Got it"]:
            try:
                btn = page.get_by_role("button", name=text, exact=False)
                await btn.first.click(timeout=3000)
                return f"Accepted cookies: '{text}'."
            except Exception:
                continue
        return "No cookie banner found."

    extra_actions = {
        "play_first_video":   _play_first_video,
        "click_first_result": _click_first_result,
        "go_back":            _go_back,
        "go_forward":         _go_forward,
        "refresh":            _refresh,
        "accept_cookies":     _accept_cookies,
    }

    if action in extra_actions:
        try:
            # Use _bt.run() — same mechanism as all other browser actions
            result = _bt.run(extra_actions[action](), timeout=45)
        except concurrent.futures.TimeoutError:
            result = f"Action '{action}' timed out."
        except Exception as e:
            result = f"Action '{action}' error: {e}"
        if player:
            player.write_log(f"[browser] {result[:60]}")
        print(f"[Browser] {result[:80]}")
        return result

    # ── Pass-through to original ──────────────────────────────────────────────
    return _browser_control_original(parameters=parameters, player=player, **kwargs)
from actions.file_controller  import file_controller
from actions.code_helper      import code_helper
from actions.dev_agent        import dev_agent
from actions.web_search       import web_search as web_search_action
from actions.computer_control import computer_control

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
FORMAT              = pyaudio.paInt16
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

pya = pyaudio.PyAudio()

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_memory_turn_counter  = 0
_memory_turn_lock     = threading.Lock()
_MEMORY_EVERY_N_TURNS = 5
_last_memory_input    = ""


def _update_memory_async(user_text: str, jarvis_text: str) -> None:
    global _memory_turn_counter, _last_memory_input

    with _memory_turn_lock:
        _memory_turn_counter += 1
        current_count = _memory_turn_counter

    if current_count % _MEMORY_EVERY_N_TURNS != 0:
        return

    text = user_text.strip()
    if len(text) < 10:
        return
    if text == _last_memory_input:
        return
    _last_memory_input = text

    try:
        import google.generativeai as genai
        genai.configure(api_key=_get_api_key())
        model = genai.GenerativeModel("gemini-2.5-flash-lite")

        check = model.generate_content(
            f"Does this message contain personal facts about the user "
            f"(name, age, city, job, hobby, relationship, birthday, preference)? "
            f"Reply only YES or NO.\n\nMessage: {text[:300]}"
        )
        if "YES" not in check.text.upper():
            return

        raw = model.generate_content(
            f"Extract personal facts from this message. Any language.\n"
            f"Return ONLY valid JSON or {{}} if nothing found.\n"
            f"Extract: name, age, birthday, city, job, hobbies, preferences, relationships, language.\n"
            f"Skip: weather, reminders, search results, commands.\n\n"
            f"Format:\n"
            f'{{"identity":{{"name":{{"value":"..."}}}}}}, '
            f'"preferences":{{"hobby":{{"value":"..."}}}}, '
            f'"notes":{{"job":{{"value":"..."}}}}}}\n\n'
            f"Message: {text[:500]}\n\nJSON:"
        ).text.strip()

        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        if not raw or raw == "{}":
            return

        data = json.loads(raw)
        if data:
            update_memory(data)
            print(f"[Memory] ✅ Updated: {list(data.keys())}")

    except json.JSONDecodeError:
        pass
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] ⚠️ {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TOOL DECLARATIONS  — descriptions are carefully separated to avoid ambiguity
# ─────────────────────────────────────────────────────────────────────────────
TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens a LOCAL application installed on the computer. "
            "Use ONLY for desktop apps like: Notepad, Calculator, Spotify, "
            "VS Code, File Explorer, Settings, Discord, Steam, Paint, etc. "
            "NEVER use for websites, YouTube, Google, or anything browser-related. "
            "For any web task use browser_control instead."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the LOCAL application (e.g. 'Notepad', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Fetches factual information from the web and RETURNS it as text to read aloud. "
            "Use ONLY when the user wants a spoken/text answer, like: "
            "'what is the population of France', 'who invented the telephone', 'latest news about X'. "
            "NEVER use if the user wants to SEE a browser open or navigate a website — "
            "use browser_control for that."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) or compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "price | specs | reviews"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "weather_report",
        "description": "Gets real-time weather information for a city.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Windows Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Use ONLY for: summarizing a YouTube video's content, getting metadata/info "
            "about a specific video URL, or fetching the trending videos list as spoken text. "
            "NEVER use to play or open YouTube in the browser — "
            "use browser_control with action='go_to' and url='https://www.youtube.com/results?search_query=...' for that."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "summarize | get_info | trending  — do NOT use 'play', use browser_control for playing"
                },
                "query":  {"type": "STRING", "description": "Search query"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam image. "
            "MUST be called when user asks what is on screen, what you see, "
            "analyze my screen, look at camera, etc. "
            "You have NO visual ability without this tool. "
            "After calling this tool, stay SILENT — the vision module speaks directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {
                    "type": "STRING",
                    "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"
                },
                "text": {
                    "type": "STRING",
                    "description": "The question or instruction about the captured image"
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "ALSO use for repeated actions: 'refresh 10 times', 'reload page 5 times' → action: reload_n, value: 10. "
            "Use for ANY single computer control command — even if repeated N times. "
            "NEVER route simple computer commands to agent_task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform (if known). For repeated reload: 'reload_n'"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, number of times, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "THE ONLY tool for anything that requires opening Chrome or a web browser. "
            "Use for ALL of these: opening any website, searching Google/Bing/DuckDuckGo, "
            "playing YouTube/Dailymotion/any video, clicking links, filling web forms, "
            "scrolling pages, shopping, checking prices, booking anything online. "
            "ALWAYS chain multiple browser_control calls to complete a task: "
            "Step 1 — navigate with go_to. Step 2 — play_first_video to click the first result. "
            "For YouTube: go_to 'https://www.youtube.com/results?search_query=QUERY' then play_first_video. "
            "For Dailymotion: go_to 'https://www.dailymotion.com/search/QUERY' then play_first_video. "
            "For Google search: go_to 'https://www.google.com/search?q=QUERY'. "
            "For clicking buttons/links on current page: use click_first_result or smart_click. "
            "NEVER stop after just navigating — always follow up with play_first_video for video tasks. "
            "If it involves a browser, a URL, or a website — ALWAYS use this tool."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | press | close | play_first_video | click_first_result | accept_cookies | go_back | go_forward | refresh"},
                "url":         {"type": "STRING", "description": "Full URL for go_to action (e.g. 'https://youtube.com/results?search_query=lofi+music')"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up or down for scroll"},
                "key":         {"type": "STRING", "description": "Key name for press action"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": (
            "Manages files and folders. Use for: listing files, creating/deleting/moving/copying "
            "files, reading file contents, finding files by name or extension, checking disk usage, "
            "organizing the desktop, getting file info."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "cmd_control",
        "description": (
            "Runs CMD/terminal commands by understanding natural language. "
            "Use when user wants to: find large files, check disk space, list processes, "
            "get system info, navigate folders, check network, find files by name, "
            "or do ANYTHING in the command line they don't know how to do themselves."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task":    {"type": "STRING", "description": "Natural language description of what to do. Example: 'find the 10 largest files on C drive'"},
                "visible": {"type": "BOOLEAN", "description": "Open visible CMD window so user can see. Default: true"},
                "command": {"type": "STRING", "description": "Optional: exact command if already known"},
            },
            "required": ["task"]
        }
    },
    {
        "name": "desktop_control",
        "description": (
            "Controls the desktop. Use for: changing wallpaper, organizing desktop files, "
            "cleaning the desktop, listing desktop contents, or ANY other desktop-related task "
            "the user describes in natural language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language description of any desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": (
            "Writes, edits, explains, runs, or self-builds code files. "
            "Use for ANY coding request: writing a script, fixing a file, "
            "editing existing code, running a file, or building and testing automatically."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do, or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file (full path or filename)"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit / explain / run / build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": (
            "Builds complete multi-file projects from scratch. "
            "Plans structure, writes all files, installs dependencies, "
            "opens VSCode, runs the project, and fixes errors automatically. "
            "Use for any project larger than a single script."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step tasks that require MULTIPLE DIFFERENT tools. "
            "Always respond to the user in the language they spoke. "
            "Examples: 'research X and save to file', 'find files and organize them', "
            "'fill a form on a website', 'write and test code'. "
            "DO NOT use for simple computer commands like volume, refresh, close, scroll, "
            "minimize, screenshot, restart, shutdown — use computer_settings for those. "
            "DO NOT use if the task can be done with a single tool call."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {
                    "type": "STRING",
                    "description": "Complete description of what needs to be accomplished"
                },
                "priority": {
                    "type": "STRING",
                    "description": "low | normal | high (default: normal)"
                }
            },
            "required": ["goal"]
        }
    },
    {
        "name": "computer_control",
        "description": (
            "Direct computer control: type text, click buttons, use keyboard shortcuts, "
            "scroll, move mouse, take screenshots, fill forms, find elements on screen. "
            "Use when the user wants to interact with any app on the computer directly. "
            "Can generate random data for forms or use user's real info from memory."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate for click/move"},
                "y":           {"type": "INTEGER", "description": "Y coordinate for click/move"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key to press e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "Scroll direction: up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER", "description": "Seconds to wait"},
                "title":       {"type": "STRING", "description": "Window title for focus_window"},
                "description": {"type": "STRING", "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING", "description": "Data type for random_data: name|email|username|password|phone|birthday|address"},
                "field":       {"type": "STRING", "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "flight_finder",
        "description": (
            "Searches for flights on Google Flights and speaks the best options. "
            "Use when user asks about flights, plane tickets, uçuş, bilet, etc."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":       {"type": "STRING",  "description": "Departure city or airport code"},
                "destination":  {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":         {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date":  {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":   {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":        {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":         {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    }
]


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None

    def speak(self, text: str):
        """Thread-safe speak — any thread can call this."""
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
         )
    
    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime 

        memory  = load_memory()
        mem_str = format_memory_for_prompt(memory)

        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders. "
            f"If user says 'in 2 minutes', add 2 minutes to this time.\n\n"
        )

        if mem_str:
            sys_prompt = time_ctx + mem_str + "\n\n" + sys_prompt
        else:
            sys_prompt = time_ctx + sys_prompt

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=sys_prompt,
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon" 
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 TOOL: {name}  ARGS: {args}")

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(
                    None, lambda: open_app(parameters=args, response=None, player=self.ui)
                )
                result = r or f"Opened {args.get('app_name')} successfully."

            elif name == "weather_report":
                r = await loop.run_in_executor(
                    None, lambda: weather_action(parameters=args, player=self.ui)
                )
                result = r or f"Weather report for {args.get('city')} delivered."

            elif name == "browser_control":
                _bc_action = args.get("action", "")
                if _bc_action == "play_video":
                    # Auto-chain: go_to then play_first_video
                    _goto_args = {"action": "go_to", "url": args.get("url", "")}
                    _play_args = {"action": "play_first_video"}
                    r = await loop.run_in_executor(
                        None, lambda a=_goto_args: browser_control(parameters=a, player=self.ui)
                    )
                    await asyncio.sleep(2)
                    r2 = await loop.run_in_executor(
                        None, lambda a=_play_args: browser_control(parameters=a, player=self.ui)
                    )
                    result = f"{r} | {r2}"
                else:
                    r = await loop.run_in_executor(
                        None, lambda a=args: browser_control(parameters=a, player=self.ui)
                    )
                    result = r or "Browser action completed."

            elif name == "file_controller":
                r = await loop.run_in_executor(
                    None, lambda: file_controller(parameters=args, player=self.ui)
                )
                result = r or "File operation completed."

            elif name == "send_message":
                r = await loop.run_in_executor(
                    None, lambda: send_message(
                        parameters=args, response=None,
                        player=self.ui, session_memory=None
                    )
                )
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(
                    None, lambda: reminder(parameters=args, response=None, player=self.ui)
                )
                result = r or f"Reminder set for {args.get('date')} at {args.get('time')}."

            elif name == "youtube_video":
                r = await loop.run_in_executor(
                    None, lambda: youtube_video(parameters=args, response=None, player=self.ui)
                )
                result = r or "Done."

            elif name == "screen_process":
                threading.Thread(
                    target=screen_process,
                    kwargs={"parameters": args, "response": None,
                            "player": self.ui, "session_memory": None},
                    daemon=True
                ).start()
                result = (
                    "Vision module activated. "
                    "Stay completely silent — vision module will speak directly."
                )

            elif name == "computer_settings":
                r = await loop.run_in_executor(
                    None, lambda: computer_settings(
                        parameters=args, response=None, player=self.ui
                    )
                )
                result = r or "Done."

            elif name == "cmd_control":
                r = await loop.run_in_executor(
                    None, lambda: cmd_control(parameters=args, player=self.ui)
                )
                result = r or "Command executed."

            elif name == "desktop_control":
                r = await loop.run_in_executor(
                    None, lambda: desktop_control(parameters=args, player=self.ui)
                )
                result = r or "Desktop action completed."

            elif name == "code_helper":
                r = await loop.run_in_executor(
                    None, lambda: code_helper(
                        parameters=args,
                        player=self.ui,
                        speak=self.speak 
                    )
                )
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(
                    None, lambda: dev_agent(
                        parameters=args,
                        player=self.ui,
                        speak=self.speak
                    )
                )
                result = r or "Done."

            elif name == "agent_task":
                goal         = args.get("goal", "")
                priority_str = args.get("priority", "normal").lower()

                from agent.task_queue import get_queue, TaskPriority
                priority_map = {
                    "low":    TaskPriority.LOW,
                    "normal": TaskPriority.NORMAL,
                    "high":   TaskPriority.HIGH,
                }
                priority = priority_map.get(priority_str, TaskPriority.NORMAL)

                queue   = get_queue()
                task_id = queue.submit(
                    goal=goal,
                    priority=priority,
                    speak=self.speak,
                )
                result = f"Task started (ID: {task_id}). I'll update you as I make progress, sir."

            elif name == "web_search":
                r = await loop.run_in_executor(
                    None, lambda: web_search_action(parameters=args, player=self.ui)
                )
                result = r or "Search completed."

            elif name == "computer_control":
                r = await loop.run_in_executor(
                    None, lambda: computer_control(parameters=args, player=self.ui)
                )
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(
                    None, lambda: flight_finder(parameters=args, player=self.ui)
                )
                result = r or "Done."

            else:
                result = f"Unknown tool: {name}"
            
        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()

        print(f"[JARVIS] 📤 {name} → {result[:80]}")

        return types.FunctionResponse(
            id=fc.id,
            name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=SEND_SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        try:
            while True:
                data = await asyncio.to_thread(
                    stream.read, CHUNK_SIZE, exception_on_overflow=False
                )
                await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})
        except Exception as e:
            print(f"[JARVIS] ❌ Mic error: {e}")
            raise
        finally:
            stream.close()

    async def _receive_audio(self):
        print("[JARVIS] 🔊 Recv started")
        out_buf = []
        in_buf  = []

        try:
            while True:
                turn = self.session.receive()
                async for response in turn:

                    if response.data:
                        chunk = response.data
                        raw_chunk = None

                        if isinstance(chunk, (bytes, bytearray)):
                            raw_chunk = bytes(chunk)
                        elif isinstance(chunk, str):
                            try:
                                raw_chunk = base64.b64decode(chunk, validate=True)
                                print("[JARVIS][AUDIO][RECV] decoded base64 -> pcm bytes")
                            except (binascii.Error, ValueError) as e:
                                print(f"[JARVIS][AUDIO][RECV] invalid base64 audio chunk: {e}")
                        else:
                            print(f"[JARVIS][AUDIO][RECV] unsupported chunk type: {type(chunk).__name__}")

                        if raw_chunk:
                            print(
                                f"[JARVIS][AUDIO][RECV] type={type(raw_chunk).__name__} "
                                f"bytes={len(raw_chunk)} queue_before={self.audio_in_queue.qsize()}"
                            )
                            self.audio_in_queue.put_nowait(raw_chunk)
                            print(f"[JARVIS][AUDIO][RECV] queued queue_after={self.audio_in_queue.qsize()}")

                    if response.server_content:
                        sc = response.server_content

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = sc.input_transcription.text.strip()
                            if txt:
                                in_buf.append(txt)

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = sc.output_transcription.text.strip()
                            if txt:
                                out_buf.append(txt)

                        if sc.turn_complete:
                            full_in  = ""
                            full_out = ""

                            if in_buf:
                                full_in = " ".join(in_buf).strip()
                                if full_in:
                                    self.ui.write_log(f"You: {full_in}")
                            in_buf = []

                            if out_buf:
                                full_out = " ".join(out_buf).strip()
                                if full_out:
                                    self.ui.write_log(f"Jarvis: {full_out}")
                            out_buf = []

                            if full_in and len(full_in) > 5:
                                threading.Thread(
                                    target=_update_memory_async,
                                    args=(full_in, full_out),
                                    daemon=True
                                ).start()

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 Tool call: {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )

        except Exception as e:
            print(f"[JARVIS] ❌ Recv error: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] Play started")
        output_device_index = 21
        input_rate = 24000
        print("[JARVIS] Forced speaker index: 21 (Headphones - JBL Tune 520BT Stereo)")

        stream = None
        active_rate = None
        rates_to_try = (24000, 48000, 44100)

        def _resample_pcm16_mono(data: bytes, src_rate: int, dst_rate: int) -> bytes:
            if src_rate == dst_rate or not data:
                return data
            from array import array

            samples = array("h")
            samples.frombytes(data)
            in_len = len(samples)
            if in_len == 0:
                return b""

            out_len = max(1, int(round(in_len * dst_rate / src_rate)))
            out = array("h", [0] * out_len)
            step = src_rate / dst_rate

            for i in range(out_len):
                src_pos = i * step
                left = int(src_pos)
                frac = src_pos - left
                if left >= in_len - 1:
                    out[i] = samples[in_len - 1]
                else:
                    s0 = samples[left]
                    s1 = samples[left + 1]
                    out[i] = int(s0 + (s1 - s0) * frac)

            result = out.tobytes()
            arr = __import__('numpy').frombuffer(result, dtype=__import__('numpy').int16)
            arr = __import__('numpy').clip(arr * 8, -32768, 32767).astype(__import__('numpy').int16)
            return arr.tobytes()

        try:
            while True:
                if stream is None:
                    for candidate_rate in rates_to_try:
                        try:
                            stream = await asyncio.to_thread(
                                pya.open,
                                format=pyaudio.paInt16,
                                channels=1,
                                rate=candidate_rate,
                                output=True,
                                output_device_index=None,
                                frames_per_buffer=1024,
                            )
                            active_rate = candidate_rate
                            print(f"[JARVIS][AUDIO][PLAY] opened output stream rate={active_rate}")
                            break
                        except Exception as open_err:
                            print(f"[JARVIS][AUDIO][PLAY] open failed rate={candidate_rate}: {open_err}")

                    if stream is None:
                        print("[JARVIS][AUDIO][PLAY] cannot open forced device 21 yet, retrying in 3s")
                        await asyncio.sleep(3)
                        continue

                chunk = await self.audio_in_queue.get()
                print(f"AUDIO CHUNK SIZE: {len(chunk) if chunk else 0}")

                if not chunk:
                    continue

                if not isinstance(chunk, (bytes, bytearray)):
                    print(f"[JARVIS][AUDIO][PLAY] skip non-bytes chunk type={type(chunk).__name__}")
                    continue

                if len(chunk) > 500000:
                    print(f"[JARVIS][AUDIO][PLAY] skip oversized chunk: {len(chunk)}")
                    continue

                out_chunk = bytes(chunk)
                if active_rate != input_rate:
                    out_chunk = _resample_pcm16_mono(out_chunk, input_rate, active_rate)
                    if not out_chunk:
                        continue
                    print(
                        f"[JARVIS][AUDIO][PLAY] resampled {input_rate}->{active_rate}, "
                        f"bytes={len(out_chunk)}"
                    )

                try:
                    await asyncio.to_thread(stream.write, out_chunk)
                    print("[JARVIS][AUDIO][PLAY] stream.write ok")
                except Exception as write_err:
                    print(f"[JARVIS][AUDIO][PLAY] stream.write failed: {write_err}")
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                    stream = None
                    active_rate = None

        except Exception as e:
            print(f"[JARVIS] Play error: {e}")
            traceback.print_exc()

        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()

    async def run(self):
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        while True:
            try:
                print("[JARVIS] 🔑 Connecting...")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop() 
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=10)

                    print("[JARVIS] ✅ Connected.")
                    self.ui.write_log("JARVIS online.")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())

            except Exception as e:
                print(f"[JARVIS] ⚠️  Error: {e}")
                traceback.print_exc()

            print("[JARVIS] 🔄 Reconnecting in 3s...")
            await asyncio.sleep(3)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()