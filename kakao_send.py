# -*- coding: utf-8 -*-
"""카카오톡 PC 앱으로 1:1 메시지 보내기.

사용법:
    python kakao_send.py "받는사람" "메시지"
    python kakao_send.py "받는사람" "메시지" --dry-run     # 검색/판정만 하고 보내지 않음

종료 코드:
    0 전송 성공 (또는 dry-run 에서 대상 1개 확인)
    2 카카오톡 로그아웃/잠금 상태 (멈춤 - 직접 로그인 필요)
    3 1:1 채팅방이 0개 또는 2개 이상 (후보 목록 출력, 보내지 않음)
    4 그 외 오류 (보내지 않음)
"""
import argparse
import asyncio
import ctypes
import datetime
import json
import os
import re
import subprocess
import sys
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import pyautogui
import pyperclip
from pywinauto import Desktop
from pywinauto.keyboard import send_keys

KAKAO_EXE = r"C:\Program Files (x86)\Kakao\KakaoTalk\KakaoTalk.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
CONTACTS = os.path.join(HERE, "contacts.json")
SHOT = os.path.join(HERE, "last_search.png")
MAIN_CLASS = "EVA_Window_Dblclk"
WM_PASTE = 0x0302

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


class Stop(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def pause(sec=0.7):
    time.sleep(sec)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 연락처
def resolve_name(alias):
    try:
        with open(CONTACTS, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    return data.get(alias, alias)


# ---------------------------------------------------------------- 창 찾기
def desktop():
    return Desktop(backend="win32")


def find_main():
    for w in desktop().windows(title="카카오톡", class_name=MAIN_CLASS):
        if any(c.window_text().startswith("OnlineMainView") for c in w.children()):
            return w
    return None


def find_any_main():
    ws = desktop().windows(title="카카오톡", class_name=MAIN_CLASS)
    return ws[0] if ws else None


def ensure_main():
    """로그인된 메인 창을 찾아 활성화. 로그아웃 상태면 Stop(2)."""
    w = find_main()
    if w is None:
        if find_any_main() is None:
            if not os.path.exists(KAKAO_EXE):
                raise Stop(4, f"카카오톡 실행 파일이 없습니다: {KAKAO_EXE}")
            subprocess.Popen([KAKAO_EXE])
        for _ in range(20):
            pause(1)
            w = find_main()
            if w:
                break
        if w is None:
            raise Stop(2, "카카오톡이 로그아웃(또는 로그인 화면) 상태입니다. 직접 로그인한 뒤 다시 요청해 주세요.")
    # 화면 잠금 모드 확인
    for c in w.children():
        if c.window_text().startswith("LockModeView") and c.is_visible():
            raise Stop(2, "카카오톡이 잠금 상태입니다. 직접 잠금을 해제한 뒤 다시 요청해 주세요.")
    try:
        w.restore()
    except Exception:
        pass
    w.set_focus()
    pause(0.8)
    return w


def child_by_prefix(parent, prefix, visible=None):
    for c in parent.descendants():
        if c.window_text().startswith(prefix):
            if visible is None or c.is_visible() == visible:
                return c
    return None


# ---------------------------------------------------------------- 클립보드
class Clip:
    def __enter__(self):
        try:
            self.old = pyperclip.paste()
        except Exception:
            self.old = None
        return self

    def __exit__(self, *a):
        try:
            if self.old is not None:
                pyperclip.copy(self.old)
        except Exception:
            pass


def select_all_safe():
    """Ctrl+A 는 카카오톡에서 '친구 추가' 단축키라 쓰면 안 된다. 커서 기준으로 전체 선택."""
    pyautogui.hotkey("ctrl", "end")
    pyautogui.hotkey("ctrl", "shift", "home")


def clear_line(n=80):
    """한 줄 입력창을 비운다 (단축키 없이 Backspace 만 사용)."""
    pyautogui.press("end")
    pyautogui.press("backspace", presses=n, interval=0.005)


def paste_hotkey():
    pyautogui.hotkey("ctrl", "v")


# ---------------------------------------------------------------- OCR
def grab(rect):
    return pyautogui.screenshot(region=(rect.left, rect.top, rect.width(), rect.height()))


def ocr_lines(img, max_x=None):
    """[(text, x, y_center, h)] 반환. 같은 줄(y 근접)은 합친다."""
    try:
        import winocr
    except ImportError:
        raise Stop(4, "winocr 가 설치되어 있지 않습니다 (pip install winocr)")

    async def run():
        return await winocr.recognize_pil(img, "ko")

    try:
        res = asyncio.run(run())
    except Exception as e:
        raise Stop(4, f"OCR 실패: {e}")
    raw = []
    for ln in res.lines:
        ws = [w for w in ln.words if max_x is None or w.bounding_rect.x <= max_x]
        if not ws:
            continue
        x = min(w.bounding_rect.x for w in ws)
        text = " ".join(w.text for w in ws).strip()
        y0 = min(w.bounding_rect.y for w in ws)
        y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in ws)
        raw.append([text, x, (y0 + y1) / 2, y1 - y0])
    raw.sort(key=lambda r: (r[2], r[1]))
    merged = []
    for r in raw:
        if merged and abs(merged[-1][2] - r[2]) < 9:
            a = merged[-1]
            if r[1] < a[1]:
                a[0], r[0] = r[0], a[0]
                a[1] = r[1]
            a[0] = (a[0] + " " + r[0]).strip()
        else:
            merged.append(r)
    return [tuple(m) for m in merged]


GROUP_RE = re.compile(r"^(.*?)\s+(\d{1,4})$")


def norm(s):
    return re.sub(r"\s+", "", s)


def same_name(found, display):
    """OCR 결과 이름이 표시 이름과 같은지. '나와의 채팅'의 '나' 배지가 O/0 등으로 읽히는 경우만 허용."""
    f, d = norm(found), norm(display)
    if f == d:
        return True
    return len(f) == len(d) + 1 and f.endswith(d) and f[0] in "OoQ0나"


def parse_rows(lines, width):
    """검색 결과 줄들을 행(row)으로 묶고 [(name, is_group, y_center)] 반환."""
    rows = []
    last_y = None
    for text, x, y, h in lines:
        if x > width * 0.68:  # 시간/배지 열
            continue
        if x < 60:  # 아바타 열
            continue
        if last_y is None or y - last_y > 42:
            m = GROUP_RE.match(text)
            if m:
                rows.append((m.group(1).strip(), True, y))
            elif text.isdigit():  # 이름을 못 읽은(이모지 등) 단톡방
                rows.append(("", True, y))
            else:
                rows.append((text, False, y))
        last_y = y
    return rows


# ---------------------------------------------------------------- 검색
def do_search(main, name):
    r = main.rectangle()
    # 채팅 탭 (왼쪽 세로 아이콘 두 번째)
    pyautogui.click(r.left + 40, r.top + 146)
    pause(0.8)
    edit = None
    for c in main.descendants(class_name="Edit"):
        if c.is_visible() and c.rectangle().width() > 100:
            edit = c
            break
    if edit is None:
        # 검색 버튼(돋보기) 눌러 검색창 열기
        pyautogui.click(r.left + 352, r.top + 69)
        pause(0.8)
        for c in main.descendants(class_name="Edit"):
            if c.is_visible() and c.rectangle().width() > 100:
                edit = c
                break
    if edit is None:
        raise Stop(4, "검색창을 찾지 못했습니다.")
    er = edit.rectangle()
    pyautogui.click((er.left + er.right) // 2, (er.top + er.bottom) // 2)
    pause(0.5)
    clear_line()
    pyperclip.copy(name)
    paste_hotkey()
    pause(1.2)  # 검색 결과 갱신 대기
    lst = child_by_prefix(main, "SearchListCtrl", visible=True)
    if lst is None:
        # 결과가 아예 없으면 목록이 숨겨질 수 있음 → 빈 결과
        return None, []
    lr = lst.rectangle()
    img = grab(lr)
    img.save(SHOT)
    rows = parse_rows(ocr_lines(img, max_x=lr.width() * 0.68), lr.width())
    return lr, rows


def clear_search(main):
    """검색창을 비워 원래 상태로 돌려놓는다 (실패해도 무시)."""
    try:
        main.set_focus()
        for c in main.descendants(class_name="Edit"):
            if c.is_visible() and c.rectangle().width() > 100:
                er = c.rectangle()
                pyautogui.click((er.left + er.right) // 2, (er.top + er.bottom) // 2)
                clear_line()
                break
    except Exception:
        pass


# ---------------------------------------------------------------- 채팅창
def find_chat_window(name):
    for w in desktop().windows(class_name=MAIN_CLASS, visible_only=True):
        if w.window_text() == name:
            if any(c.class_name() == "RICHEDIT50W" for c in w.children()):
                return w
    return None


def header_looks_group(chat, name):
    """채팅창 상단의 방 이름 옆에 인원수 숫자가 있으면 단톡방으로 판단."""
    r = chat.rectangle()
    from PIL import Image  # noqa
    img = pyautogui.screenshot(region=(r.left, r.top, int(r.width() * 0.65), 80))
    for text, x, y, h in ocr_lines(img):
        m = GROUP_RE.match(text)
        if m and norm(m.group(1)) == norm(name):
            return True
    return False


def send_in_chat(chat, message):
    chat.restore()
    chat.set_focus()
    pause(0.8)
    edit = None
    for c in chat.children():
        if c.class_name() == "RICHEDIT50W":
            edit = c
            break
    if edit is None:
        raise Stop(4, "메시지 입력창을 찾지 못했습니다.")
    er = edit.rectangle()
    pyautogui.click(er.left + 40, er.top + 20)
    pause(0.5)
    select_all_safe()
    pyperclip.copy(message)
    paste_hotkey()
    pause(0.8)
    pyautogui.press("enter")
    pause(1.0)
    # 입력창이 비었는지로 전송 확인
    try:
        left = edit.window_text().strip()
    except Exception:
        left = ""
    if left and left != "메시지 입력":
        raise Stop(4, "전송 후에도 입력창에 내용이 남아 있습니다. 전송 여부를 직접 확인해 주세요.")


# ---------------------------------------------------------------- 메인
def main_flow(alias, message, dry_run):
    display = resolve_name(alias)
    if display != alias:
        log(f"호칭 '{alias}' → 카톡 이름 '{display}'")
    with Clip():
        main = ensure_main()
        lr, rows = do_search(main, display)
        cands = [(n, g) for n, g, _ in rows]
        matches = [(n, g, y) for n, g, y in rows if not g and same_name(n, display)]
        if len(matches) != 1:
            log(f"[중단] '{display}' 와 정확히 일치하는 1:1 채팅방이 {len(matches)}개입니다. 보내지 않았습니다.")
            log("후보 목록(OCR 결과):")
            if not rows:
                log("  (검색 결과 없음)")
            for n, g in cands:
                log(f"  - {n or '(이름 판독 불가)'}  [{'단톡방' if g else '1:1/기타'}]")
            log(f"검색 결과 스크린샷: {SHOT}")
            clear_search(main)
            raise Stop(3, "")
        _, _, y = matches[0]
        if dry_run:
            log(f"[dry-run] 1:1 채팅방 1개 확인: {display} (보내지 않음)")
            clear_search(main)
            return
        pyautogui.doubleClick(lr.left + 180, lr.top + int(y))
        pause(1.2)
        chat = find_chat_window(display)
        if chat is None:
            raise Stop(4, f"'{display}' 채팅창이 열리지 않았습니다. 보내지 않았습니다.")
        if header_looks_group(chat, display):
            raise Stop(3, "열린 방이 단톡방(인원수 표시)으로 보입니다. 보내지 않았습니다.")
        send_in_chat(chat, message)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        one = message.replace("\n", " ⏎ ")
        log(f"[전송 완료] 받는사람: {display} | 내용: {one} | 시각: {now}")
        try:
            chat.close()
        except Exception:
            pass
        pause(0.5)
        clear_search(main)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("recipient")
    ap.add_argument("message")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.message.strip():
        log("메시지가 비어 있습니다.")
        return 4
    try:
        main_flow(a.recipient, a.message, a.dry_run)
        return 0
    except Stop as s:
        if s.msg:
            log(f"[중단] {s.msg}")
        return s.code
    except Exception as e:  # noqa
        log(f"[오류] {type(e).__name__}: {e}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
