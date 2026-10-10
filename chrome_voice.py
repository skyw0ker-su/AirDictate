"""Голосовое управление Google Chrome.

Команда начинается со слова «хром» (или «браузер»), например: «хром открой ютуб», «хром напиши привет».
Речь распознаёт офлайн модель sherpa-onnx (assets/vosk-model-small-ru), слушает микрофон пульта.

  python chrome_voice.py           слушать и управлять Chrome
  python chrome_voice.py --dry     только печатать распознанное и команду, клавиши не нажимать
"""
import ctypes, os, queue, re, sys, time
from ctypes import wintypes

import numpy as np
import sounddevice as sd
import sherpa_onnx

import voice_remote as v

MODEL = os.path.join(v.ASSETS, "vosk-model-small-ru")
RATE = 16000
VK_CONTROL, VK_MENU, VK_SHIFT, VK_TAB = 0x11, 0x12, 0x10, 0x09
VK_RETURN, VK_LEFT, VK_RIGHT, VK_PRIOR, VK_NEXT = 0x0D, 0x25, 0x27, 0x21, 0x22
VK_L, VK_F, VK_T, VK_W, VK_F5 = 0x4C, 0x46, 0x54, 0x57, 0x74

SITES = {"ютуб": "youtube.com", "гугл": "google.com", "яндекс": "ya.ru", "почту": "mail.ru", "почта": "mail.ru",
         "вконтакте": "vk.com", "википедию": "wikipedia.org", "википедия": "wikipedia.org"}

WAKE = re.compile(r"^(хром|хрома|браузер)\b[,\s]*")
# (шаблон, действие): действия с аргументом берут текст из второй группы
RULES = [
    (r"^(открой|открыть|перейди на|зайди на|зайти на) (.+)$", "open"),
    (r"^(найди|поищи|поиск) (.+)$", "find"),
    (r"^(напиши|напечатай|печатай) (.+)$", "type"),
    (r"^новая вкладка$", "newtab"),
    (r"^(закрой|закрыть) вкладку$", "closetab"),
    (r"^следующая вкладка$", "nexttab"),
    (r"^назад$", "back"),
    (r"^(вперёд|вперед)$", "forward"),
    (r"^(вниз|листай вниз)$", "down"),
    (r"^(вверх|листай вверх)$", "up"),
    (r"^(обнови|обновить|перезагрузи)$", "reload"),
    (r"^(enter|энтер|нажми enter|ввод)$", "enter"),
    (r"^(стоп|хватит|выход)$", "stop"),
]


def parse(text):
    """Текст после слова «хром» -> (действие, аргумент) или None."""
    text = " ".join(text.lower().replace("ё", "е").split())
    for pattern, action in RULES:
        m = re.match(pattern, text.replace("ё", "е"))
        if m:
            return action, (m.group(m.lastindex) if m.lastindex and m.lastindex > 1 else "")
    return None


def url_for(arg):
    """«ютуб» -> youtube.com; то, что похоже на адрес, оставляем как есть; остальное Chrome ищет сам."""
    a = arg.strip().lower()
    if a in SITES:
        return SITES[a]
    return a


# --- окно Chrome ---
def chrome_window():
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        v.user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1" or v.user32.GetWindow(hwnd, 4) or not v.user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        v.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        path = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        if path.value.lower().endswith("chrome.exe"):
            found.append(hwnd)
        return True

    v.user32.EnumWindows(EnumProc(cb), 0)
    fg = v.user32.GetForegroundWindow()
    if fg in found:
        return fg
    return found[0] if found else None      # EnumWindows идёт сверху вниз по Z-порядку


# --- ввод текста в любое поле (Unicode) ---
KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def type_text(text):
    events = []
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            e = INPUT()
            e.type = 1                                   # INPUT_KEYBOARD
            e.u.ki = KEYBDINPUT(0, ord(ch), flags, 0, 0)
            events.append(e)
    if events:
        arr = (INPUT * len(events))(*events)
        v.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


# --- выполнение команды в Chrome ---
def do(action, arg, dry):
    keys = {
        "newtab": ((VK_CONTROL, VK_T),), "closetab": ((VK_CONTROL, VK_W),),
        "nexttab": ((VK_CONTROL, VK_TAB),), "back": ((VK_MENU, VK_LEFT),), "forward": ((VK_MENU, VK_RIGHT),),
        "down": ((VK_NEXT,),), "up": ((VK_PRIOR,),), "reload": ((VK_F5,),), "enter": ((VK_RETURN,),),
    }
    if action == "stop":
        return False
    if dry:
        print("   действие:", action, repr(arg), flush=True)
        return True
    hwnd = chrome_window()
    if not hwnd:
        print("   Chrome не открыт", flush=True)
        return True
    v.focus(hwnd)
    time.sleep(0.15)
    if action in keys:
        for combo in keys[action]:
            v.press_keys(*combo)
    elif action == "open":
        v.press_keys(VK_CONTROL, VK_L)
        time.sleep(0.15)
        type_text(url_for(arg))
        time.sleep(0.1)
        v.press_keys(VK_RETURN)
    elif action == "find":
        v.press_keys(VK_CONTROL, VK_F)
        time.sleep(0.15)
        type_text(arg)
        time.sleep(0.1)
        v.press_keys(VK_RETURN)
    elif action == "type":
        type_text(arg)
    return True


class Listener:
    def __init__(self, device, rate):
        self.rate = rate
        self.q = queue.Queue()
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=os.path.join(MODEL, "am", "encoder.onnx"),
            decoder=os.path.join(MODEL, "am", "decoder.onnx"),
            joiner=os.path.join(MODEL, "am", "joiner.onnx"),
            tokens=os.path.join(MODEL, "lang", "tokens.txt"),
            num_threads=2, sample_rate=RATE, decoding_method="greedy_search")
        self.device = device

    def decode(self, x, rate):
        if rate != RATE:
            n = int(len(x) * RATE / rate)
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        s = self.rec.create_stream()
        s.accept_waveform(RATE, x.astype(np.float32))
        self.rec.decode_stream(s)
        return s.result.text.strip().lower()

    def segments(self, chunks, rate, on, off):
        """Режет поток на фразы: начало — громче порога on, конец — 0.8 с тишины ниже off (или 6 с максимум)."""
        pre, buf, talking, quiet = [], [], False, 0.0
        for x in chunks:
            r = float(np.sqrt(np.mean(x ** 2)))
            dur = len(x) / rate
            if not talking:
                pre = (pre + [x])[-3:]
                if r > on:
                    talking, buf, quiet = True, list(pre), 0.0
            else:
                buf.append(x)
                quiet = quiet + dur if r < off else 0.0
                if quiet > 0.8 or len(buf) * dur > 6.0:
                    talking, pre = False, []
                    yield np.concatenate(buf)

    def run(self, dry):
        cfg = v.load_cfg()
        on, off = cfg.get("speech_on", 0.004), cfg.get("silence_off", 0.0015)
        block = self.rate // 10

        def cb(indata, frames, t, status):
            self.q.put(indata[:, 0].copy())
        with sd.InputStream(device=self.device, channels=1, samplerate=self.rate, blocksize=block,
                            dtype="float32", callback=cb):
            print("слушаю. Скажите: «хром открой ютуб» (стоп - чтобы выйти)", flush=True)

            def chunks():
                while True:
                    yield self.q.get()
            for utter in self.segments(chunks(), self.rate, on, off):
                text = self.decode(utter, self.rate)
                print("услышал:", text, flush=True)
                m = WAKE.match(text)
                if not m:
                    continue
                cmd = parse(text[m.end():])
                if cmd is None:
                    print("   не поняла команду", flush=True)
                    continue
                if not do(cmd[0], cmd[1], dry):
                    print("выход", flush=True)
                    return


def main():
    dry = "--dry" in sys.argv
    dev = v.find_audio(v.load_cfg()["audio_device"])
    if dev is None:
        print("микрофон пульта не найден")
        return
    rate = int(sd.query_devices(dev)["default_samplerate"])
    Listener(dev, rate).run(dry)


if __name__ == "__main__":
    main()
